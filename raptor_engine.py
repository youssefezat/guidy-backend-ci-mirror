import csv
import json
from collections import defaultdict
import time
import math
import heapq
import datetime
from osrm_client import OSRMClient
from live_tracking import LivePositionStore


# Vehicle type detection is based on agency_id, since that's the most
# reliable signal in this feed (route_short_name is often just a bare
# word like "Microbus" or "Minibus" with no route number, and route_type
# is '3' for literally everything -- there's no GTFS route_type
# distinction between a formal CTA bus and an informal 14-seater
# microbus in this data).
AGENCY_VEHICLE_TYPES = {
    "CTA": "bus",       # Cairo Transport Authority (formal bus)
    "CTA_M": "minibus",  # Minibus licensed by CTA
    "LTRA_M": "minibus", # Minibus licensed by LTRA
    "BOX": "microbus",   # Box Paratransit
    "P_B_8": "microbus",  # Paratransit 8-seater
    "P_O_14": "microbus",  # Paratransit 14-seater (the classic Cairo microbus)
    "COOP": "bus",        # Cooperative paratransit 29-seater
    "MM": "bus",           # Mwasalat Misr (formal operator)
    "GRN": "bus",           # Green Bus (formal operator)
    "PGT": "microbus",       # Peugeot (informal shared taxi/microbus)
    "NAT": "metro",          # National Authority for Tunnels (Cairo Metro)
    "CAI_APM": "apm",        # Cairo Airport inter-terminal shuttle
    # Reconstructed from the Cairo Governorate corridor documents -- real
    # stop sequences, estimated times. See RECONSTRUCTED_AGENCY_IDS.
    "GOV_CTA": "bus",
    "GOV_CTA_M": "minibus",
}

# ...but agency_id STOPS being sufficient once one agency runs more than one
# mode. NAT owns the Cairo Metro, the East Nile Monorail AND the Cairo LRT,
# so "NAT -> metro" would have quietly labelled a monorail leg as a metro
# leg -- and, worse, charged it the metro's fare tiers, which are a
# separate ticket a rider pays on top.
#
# The rail rows added by build_rail_gtfs.py DO carry a real GTFS route_type,
# unlike the surface network where everything is '3'. So route_type wins
# where it is meaningful, and agency_id only fills in the '3' case.
#
# '2' is read as LRT rather than generic rail because in THIS feed the only
# route_type=2 rows are the two Cairo LRT branches. If Egyptian National
# Railways is ever ingested it will be route_type 2 as well, and this
# mapping will then have to consult agency_id to tell the two apart.
ROUTE_TYPE_VEHICLE_TYPES = {
    "0": "tram",
    "1": "metro",
    "2": "lrt",
    "12": "monorail",
}

# Agencies whose vehicle type must win over route_type: the airport shuttle
# is route_type 12 (monorail) like the East Nile line, but it is a free
# 4-stop inter-terminal people mover -- nothing about how it is fared,
# timetabled or presented to a rider is the same.
RAIL_AGENCY_OVERRIDES = {"apm"}

# Gated rail: boarding means entering a station through a barrier rather
# than flagging down a kerbside vehicle, so these share the metro's
# boarding-transfer treatment in the search.
GATED_RAIL_VTYPES = {"metro", "monorail", "lrt", "apm", "tram"}

# Everything a rider would call "a train" -- drives the rail planner and the
# Lines browser's mode filter. Currently identical to GATED_RAIL_VTYPES, but
# they answer different questions and will diverge the moment a non-gated
# rail mode (an open-platform tram, a commuter line with on-board fares)
# is added, so they are kept apart rather than aliased.
RAIL_VTYPES = {"metro", "monorail", "lrt", "apm", "tram"}

# Speed ceiling for grade-separated rail, used by the implausible-timing
# guard instead of the road-tuned one. 28 m/s = 100 km/h: the Cairo LRT's
# design speed, comfortably above the monorail's 80 km/h. See the long note
# at the guard itself for why rail needs a number of its own.
RAIL_MAX_SPEED_MPS = 28


# Agency id marking routes rebuilt from published corridor documents rather
# than surveyed on the ground. See reconstruct.py / build_gov_gtfs.py.
#
# Merging these without honouring the distinction measurably HARMS routing:
# a 12-trip benchmark on 2026-09-09 came back 6 worse / 3 better / 3 unchanged
# with zero of the new routes actually chosen. The cause was the cross-route
# median pass below -- 5,972 synthetic stop_times, all generated at one
# uniform speed, moved the medians that SURVEYED routes are corrected against.
# One id per mode so resolve_vehicle_type() below still returns minibus for a
# minibus; a single shared id would silently turn all of them into buses.
RECONSTRUCTED_AGENCY_IDS = frozenset({'GOV_CTA', 'GOV_CTA_M'})


def resolve_vehicle_type(route_type, agency_id):
    """Vehicle type for a route -- see the two tables above for the order."""
    agency_vtype = AGENCY_VEHICLE_TYPES.get(agency_id)
    if agency_vtype in RAIL_AGENCY_OVERRIDES:
        return agency_vtype
    return (ROUTE_TYPE_VEHICLE_TYPES.get(str(route_type).strip())
            or agency_vtype or "bus")

# Rough per-boarding fare estimates in EGP. There's no fare data at all in
# this GTFS feed (no fare_attributes.txt/fare_rules.txt), so these are
# best-effort heuristics, NOT sourced from an authoritative live fare
# table. Flag these as estimates in the UI and revisit once real fare
# data is available.
FLAT_FARE_BY_VEHICLE = {
    # PROVENANCE MATTERS HERE -- this table has now gone stale twice, and
    # the second correction came from published fare announcements that
    # turned out NOT to match what riders actually pay.
    #
    # Published March 2026 Cairo Governorate figures (elwatannews
    # /news/details/8242721 and elbalad.news/6897307, two outlets agreeing
    # on pre- and post-increase values) gave 13 EGP regular bus / 19 EGP
    # regular minibus, and those were used briefly on 2026-08-22. The
    # project owner -- who actually rides this network in Cairo -- then
    # corrected the bus figure to 20 EGP as the CHEAPEST real bus ticket.
    # A rider's direct knowledge outranks a tariff announcement: the
    # published number is the mandated rate for a specific vehicle
    # category, while a real passenger pays whatever the routes in this
    # feed actually charge. When these disagree, trust the rider.
    "bus": 20,       # 10 -> 13 -> 20. Cheapest real bus ticket, per the
                      # project owner (2026-08-22). Note this is ABOVE the
                      # published 13 EGP regular-bus tariff, which is a
                      # reminder not to "correct" it back toward a news
                      # figure without asking someone who rides it.
    "minibus": 20,   # 7 -> 14 -> 19 -> 20. Raised alongside bus rather
                      # than left at the published 19: leaving it lower
                      # would have implied minibus is cheaper than bus,
                      # which nothing in the correction above supports.
                      # CONFIRM WITH A RIDER -- this one is inferred, not
                      # directly reported, unlike the bus figure.
    # Every banded mode contributes 0 per boarding for the same reason the
    # metro does -- its fare is added once per journey in _build_option_data
    # from BANDED_FARE_TIERS, not per leg. Leaving them out of this table
    # entirely would have fallen through to the `10` default in
    # _estimate_leg_fare and charged a phantom 10 EGP on top.
    "monorail": 0,
    "lrt": 0,
    "apm": 0,
    "tram": 0,
    "metro": 0,  # metro legs contribute 0 here; the real fare is added once per
                 # journey via the stop-count tiers below (10/12/15/20 EGP), not
                 # per boarding, since Cairo Metro fare is distance/stop-based
                 # rather than a flat per-ride fee like bus/minibus. That
                 # journey-level fare IS attached to the first metro
                 # boarding instruction (see _build_option_data) so the
                 # per-step fares a rider sees still add up to the total.
}
# APPROXIMATION BY NECESSITY -- and the weakest numbers in this file.
#
# Searched properly on 2026-08-22 for an authoritative 2026 per-km
# microbus rate. There isn't one, and that's structural rather than a
# gap in the search: Cairo/Giza set microbus ("سيارات السرفيس") fares as a
# PER-ROUTE TABLE, published as banners and stickers posted at each
# station, not as a distance formula. Governorate announcements after a
# fuel increase give only a percentage uplift (e.g. "10-15% across
# routes", "Giza +17%") and explicitly say the per-route figures account
# for route length AND trip frequency together. So there is no published
# rate this model could be reconciled against -- only a few hundred
# individual route prices that would have to be scraped and mapped onto
# feed route_ids.
#
# The distance model below is therefore a stand-in for that table. The
# project owner (who rides this network) reviewed it on 2026-08-22 and
# called it "roughly right", which is the best validation available
# short of that scraping exercise.
#
# The minimum was raised 5 -> 10 to stop it undercutting the CHEAPEST
# METRO TICKET (10 EGP, confirmed by the project owner): at 5 EGP the
# router believed a microbus was always cheaper than any metro ride,
# which is implausible for any real trip and biased the "cheapest"
# profile toward microbus legs.
MICROBUS_FARE_PER_KM = 1.25
MICROBUS_MIN_FARE = 10

# Cairo Metro fare tiers as (max_HOPS_inclusive, fare_egp), open-ended at
# the end. Note HOPS, not stations -- that distinction is the whole
# reason the numbers below look off-by-one against every news report.
#
# There are FOUR tiers, not three. Re-verified 2026-08-22 against two
# independent reports of the March 2026 increase (dailynewsegypt.com
# /2026/03/26/... and elbalad.news/6993921), which agree exactly:
#   up to  9 stations -> 10 EGP   (was 8)
#   up to 16 stations -> 12 EGP   (was 10)
#   up to 23 stations -> 15 EGP   (unchanged)
#   24-39   stations -> 20 EGP    (unchanged)
# The 20 EGP tier is easy to miss in practice -- it needs a >23-station
# journey, i.e. something like the full length of Line 1 (Helwan -> New
# El-Marg) -- which is why it can look like the system only has 10/12/15.
#
# The boundaries here (8/15/22) are NOT a disagreement with those
# announcements: they are the same thresholds counted in hops. A trip
# "through 9 stations" is 8 hops between them, "16 stations" is 15 hops,
# "23 stations" is 22 hops. self.metro_stops counts graph edges, so the
# hop form is what this table must be expressed in. Independently
# corroborated: an earlier session derived exactly 8/15/22 empirically
# from Transport for Cairo's own fare_rules.txt matrix (6,767 real
# origin-destination fare lookups, Oct 2024-2025 feed) before the
# announcement was consulted at all -- two routes to the same numbers.
#
# So: do NOT "fix" these to 9/16/23 to match a news article. That would
# overcharge every trip sitting exactly on a boundary.
METRO_FARE_TIERS = [(8, 10), (15, 12), (22, 15), (None, 20)]

# The metro is no longer the only mode fared this way. The monorail and the
# LRT are both banded on distance travelled too, and -- this is the part
# that matters -- they are SEPARATE TICKETS. A rider going Nasr City ->
# New Capital by metro to Adly Mansour, LRT to Arts & Culture City, then
# monorail, buys three tickets, not one. So hops are counted per mode and
# each mode's band is charged once, rather than pooling them into a single
# journey total the way a single-operator network would.
#
# Bands are in HOPS, matching METRO_FARE_TIERS -- an operator advertising
# "up to 5 stations" means 5 hops from where you got on, which is a 6-station
# span. Getting this off by one overcharges every trip sitting on a boundary.
BANDED_FARE_TIERS = {
    # Cairo Metro. See the long note above; do not "correct" to 9/16/23.
    "metro": METRO_FARE_TIERS,
    # East Nile Monorail: 20 / 40 / 55 / 80 EGP announced at launch for
    # up to 5, 10, 15 stations and the full line (Egyptian Streets,
    # 9 May 2026). Same hops-not-stations reading as the metro.
    "monorail": [(5, 20), (10, 40), (15, 55), (None, 80)],
    # Cairo LRT: 10 / 15 / 20 EGP. The published figures for this line
    # disagree between sources -- a July 2022 report gives 15/20/25/35 for
    # 3/5/7/9 stations, a later one gives 10 up to 3 stations and 20 beyond
    # 7 -- and the March 2026 nationwide adjustment landed between the two.
    # The later, cheaper reading is used because it is the more recent
    # publication and because understating a fare is the safer error for a
    # rider standing at a ticket window. FLAGGED AS UNVERIFIED: worth
    # confirming against a real ticket before the thesis defence.
    "lrt": [(3, 10), (7, 15), (None, 20)],
    # Free inter-terminal shuttle. Present as an explicit band rather than
    # an omission so it reads as "checked, costs nothing" rather than
    # "nobody got round to it".
    "apm": [(None, 0)],
}


def banded_journey_fare(vehicle_type, hops):
    """Fare in EGP for `hops` hops ridden on one banded mode, charged once."""
    tiers = BANDED_FARE_TIERS.get(vehicle_type)
    if not tiers or hops <= 0:
        return 0
    for max_hops, fare in tiers:
        if max_hops is None or hops <= max_hops:
            return fare
    return tiers[-1][1]


def metro_journey_fare(metro_stops):
    """Journey-level Cairo Metro fare for a given number of metro hops.
    Charged once per journey no matter how many lines are changed.

    Kept as a named function because main.py and the test suite import it
    directly; new code should call banded_journey_fare("metro", hops)."""
    return banded_journey_fare("metro", metro_stops)


# Typical microbus fare used ONLY as a search-time cost estimate for the
# "cheapest" profile -- the real per-leg figure is distance-based (see
# _estimate_leg_fare). Kept deliberately mid-range rather than at
# MICROBUS_MIN_FARE, since the search doesn't know the leg's length until
# after the path is chosen.
MICROBUS_TYPICAL_FARE_EGP = 14


class GTFSRaptorEngine:
    def __init__(self, gtfs_folder_path, use_osrm=True, osrm_base_url=None):
        self.folder = gtfs_folder_path
        self.stops = {}
        self.stop_alias = {}   # non-canonical stop_id -> canonical stop_id (see _build_stop_clusters)
        self.routes = {}       # route_id -> {short_name, long_name, type, agency_id, vehicle_type}
        # Populated from agency_id -- see RECONSTRUCTED_AGENCY_ID.
        self.reconstructed_route_ids = set()
        self.metro_route_ids = set()  # precomputed at load time to avoid repeated dict.get() calls in Dijkstra's hot loop
        self.rail_route_ids = set()   # metro + monorail + LRT + airport shuttle
        self.banded_route_ids = {}    # vehicle_type -> {route_id}, for per-mode journey fares
        self.trips = {}
        self.trip_direction = {}  # trip_id -> direction_id string ('' if unknown)
        self.stop_times = defaultdict(list)
        self.graph = defaultdict(list)

        # (route_id, direction_id) -> ordered, de-duplicated list of
        # canonical stop_ids -- the longest single-trip stop sequence
        # observed for that route+direction, used as a representative
        # picture of "everywhere this route goes, in order". Built in
        # load_data() and consumed by _find_equivalent_routes() to
        # detect when a DIFFERENT route also serves a rider's exact
        # board->alight pair (in the same order), i.e. is a genuine
        # substitute the rider could take instead.
        self.route_stop_sequence = {}

        # (route_id, direction_id) -> typical minutes between vehicles,
        # derived from frequencies.txt (duration-weighted average across
        # that route's headway blocks -- see load_data()). This is the
        # cold-start fallback for _find_equivalent_routes(): when nobody
        # is currently live-tracked on a route, "no live ETA, but this
        # route runs about every N min" is a far more useful signal to
        # show a rider than a bare "no data".
        self.route_typical_headway_min = {}
        # route_id -> [(start_sec, end_sec), ...] merged service windows
        # from frequencies.txt. Empty for routes the feed gives no hours
        # for, which are treated as always running (see
        # _routes_not_running_at).
        self.route_service_windows = {}
        self._service_window_cache = {}
        self._route_running_cache = {}
        # Real vehicle crossings of the Zamalek/Gezira island boundary,
        # derived from shapes.txt at load time -- see
        # _derive_island_bridges.
        self.island_bridges = []
        # route_id -> vehicle_type, precomputed so the search's hot loop
        # does one dict get instead of two (see _find_shortest_path).
        self._route_vtype = {}

        # In-memory store of riders' own live GPS while actively riding a
        # leg (see live_tracking.py's module docstring for the full
        # rationale) -- there is no dedicated vehicle-tracking hardware
        # feed in this system, so this crowdsourced stream is the only
        # source of real-time vehicle position, and it's what lets
        # _find_equivalent_routes() attach a real-time ETA to each
        # interchangeable route instead of just listing them blind.
        self.live_store = LivePositionStore()

        # Arabic translations sourced from the GTFS feed's own
        # translations.txt (real, professionally-translated names), keyed
        # by the original English value -> Arabic value. This is what
        # actually fixes turn-by-turn text staying in English when Arabic
        # is selected: station/route names are now genuinely translated
        # at the source instead of guessed at via client-side pattern
        # matching against English sentences.
        self.stop_name_ar = {}
        self.route_long_name_ar = {}
        self.route_short_name_ar = {}

        # Fastest implied STRAIGHT-LINE speed a surface transit edge may
        # claim before its recorded time is discarded as a data error.
        # 25 m/s (90 km/h) divided by this feed's measured 1.63x median
        # road circuity -- see the guard in load_data() for the full
        # derivation and the before/after verification against Google.
        self.MAX_IMPLIED_STRAIGHT_LINE_SPEED_MPS = 15.3
        self.WALK_LINK_RADIUS_M = 500  # was 350; too tight given how sparse this network is
        self.WALK_SPEED_MPS = 1.25
        self.NEAREST_STOP_CANDIDATES = 6
        self.DIRECT_WALK_MAX_M = 3000
        self.MAX_SNAP_DISTANCE_M = 5000  # beyond this, don't snap at all -- see find_nearest_stop

        # --- Sanity limits on a walk-only recommendation -------------------
        # DIRECT_WALK_MAX_M is a STRAIGHT-LINE test, but what the rider
        # actually walks is OSRM's path, and those two can diverge wildly
        # where the OSM pedestrian network is incomplete. Verified case
        # (2026-08-23): Koshary Tawagen Salsa -> AASTMT Sheraton is 3367 m
        # straight line and 4.1 km on Google, but our own OSRM answers
        # 9285.4 m / 6685 s -- it walks NORTH up El-Saaqah past the
        # destination's latitude to ~30.112 before doubling back down
        # Abdel Hamid Badawi, because no pedestrian crossing of Tareeq
        # El-Nasr is mapped anywhere nearer. The app duly showed the rider
        # "Walk 9285m to your destination".
        #
        # We cannot fix the OSM extract before release, and we must not
        # invent a shorter path we can't draw, so the rule is: judge the
        # walk on the number the rider would actually see, and if that
        # number is not credible, don't offer walking at all. Silence
        # beats a confident 9 km lie.
        self.WALK_ONLY_MAX_REAL_M = 4500        # absolute ceiling on a recommended walk
        self.WALK_ONLY_MAX_CIRCUITY = 1.6       # OSRM path length vs straight line
        self.WALK_CIRCUITY_SLACK_M = 400        # short trips legitimately round a block

        # --- Waiting for the vehicle --------------------------------------
        # frequencies.txt gives a real headway for 1760 route/directions
        # (median 16.3 min, and 396 of them worse than 30 min), but until
        # 2026-08-23 that number was only ever surfaced to the app as
        # `typical_headway_min` -- it never entered the routing cost or the
        # time we quoted. We were pricing pure riding time.
        #
        # Confirmed against Google on Koshary Tawagen Salsa -> AASTMT:
        # Guidy said 41 min, Google's five options ran 69-91 min, and
        # Google's own itinerary showed a 2:04 departure boarding at 2:24.
        # Adding expected wait puts us at ~63 min, inside Google's range.
        #
        # Expected wait for a uniformly-arriving rider is half the headway.
        # It is capped because the model stops being true at the tail: at a
        # 90-minute headway nobody stands at the curb for 45 minutes, they
        # find another way, so charging the full half-headway would let one
        # bad route poison an otherwise sane itinerary.
        self.FALLBACK_HEADWAY_MIN = 20.0     # no frequencies.txt entry for this route/direction
        self.EXPECTED_WAIT_CAP_SEC = 20 * 60

        # Service calendar (calendar.txt): which days of the week each
        # route actually runs -- some routes skip Friday/Saturday
        # (Egypt's weekend). Verified this feed has exactly 1 service
        # pattern per route (no route mixes service_ids across its
        # trips), so filtering at the route level is exact here, not an
        # approximation. See _inactive_routes_for_day().
        self.service_days = {}       # service_id -> set of day-name strings (lowercase)
        self.route_service_ids = defaultdict(set)  # route_id -> set of service_ids used by its trips
        # today_day -> frozenset of route_ids inactive that day, computed
        # once per day rather than checked per-edge in Dijkstra's hot
        # loop -- see _inactive_routes_for_day().
        self._route_active_cache = {}

        # Real street-following route geometry (shapes.txt), used to draw
        # segments that actually trace streets between stops instead of
        # straight lines. One representative shape per route (first trip
        # seen for that route "wins") -- see load_data() and
        # _shape_segment().
        self.shapes = {}             # shape_id -> ordered list of (lat, lon)
        self.route_shape = {}        # route_id -> shape_id (representative fallback)
        self.route_direction_shape = {}  # (route_id, direction_id) -> shape_id
        # Caches the nearest-shape-point lookup per (shape_id, stop_id),
        # since the Fastest/Regular/Cheapest profiles frequently share the
        # same hops -- without this, the same linear scan over ~300 shape
        # points was getting redone 3x per request, and again on every
        # subsequent request through the same stop. See _shape_segment().
        self._shape_index_cache = {}

        # Real street-network walking directions via a self-hosted OSRM
        # instance, instead of straight-line haversine estimates. Falls
        # back to haversine automatically if OSRM isn't reachable -- see
        # osrm_client.py for setup instructions.
        osrm_kwargs = {"enabled": use_osrm}
        if osrm_base_url:
            osrm_kwargs["base_url"] = osrm_base_url
        self.osrm = OSRMClient(**osrm_kwargs)

    def _haversine(self, lat1, lon1, lat2, lon2):
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = math.sin(delta_phi / 2.0) ** 2 + \
            math.cos(phi1) * math.cos(phi2) * \
            math.sin(delta_lambda / 2.0) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    @staticmethod
    def _parse_gtfs_time(t):
        h, m, s = (int(x) for x in t.strip().split(':'))
        return h * 3600 + m * 60 + s

    def _build_stop_clusters(self):
        """
        Groups stop_ids that share an identical name and are within a
        small radius of each other into one logical hub, mapped through
        self.stop_alias (non-canonical -> canonical stop_id).

        Real-world paratransit GTFS data often has multiple separate
        stop_ids for what's physically the same interchange -- different
        operators or platforms digitized independently. Confirmed this
        directly on this feed: "Suez Bridge Entrance to Ring Road" alone
        has 6 different stop_ids all within ~130m of each other. Without
        merging these, the router can "transfer" between them (paying a
        fresh transfer penalty each time) for zero real progress, which
        was traced as the actual cause of routes with far more boardings
        than actually necessary.
        """
        CLUSTER_RADIUS_M = 150
        by_name = defaultdict(list)
        for stop_id, info in self.stops.items():
            by_name[info['name']].append(stop_id)

        clustered_count = 0
        for name, stop_ids in by_name.items():
            if len(stop_ids) < 2:
                continue
            clusters = []  # list of lists of stop_id, grouped by proximity
            for sid in stop_ids:
                placed = False
                for cluster in clusters:
                    rep = cluster[0]
                    d = self._haversine(
                        self.stops[sid]['lat'], self.stops[sid]['lon'],
                        self.stops[rep]['lat'], self.stops[rep]['lon']
                    )
                    if d <= CLUSTER_RADIUS_M:
                        cluster.append(sid)
                        placed = True
                        break
                if not placed:
                    clusters.append([sid])

            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                canonical = cluster[0]
                for sid in cluster[1:]:
                    self.stop_alias[sid] = canonical
                    clustered_count += 1

        if clustered_count:
            print(f"Clustered {clustered_count} duplicate-hub stop_ids into their canonical stop.")

    def load_data(self):
        print("Ingesting GTFS Data & Building Network Graph...")
        start_time = time.time()

        with open(f"{self.folder}/stops.txt", 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.stops[row['stop_id']] = {
                    'name': row['stop_name'],
                    'lat': float(row['stop_lat']),
                    'lon': float(row['stop_lon'])
                }

        self._build_stop_clusters()

        with open(f"{self.folder}/routes.txt", 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                agency_id = row.get('agency_id', '')
                route_type = str(row['route_type']).strip()
                self.routes[row['route_id']] = {
                    'short_name': row.get('route_short_name', ''),
                    'long_name': row.get('route_long_name', ''),
                    'type': route_type,
                    'agency_id': agency_id,
                    'vehicle_type': resolve_vehicle_type(route_type, agency_id),
                }
                # Routes reconstructed from the Cairo Governorate corridor
                # documents rather than surveyed. Their STOP SEQUENCES are
                # real -- every consecutive pair is a hop some surveyed
                # route already makes -- but every TIME in them is an
                # estimate, because the source documents contain no
                # timetable at all. That distinction has to survive into
                # the edge-statistics passes below: a guess may be
                # corrected by real data, and must never correct it.
                if agency_id in RECONSTRUCTED_AGENCY_IDS:
                    self.reconstructed_route_ids.add(row['route_id'])
                if route_type == '1':
                    self.metro_route_ids.add(row['route_id'])
                # Every mode whose fare is banded per journey rather than
                # charged per boarding. Kept as one set per mode so the
                # fare pass can count hops separately -- see the note on
                # BANDED_FARE_TIERS about these being separate tickets.
                vt = self.routes[row['route_id']]['vehicle_type']
                if vt in BANDED_FARE_TIERS:
                    self.banded_route_ids.setdefault(vt, set()).add(row['route_id'])
                # Everything a rider would call "a train": used by the
                # lookups module's rail planner and by the Lines browser's
                # mode filter. Superset of metro_route_ids.
                if vt in RAIL_VTYPES:
                    self.rail_route_ids.add(row['route_id'])

        with open(f"{self.folder}/trips.txt", 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                trip_id = row['trip_id']
                route_id = row['route_id']
                self.trips[trip_id] = route_id
                self.trip_direction[trip_id] = (row.get('direction_id') or '').strip()
                service_id = row.get('service_id')
                if service_id:
                    self.route_service_ids[route_id].add(service_id)
                shape_id = (row.get('shape_id') or '').strip()
                if shape_id:
                    dir_id = (row.get('direction_id') or '').strip()
                    if (route_id, dir_id) not in self.route_direction_shape:
                        self.route_direction_shape[(route_id, dir_id)] = shape_id
                    if route_id not in self.route_shape:
                        self.route_shape[route_id] = shape_id

        with open(f"{self.folder}/stop_times.txt", 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                trip_id = row['trip_id']
                stop_id = self.stop_alias.get(row['stop_id'], row['stop_id'])
                seq = int(row['stop_sequence'])
                arr = row.get('arrival_time', '').strip()
                dep = row.get('departure_time', '').strip()
                self.stop_times[trip_id].append((stop_id, seq, arr, dep))

        # frequencies.txt (trip_id, start_time, end_time, headway_secs):
        # this feed represents most routes as exactly ONE trip per
        # (route_id, direction_id) rather than many trips spread across
        # the day (confirmed: 1,792 trips total for 1,792 stop_times
        # entries -- effectively 1-2 trips per route). That means "gap
        # between consecutive trip start times" -- the usual way to infer
        # a route's frequency from a timetable -- is not computable here;
        # frequencies.txt is this feed's actual encoding of how often a
        # route really runs, and until now nothing in this engine read it
        # (see the 2026-08-21 routing-engine-debug doc's noted
        # limitation). Parsed here into trip_frequency_blocks so
        # route_typical_headway_min below has real data to build from --
        # this is the first use of frequencies.txt in the codebase.
        trip_frequency_blocks = defaultdict(list)  # trip_id -> [(start_sec, end_sec, headway_sec), ...]
        try:
            with open(f"{self.folder}/frequencies.txt", 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        start_sec = self._parse_gtfs_time(row['start_time'])
                        end_sec = self._parse_gtfs_time(row['end_time'])
                        headway_sec = float(row['headway_secs'])
                    except (ValueError, KeyError, AttributeError):
                        continue
                    if end_sec > start_sec and headway_sec > 0:
                        trip_frequency_blocks[row['trip_id']].append((start_sec, end_sec, headway_sec))
        except FileNotFoundError:
            print("No frequencies.txt found -- no schedule-derived headway fallback for the live-tracking feature.")

        # Real Arabic names from the feed's own translations.txt, if
        # present. Not every GTFS feed includes this file, so this is
        # optional -- the app falls back to English-only if it's missing.
        try:
            with open(f"{self.folder}/translations.txt", 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('language') != 'ar':
                        continue
                    table, field = row.get('table_name'), row.get('field_name')
                    en_val, ar_val = row.get('field_value'), row.get('translation')
                    if not en_val or not ar_val:
                        continue
                    if table == 'stops' and field == 'stop_name':
                        self.stop_name_ar[en_val] = ar_val
                    elif table == 'routes' and field == 'route_long_name':
                        self.route_long_name_ar[en_val] = ar_val
                    elif table == 'routes' and field == 'route_short_name':
                        self.route_short_name_ar[en_val] = ar_val
            print(f"Loaded Arabic translations: {len(self.stop_name_ar)} stops, {len(self.route_long_name_ar)} route names.")
        except FileNotFoundError:
            print("No translations.txt found -- Arabic will fall back to English stop/route names.")

        # Service calendar: which days of the week each service_id runs.
        # Optional -- if missing, every route is treated as always active
        # (the engine's prior behavior), rather than breaking routing.
        DAY_COLUMNS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        try:
            with open(f"{self.folder}/calendar.txt", 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                today_str = datetime.date.today().strftime('%Y%m%d')
                any_active_today_by_date = False
                for row in reader:
                    service_id = row['service_id']
                    self.service_days[service_id] = {day for day in DAY_COLUMNS if row.get(day) == '1'}
                    start_date, end_date = row.get('start_date', ''), row.get('end_date', '')
                    if start_date and end_date and start_date <= today_str <= end_date:
                        any_active_today_by_date = True
                if self.service_days and not any_active_today_by_date:
                    # The feed's declared validity window (start_date/
                    # end_date) has lapsed relative to today -- this is
                    # common in feeds that aren't kept perfectly current,
                    # and doesn't mean real-world service actually
                    # stopped. So this filters by DAY OF WEEK only, and
                    # deliberately does NOT enforce start_date/end_date --
                    # just flagging it so it's visible if the feed is
                    # stale enough that this matters (e.g. a real holiday
                    # calendar change).
                    print(f"NOTE: calendar.txt's declared date range doesn't cover today ({today_str}). "
                          f"Routing still uses day-of-week service patterns; consider refreshing the GTFS feed.")
            print(f"Loaded service calendar: {len(self.service_days)} service pattern(s).")
        except FileNotFoundError:
            print("No calendar.txt found -- all routes treated as running every day.")

        # Real route geometry, optional -- falls back to straight
        # stop-to-stop lines (the engine's prior behavior) if missing.
        try:
            shape_points_raw = defaultdict(list)
            with open(f"{self.folder}/shapes.txt", 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    shape_points_raw[row['shape_id']].append((
                        int(row['shape_pt_sequence']),
                        float(row['shape_pt_lat']),
                        float(row['shape_pt_lon']),
                    ))
            for shape_id, points in shape_points_raw.items():
                points.sort(key=lambda p: p[0])
                self.shapes[shape_id] = [(lat, lon) for _, lat, lon in points]
            print(f"Loaded {len(self.shapes)} route shapes ({sum(len(p) for p in self.shapes.values())} points total).")
        except FileNotFoundError:
            print("No shapes.txt found -- route segments will draw as straight lines between stops.")

        self._route_vtype = {rid: info.get('vehicle_type') for rid, info in self.routes.items()}
        # One bit per banded-fare mode, for the search's "have I already
        # bought a ticket for this mode" state. Sorted so the bit assignment
        # is stable across runs -- an unstable mapping would make the hot
        # loop's behaviour depend on dict iteration order.
        self._banded_bit = {vt: 1 << i
                            for i, vt in enumerate(sorted(BANDED_FARE_TIERS))}

        self._derive_island_bridges()

        # Edges are keyed by (u, v, route_id, direction_id) -- NOT just
        # (u, v, route_id). GTFS feeds routinely run both directions of a
        # route (outbound + return) under one shared route_id, and this
        # feed does it for the large majority of routes (773 of 1014 have
        # both direction_id 0 and 1 trips on the same route_id). Keying
        # edges by route_id alone -- and treating "same route_id" as "same
        # continuous vehicle run, no transfer needed" -- lets the search
        # stitch an outbound trip's edges to a return trip's edges through
        # a shared/clustered stop, producing a graph cycle that requires
        # getting off one vehicle and boarding the physically opposite one
        # as if it were a single uninterrupted ride, with no transfer
        # penalty and no wait. Confirmed concretely on route
        # p3-8qu3UivDvTp0cckEVG: its two direction trips stitch into a
        # closed loop 394 -> 2526 -> 2169 -> 394, entirely on one
        # route_id. direction_id is threaded through the graph (and
        # path-finding / display below) so a same-route_id-but-opposite-
        # direction hop is correctly treated as a real transfer.
        #
        # Multiple trips can also cover the same (u, v, route_id,
        # direction_id) hop at different times of day. Rather than keeping
        # whichever trip happened to be encountered first while iterating
        # self.stop_times (effectively an arbitrary pick), every plausible
        # sample is collected and the edge weight is their mean -- a more
        # representative single travel time for that hop.
        edge_samples = defaultdict(list)  # (u, v, route_id, direction_id) -> [time_sec, ...]
        implausible_guarded = 0
        slow_outlier_guarded = 0
        for trip_id, st_list in self.stop_times.items():
            route_id = self.trips.get(trip_id)
            if not route_id: continue
            direction_id = self.trip_direction.get(trip_id, '')
            st_list.sort(key=lambda x: x[1])

            # Keep the LONGEST stop sequence seen for this (route_id,
            # direction_id) as the representative "everywhere this route
            # goes, in order" -- a shorter trip on the same route
            # (short-turn, skips the far end at off-peak) would otherwise
            # make _find_equivalent_routes() miss legitimate equivalences
            # against its longer siblings.
            seq_key = (route_id, direction_id)
            ordered_stops = []
            for stop_id, _, _, _ in st_list:
                canonical = self.stop_alias.get(stop_id, stop_id)
                if not ordered_stops or ordered_stops[-1] != canonical:
                    ordered_stops.append(canonical)
            if len(ordered_stops) > len(self.route_stop_sequence.get(seq_key, [])):
                self.route_stop_sequence[seq_key] = ordered_stops

            # First pass over this trip's hops: compute distance + timed
            # duration for each, and this trip's own overall pace
            # (total distance / total time, over hops with usable
            # timing). Used below to catch a hop that's a wild outlier
            # relative to how fast THIS SAME vehicle otherwise moves --
            # a real, confirmed problem in this feed: one Minibus 66 trip
            # covers most of its hops at 15-25+ km/h but has a single
            # 797m hop timed at 352s (8 km/h, over 3x slower than its own
            # median pace), which is far more likely a bad timepoint in
            # that one row than an actual traffic crawl. This is checked
            # per-trip against the trip's own pace, not against other
            # routes -- comparing across routes (tried and reverted, see
            # below) touched thousands of unrelated edges feed-wide since
            # most routes here have only 1-2 trips, so cross-route
            # comparisons were really just comparing single noisy samples
            # against other single noisy samples.
            hops = []
            for i in range(len(st_list) - 1):
                u, _, _, u_dep = st_list[i]
                v, _, v_arr, _ = st_list[i + 1]
                if u not in self.stops or v not in self.stops: continue
                if u == v: continue

                dist_m = self._haversine(
                    self.stops[u]['lat'], self.stops[u]['lon'],
                    self.stops[v]['lat'], self.stops[v]['lon']
                )

                time_sec = None
                try:
                    t_dep = self._parse_gtfs_time(u_dep)
                    t_arr = self._parse_gtfs_time(v_arr)
                    candidate = t_arr - t_dep
                    # Data-quality guard: reject non-positive durations, or
                    # implied speeds a bus/microbus in Cairo traffic could
                    # never actually reach between two stops. These come
                    # from real data-entry inconsistencies in crowdsourced
                    # GTFS feeds and otherwise create artificially "fast"
                    # edges that bias the router toward nonsensical paths.
                    #
                    # THE THRESHOLD IS AGAINST STRAIGHT-LINE DISTANCE, and
                    # that used to make this guard almost inert. It read
                    # `<= 25` with a comment saying "25 m/s = 90 km/h" --
                    # but dist_m here is haversine, and this feed's own
                    # audit measured real road distance at 1.63x haversine
                    # at the median (see claude/gtfs-sanity-check). So 25
                    # m/s straight-line was really tolerating ~147 km/h on
                    # the road, and caught only 1.7% of surface edges.
                    #
                    # 15.3 = 25 / 1.63 makes the guard enforce what its
                    # comment always claimed: ~90 km/h on real roads. It
                    # now catches 6.4% of surface edges (2850 vs 133).
                    #
                    # Verified against real Google transit times before
                    # landing, on 2026-08-23:
                    #   Shorouq -> Dokki:      Guidy 73 -> 117 min (Google 102-127)
                    #   6th October -> Downtown: Guidy 67 -> 74 min (Google 93-107)
                    # i.e. it corrects the systematic optimism the
                    # route-comparison audit found (Guidy was running
                    # ~0.7x of Google's times feed-wide). Metro is
                    # untouched: the fastest metro edge in this feed
                    # implies 13.9 m/s, comfortably under the threshold.
                    #
                    # GRADE-SEPARATED RAIL GETS ITS OWN CEILING, because
                    # the 1.63x circuity divisor above is a property of
                    # ROADS. A monorail beam and an LRT viaduct run
                    # essentially straight between stations, so their real
                    # path length is close to haversine and dividing their
                    # top speed by 1.63 is a correction for a detour they
                    # never take. Applied to them, 15.3 m/s rejected the
                    # LRT's genuine timings on its long desert hops
                    # (Adly Mansour to El-Obour is 6km) and replaced them
                    # with the 20 km/h urban-surface fallback below --
                    # which quoted a 9-stop LRT ride at 172 minutes
                    # against a published end-to-end time of 60.
                    #
                    # 28 m/s is 100 km/h, the Cairo LRT's design speed and
                    # above the monorail's 80 km/h, so it still catches
                    # genuinely impossible values without clipping real ones.
                    speed_cap = (RAIL_MAX_SPEED_MPS
                                 if self.routes.get(route_id, {}).get('vehicle_type') in RAIL_VTYPES
                                 else self.MAX_IMPLIED_STRAIGHT_LINE_SPEED_MPS)
                    if candidate > 0 and (dist_m / candidate) <= speed_cap:
                        time_sec = candidate
                    else:
                        implausible_guarded += 1
                except (ValueError, AttributeError):
                    pass

                hops.append([u, v, dist_m, time_sec])

            timed_dist = sum(h[2] for h in hops if h[3] is not None)
            timed_time = sum(h[3] for h in hops if h[3] is not None)
            trip_speed_mps = (timed_dist / timed_time) if timed_time > 0 else None

            for u, v, dist_m, time_sec in hops:
                if time_sec is not None and trip_speed_mps and dist_m > 0:
                    implied_speed = dist_m / time_sec
                    # Under a third of this trip's own overall pace, and
                    # the hop is at least 90s (skip short hops -- normal
                    # dwell/signal variance at a stop easily swings a
                    # 20-30s hop by 3x without meaning anything).
                    if implied_speed < trip_speed_mps / 3 and time_sec >= 90:
                        time_sec = dist_m / trip_speed_mps
                        slow_outlier_guarded += 1

                if time_sec is None:
                    # Fallback: assume ~20 km/h average urban surface-transit
                    # speed (accounts for traffic/stops), not a bare floor.
                    # Rail gets 45 km/h instead -- it does not sit in the
                    # traffic that 20 km/h is accounting for, and quoting a
                    # monorail at bus pace would send riders onto the road
                    # network to "save" time they would actually lose.
                    fallback_kmh = (45 if self.routes.get(route_id, {}).get(
                        'vehicle_type') in RAIL_VTYPES else 20)
                    time_sec = max(dist_m / (fallback_kmh * 1000 / 3600), 10)

                edge_key = (u, v, route_id, direction_id)
                edge_samples[edge_key].append(time_sec)

        # Typical headway per (route_id, direction_id), from
        # frequencies.txt's headway_secs blocks (see the parsing note
        # above for why this feed needs that file rather than inferring
        # frequency from trip-start-time gaps -- there usually aren't
        # enough trips per route to have gaps to measure). A route can
        # have several blocks across the day (rush hour vs. midday, etc,
        # each on its own representative trip_id) -- this collapses them
        # into one number via a duration-weighted average, which is a
        # deliberate simplification: it's meant as a rough "runs about
        # every N min" cold-start signal for _find_equivalent_routes(),
        # not a time-of-day-aware schedule lookup.
        headway_weighted_sum = defaultdict(float)   # (route_id, direction_id) -> sum(headway_sec * block_duration_sec)
        headway_total_duration = defaultdict(float)  # (route_id, direction_id) -> sum(block_duration_sec)
        for trip_id, blocks in trip_frequency_blocks.items():
            route_id = self.trips.get(trip_id)
            if not route_id:
                continue
            seq_key = (route_id, self.trip_direction.get(trip_id, ''))
            for start_sec, end_sec, headway_sec in blocks:
                duration = end_sec - start_sec
                headway_weighted_sum[seq_key] += headway_sec * duration
                headway_total_duration[seq_key] += duration
        for seq_key, total_duration in headway_total_duration.items():
            if total_duration <= 0:
                continue
            self.route_typical_headway_min[seq_key] = round(headway_weighted_sum[seq_key] / total_duration / 60, 1)

        # Service WINDOW per route, from the same frequencies.txt blocks.
        # Until 2026-08-22 this file's start_time/end_time were read only
        # to average headways, and the window itself was thrown away --
        # so the engine happily routed a rider onto an 06:00-23:00 bus at
        # 02:00 and quoted a two-hour trip for a journey whose first
        # vehicle was four hours away. Surfaced by the route-comparison
        # audit: run at 21:35 Cairo, Google priced the same trips at
        # 7-12 HOURS (it counts the overnight wait for first service)
        # while Guidy's numbers did not move at all between a midday and
        # a night run.
        #
        # Stored as a union of (start_sec, end_sec) blocks per route_id
        # rather than one min/max span, so a route that genuinely runs in
        # two separate windows (a morning and an evening block with a
        # midday gap) isn't silently treated as running straight through
        # the gap. Times come from _parse_gtfs_time, so GTFS's after-
        # midnight convention (24:xx, 25:xx for service belonging to the
        # previous service day) is preserved and handled at lookup.
        route_windows = defaultdict(list)
        for trip_id, blocks in trip_frequency_blocks.items():
            route_id = self.trips.get(trip_id)
            if not route_id:
                continue
            for start_sec, end_sec, _headway in blocks:
                route_windows[route_id].append((start_sec, end_sec))
        for route_id, blocks in route_windows.items():
            blocks.sort()
            merged = [list(blocks[0])]
            for s, e in blocks[1:]:
                if s <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            self.route_service_windows[route_id] = [tuple(b) for b in merged]
        if self.route_service_windows:
            spans = [w for ws in self.route_service_windows.values() for w in ws]
            earliest = min(s for s, _ in spans)
            latest = max(e for _, e in spans)
            print(f"Service windows: {len(self.route_service_windows)} routes have declared hours "
                  f"({earliest // 3600:02d}:{earliest % 3600 // 60:02d}-{latest // 3600:02d}:{latest % 3600 // 60:02d} overall).")

        # First pass: this route's own average time for each (u, v) hop it
        # serves, before any cross-route correction.
        pair_avg_time = {}  # (u, v, route_id, direction_id) -> avg_time
        for edge_key, samples in edge_samples.items():
            pair_avg_time[edge_key] = sum(samples) / len(samples)

        # --- Real-road-distance correction (ground truth, not a guess) ---
        # The haversine-based implausible-timing guard above catches edges
        # that are impossibly fast against STRAIGHT-LINE distance, but a
        # 2026-08-22 sanity check against real OSRM driving data (100 stop
        # pairs, see gtfs_sanity_check.xlsx) found real road distance runs
        # 1.63x haversine at the median in this feed, and up to 5.8x for
        # pairs needing a bridge/ring-road detour -- so a hop that needs a
        # real detour can look plausible against straight-line distance
        # while being physically impossible on the real street network.
        # That same check found ~1 in 5 sampled corridors had a route
        # recorded as FASTER than a car can drive the same real distance --
        # including cases where 20+ different route_ids all shared the
        # exact same (wrong) time, which the cross-route disagreement pass
        # below can't catch either, since routes that all agree with each
        # other never trip a disagreement check.
        #
        # osrm_edge_cache.json (built offline by build_osrm_cache.py, see
        # that file's docstring for why this isn't a live lookup at load
        # time) holds real driving duration/distance for stop pairs whose
        # GTFS-recorded time looked suspiciously fast. Where a cache entry
        # exists and the recorded time implies a REAL speed above what a
        # bus/minibus/microbus can plausibly sustain, floor it to the real
        # distance at that cap -- this is a hard floor, not a blend,
        # because unlike the cross-route disagreement below (where we
        # often can't tell which side is right) this is checked against
        # independently-verified ground truth, not another guess.
        # Gracefully absent if the cache hasn't been built yet -- same
        # fallback philosophy as OSRMClient for walking legs.
        OSRM_EDGE_REAL_SPEED_CAP_MPS = self.MAX_PLAUSIBLE_SPEED_MPS  # 20 m/s / 72 km/h -- same cap already used elsewhere for a bus/minibus/microbus ceiling
        osrm_edge_cache = {}
        try:
            with open(f"{self.folder}/osrm_edge_cache.json", 'r', encoding='utf-8') as f:
                osrm_edge_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        real_distance_corrected = 0
        if osrm_edge_cache:
            for (u, v, route_id, direction_id), avg_time in list(pair_avg_time.items()):
                vtype = self.routes.get(route_id, {}).get('vehicle_type')
                if vtype not in ('bus', 'minibus', 'microbus'):
                    continue
                entry = osrm_edge_cache.get(f"{u}|{v}")
                if not entry or avg_time <= 0:
                    continue
                real_dist_m = entry['distance_m']
                floor_time = real_dist_m / OSRM_EDGE_REAL_SPEED_CAP_MPS
                if avg_time < floor_time:
                    pair_avg_time[(u, v, route_id, direction_id)] = floor_time
                    real_distance_corrected += 1

        # NOTE: an earlier version of this pass floored any edge under
        # half of the SLOWEST same-stop-pair edge from a different route
        # -- i.e. always assumed the slower route's number was the
        # trustworthy one. Reverted: it touched ~4,400 edges feed-wide,
        # and on the one real case it was aimed at (352s vs 97s for the
        # same two stops, 797m apart), it turned out to have the
        # assumption backwards -- direct diagnosis against a rider's
        # confirmed real route showed the SLOW number (Minibus 66) was
        # the accurate one and the fast competing route (CTA_1053) was
        # the noise, not the other way around. There's no general way to
        # know in advance which side of a disagreement is the bad one.
        #
        # This version instead pulls each outlier PARTWAY toward the
        # cross-route MEDIAN for that exact stop pair -- not toward
        # whichever extreme happens to be slowest or fastest -- so it
        # softens disagreements symmetrically without betting on a
        # direction. It only fires where there's real corroboration to
        # act on: at least 3 DIFFERENT routes independently timing the
        # identical (u, v) hop (common in this feed -- Cairo's bus
        # network is heavily hub-and-spoke, so busy corridors near
        # Ramses/Tahrir/Abbaseya can have 10-100+ routes sharing the
        # same couple of stops), and only when a route's own number is
        # at least 1.5x off that median in either direction. Even then
        # it's a 50/50 blend toward the median, not a snap to it, so a
        # single route that's genuinely, correctly different (e.g. an
        # express service skipping stops the others make) keeps most of
        # its own signal instead of being forced to match the crowd.
        CROSS_ROUTE_MIN_CORROBORATING_ROUTES = 3
        CROSS_ROUTE_OUTLIER_RATIO = 1.5
        CROSS_ROUTE_BLEND = 0.5
        pair_times_by_route = defaultdict(dict)  # (u, v) -> {route_id: avg_time}
        for (u, v, route_id, direction_id), avg_time in pair_avg_time.items():
            # A reconstructed route's time is an estimate, so it contributes
            # NOTHING to the median. It is still corrected BY the median
            # below -- the asymmetry is the whole point. Without this, every
            # surveyed route sharing a corridor with a reconstructed one gets
            # pulled toward a number that was invented.
            if route_id in self.reconstructed_route_ids:
                continue
            # Same physical (u, v) hop can appear under more than one
            # direction_id across different routes' own conventions --
            # keyed by route_id here (not direction_id) since it's the
            # SAME physical stop pair being compared either way.
            pair_times_by_route[(u, v)][route_id] = avg_time

        cross_route_corrected = 0
        for (u, v, route_id, direction_id), avg_time in list(pair_avg_time.items()):
            same_pair = pair_times_by_route.get((u, v), {})
            if len(same_pair) < CROSS_ROUTE_MIN_CORROBORATING_ROUTES:
                continue
            sorted_vals = sorted(same_pair.values())
            n = len(sorted_vals)
            median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
            if median <= 0:
                continue
            ratio = avg_time / median
            if ratio >= CROSS_ROUTE_OUTLIER_RATIO or ratio <= 1 / CROSS_ROUTE_OUTLIER_RATIO:
                pair_avg_time[(u, v, route_id, direction_id)] = (
                    avg_time + (median - avg_time) * CROSS_ROUTE_BLEND
                )
                cross_route_corrected += 1

        for (u, v, route_id, direction_id), avg_time in pair_avg_time.items():
            self.graph[u].append((v, route_id, direction_id, avg_time))

        if implausible_guarded:
            print(f"Guarded {implausible_guarded} implausible-timing edges (replaced with distance-based estimates).")
        if slow_outlier_guarded:
            print(f"Guarded {slow_outlier_guarded} within-trip slow-outlier edges (replaced with that trip's own average pace).")
        if real_distance_corrected:
            print(f"Corrected {real_distance_corrected} edges against real OSRM road distance ({len(osrm_edge_cache)} stop pairs cached).")
        if cross_route_corrected:
            print(f"Blended {cross_route_corrected} cross-route-outlier edges partway toward their stop pair's multi-route median.")

        self._add_interchange_edges()
        self._add_rail_interchange_edges()
        print(f"Engine Ready in {round(time.time() - start_time, 2)} seconds.")

    def _add_rail_interchange_edges(self):
        """
        Walking links between rail stations of DIFFERENT modes that sit
        further apart than the general WALK_LINK_RADIUS_M.

        One connection in Greater Cairo needs this and the general pass
        cannot provide it. The LRT's "Arts and Culture City" and the
        monorail's "Art and Culture City" are the designed interchange
        between the two lines in the New Administrative Capital, and they
        are 628m apart -- past the 500m radius. Without an edge there, the
        monorail's entire New Capital half is reachable only by surface
        road, and a Badr-to-Justice City journey (LRT then monorail, the
        obvious way to make it) comes back as no route at all.

        Raising WALK_LINK_RADIUS_M globally would have fixed it too, and
        was rejected: it applies to all 3,119 stops, would add thousands of
        edges to the busiest surface corridors, and would re-open the
        transfer-chaining behaviour the 500m value was tuned to control.
        This pass touches only rail.

        The different-modes condition is what keeps it honest. Plenty of
        same-line station pairs fall inside 750m -- Ghamra and El Geish are
        774m apart, Dokki and Bohooth 1,080m -- and joining consecutive
        stations of one line by a walking edge would invite the search to
        "transfer" between two points already connected by the train the
        rider is sitting on. Requiring the two stops to share NO rail mode
        excludes every one of those by construction.
        """
        RAIL_INTERCHANGE_WALK_M = 750

        rail_modes = {}
        for (route_id, _direction), seq in self.route_stop_sequence.items():
            vtype = self.routes.get(route_id, {}).get('vehicle_type')
            if vtype not in RAIL_VTYPES:
                continue
            for stop_id in seq:
                if stop_id in self.stops:
                    rail_modes.setdefault(stop_id, set()).add(vtype)

        existing = {
            (s1, n) for s1, edges in self.graph.items()
            for n, r, _d, _t in edges if r == "WALK"
        }

        added = 0
        stop_ids = sorted(rail_modes)
        for i, s1 in enumerate(stop_ids):
            m1 = rail_modes[s1]
            a = self.stops[s1]
            for s2 in stop_ids[i + 1:]:
                if m1 & rail_modes[s2]:
                    continue
                if (s1, s2) in existing:
                    continue
                b = self.stops[s2]
                d_m = self._haversine(a['lat'], a['lon'], b['lat'], b['lon'])
                if not (0 < d_m <= RAIL_INTERCHANGE_WALK_M):
                    continue
                walk_time = max(d_m / self.WALK_SPEED_MPS, 10)
                self.graph[s1].append((s2, "WALK", None, walk_time))
                self.graph[s2].append((s1, "WALK", None, walk_time))
                added += 1

        if added:
            print(f"Rail interchange edges: {added} cross-mode walking link(s) "
                  f"beyond the {self.WALK_LINK_RADIUS_M}m general radius.")

    def _add_interchange_edges(self):
        cell_deg = self.WALK_LINK_RADIUS_M / 111000
        grid = defaultdict(list)
        for stop_id, info in self.stops.items():
            key = (round(info['lon'] / cell_deg), round(info['lat'] / cell_deg))
            grid[key].append(stop_id)

        # A single OSRM Table request covering every stop in the network
        # at once (all ~3000 of them -- an ~9M-cell matrix) reliably
        # failed/timed out on EVERY boot, silently falling back to
        # haversine for every interchange edge feed-wide. Confirmed by
        # reproducing the exact call size locally during the 2026-08-22
        # routing-engine audit: with the old code, `involved_stops` came
        # out to 2997 of the feed's 3081 stops -- a 2997x2997 request --
        # even though only ~7500 of those ~9M cells (0.08%) were ever
        # actually used, since each stop only cares about its own
        # ~500m neighborhood. Fixed by batching: one Table call per
        # spatial neighborhood (the same 3x3-cell grouping already used
        # below to find candidate pairs), each covering only the
        # handful-to-low-hundreds of stops actually near each other, with
        # a defensive INTERCHANGE_TABLE_BATCH_MAX re-chunk so one
        # unusually dense cluster (e.g. a major metro interchange) can't
        # reintroduce the same oversized-request problem. A pair that
        # happens to split across two sub-batches of one dense
        # neighborhood just keeps its haversine estimate for that pair --
        # a minor accuracy trade-off, not a correctness bug, and one that
        # should affect at most a handful of pairs in the busiest spots.
        INTERCHANGE_TABLE_BATCH_MAX = 200

        # A real OSM pedestrian-data gap can make OSRM's foot router
        # detour all the way around an obstacle instead of through the
        # actual shortcut/gate real riders use -- confirmed on a real
        # case during the 2026-08-22 Nasr City investigation: the walk
        # from a bus stop to "Fair Zone" metro entrance, ~100m apart by
        # straight line (both sit right at the edge of the walled Cairo
        # Fairgrounds complex), came back from OSRM as 3010.9s (50
        # minutes) -- an implied walking speed of ~0.03 m/s, because the
        # real informal gap in the perimeter isn't mapped in OSM, so the
        # router had to route all the way around the walled complex to
        # find an actual mapped path. That one implausible value was
        # enough to make the search abandon the correct transfer point
        # several stops early. Reject any OSRM duration implying a
        # walking speed below this floor and fall back to the haversine
        # estimate for just that one pair -- 0.3 m/s allows up to ~4.2x
        # circuity versus straight-line distance (generous next to the
        # 5.8x worst-case real-road circuity documented in
        # claude/gtfs-sanity-check-2026-08-22.md for actual river/bridge
        # detours), while still catching a 30x+ explosion like this one.
        MIN_PLAUSIBLE_TRANSFER_WALK_MPS = 0.3

        # Candidate selection still uses haversine (cheap, and fine for
        # deciding which stops are "close enough" to be worth a real
        # walking-route lookup). The actual edge weight below prefers the
        # real OSRM street-network duration when available.
        seen_pairs = set()
        pair_haversine = {}
        resolved_osrm = {}
        rejected_implausible = 0
        for (gx, gy), stop_ids in grid.items():
            neighbor_ids = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbor_ids.extend(grid.get((gx + dx, gy + dy), []))
            candidates = set(neighbor_ids)

            cell_pairs = []
            for s1 in stop_ids:
                for s2 in candidates:
                    if s1 >= s2: continue
                    pair = (s1, s2)
                    if pair in seen_pairs: continue
                    seen_pairs.add(pair)

                    d_m = self._haversine(
                        self.stops[s1]['lat'], self.stops[s1]['lon'],
                        self.stops[s2]['lat'], self.stops[s2]['lon']
                    )
                    if 0 < d_m <= self.WALK_LINK_RADIUS_M:
                        pair_haversine[pair] = d_m
                        cell_pairs.append(pair)

            if not cell_pairs or not self.osrm.enabled:
                continue

            local_stops = sorted(candidates)
            for i in range(0, len(local_stops), INTERCHANGE_TABLE_BATCH_MAX):
                batch = local_stops[i:i + INTERCHANGE_TABLE_BATCH_MAX]
                if len(batch) < 2:
                    continue
                batch_index = {s: idx for idx, s in enumerate(batch)}
                relevant = [(s1, s2) for s1, s2 in cell_pairs if s1 in batch_index and s2 in batch_index]
                if not relevant:
                    continue
                coords = [(self.stops[s]['lat'], self.stops[s]['lon']) for s in batch]
                table = self.osrm.walking_table(coords)
                durations = table.get("durations") if table else None
                if durations is None:
                    continue
                for s1, s2 in relevant:
                    try:
                        d = durations[batch_index[s1]][batch_index[s2]]
                    except (IndexError, KeyError):
                        continue
                    if d is None or d <= 0:
                        continue
                    implied_speed = pair_haversine.get((s1, s2), 0) / d
                    if implied_speed < MIN_PLAUSIBLE_TRANSFER_WALK_MPS:
                        rejected_implausible += 1
                        continue
                    resolved_osrm[(s1, s2)] = max(d, 10)

        if not pair_haversine:
            return

        osrm_pairs = 0
        for (s1, s2), d_m in pair_haversine.items():
            walk_time = resolved_osrm.get((s1, s2))
            if walk_time is not None:
                osrm_pairs += 1
            else:
                walk_time = max(d_m / self.WALK_SPEED_MPS, 10)

            self.graph[s1].append((s2, "WALK", None, walk_time))
            self.graph[s2].append((s1, "WALK", None, walk_time))

        print(f"Interchange edges: {len(pair_haversine)} total, {osrm_pairs} from OSRM, "
              f"{len(pair_haversine) - osrm_pairs} from haversine fallback "
              f"({rejected_implausible} of those rejected as implausible OSRM detours).")

    # How many well-connected, straight-line-nearest UNIQUE stops get
    # pulled into the real-walking-distance comparison in
    # find_nearest_stop. This needs real headroom, not just a handful --
    # confirmed on a real case (a Zamalek destination near the Nile) that
    # the genuinely-closest-to-walk-to stop ranked 36th by straight-line
    # distance, because ~30 mainland stops across the river all measured
    # "closer" than it despite requiring a real bridge detour to reach.
    # See claude/gtfs-sanity-check-2026-08-22.md and
    # claude/lint-cleanup-2026-08-22.md in the project for the
    # investigation. 50 gives real margin above that confirmed case.
    REAL_DISTANCE_CANDIDATE_POOL = 50
    # How many extra raw (pre-dedup) candidates to pull before filtering,
    # since multiple raw GTFS stop_ids commonly cluster into one canonical
    # stop (confirmed ~42% of stops feed-wide, per the "Clustered N
    # duplicate-hub stop_ids" boot log) -- without over-fetching, dedup
    # alone can shrink the final pool well below REAL_DISTANCE_CANDIDATE_POOL.
    REAL_DISTANCE_RAW_OVERFETCH = 2
    # Small fallback pool size if the batched OSRM Table call fails (e.g.
    # OSRM overloaded/unreachable for that one request) -- individually
    # checking a wide pool one-by-one would be far too slow, so this
    # degrades to checking just the closest few instead of the full pool.
    REAL_DISTANCE_FALLBACK_CHECK_CANDIDATES = 5
    # How many destination-stop candidates run_raptor_by_coords evaluates
    # a full path search for, per profile, instead of committing to the
    # single nearest one. See _find_nearest_stop_candidates for why this
    # matters -- the nearest stop by real walking distance isn't always
    # the stop that gives the best overall trip. Kept modest (each extra
    # candidate is a full Dijkstra run per profile, so this multiplies
    # request latency roughly linearly); 4 was enough to include Safaa
    # Hegazy alongside the closer-but-wrong Imbaba Police Station in the
    # confirmed real case that motivated this.
    DESTINATION_CANDIDATE_K = 4

    # --- Water-crossing guard (Zamalek/Gezira island) --------------------
    #
    # WHY THIS EXISTS. A rider was told to walk 538m from "Imbaba Police
    # Station" to a destination on Zamalek. The real walk is 5.2km (Google
    # Maps, verified 2026-08-22) because the two points are on opposite
    # banks of the Nile. OSRM's foot profile genuinely returned 538m: the
    # OSM extract has a way across the water there -- almost certainly the
    # Imbaba RAILWAY bridge -- that the foot profile is willing to use but
    # no pedestrian actually can.
    #
    # This is the mirror image of MIN_PLAUSIBLE_TRANSFER_WALK_MPS. That
    # guard catches OSRM reporting an implausibly LONG walk (missing OSM
    # path). This catches an implausibly SHORT one (OSM path that isn't
    # really walkable). Critically, NO distance heuristic can detect this:
    # 538m against a 376m straight line is a perfectly ordinary 1.43x
    # ratio. The only way to know is to know where the water is.
    #
    # Scoped to Zamalek deliberately -- both times this bug has been
    # reported (the original "walk over the Nile" report that started the
    # 2026-08-21 work, and this one) it involved this island. Widening it
    # to the whole Nile means encoding every bridge from Imbaba to Al
    # Munib, where one missing bridge silently breaks legitimate routes.
    #
    # Outline verified 2026-08-22 by classifying all 109 GTFS stops in the
    # area (14 land inside, all of them genuinely Zamalek/Gezira) and
    # against six points whose district Google's geocoder confirms:
    # Zamalek/Gezira inside; Imbaba, Agouza and Bulaq outside.
    ZAMALEK_ISLAND_POLYGON = [
        (30.0738, 31.2215), (30.0715, 31.2235), (30.0700, 31.2245),
        (30.0650, 31.2252), (30.0622, 31.2255), (30.0550, 31.2270),
        (30.0500, 31.2288), (30.0450, 31.2290), (30.0420, 31.2288),
        (30.0395, 31.2255), (30.0390, 31.2230), (30.0410, 31.2210),
        (30.0440, 31.2215), (30.0490, 31.2225), (30.0550, 31.2200),
        (30.0590, 31.2185), (30.0620, 31.2163), (30.0660, 31.2180),
        (30.0700, 31.2188), (30.0722, 31.2198),
    ]
    # How close a boundary crossing must be to a known bridge to be
    # allowed. Generous, because the derived bridge points are segment
    # midpoints from shapes.txt rather than exact bridge centrelines.
    ISLAND_BRIDGE_TOLERANCE_M = 500
    # Cluster radius when collapsing raw shape crossings into bridges.
    ISLAND_BRIDGE_CLUSTER_M = 300

    def _point_in_island(self, lat, lon):
        poly = self.ZAMALEK_ISLAND_POLYGON
        n = len(poly)
        inside = False
        j = n - 1
        for i in range(n):
            yi, xi = poly[i]
            yj, xj = poly[j]
            if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _derive_island_bridges(self):
        """
        Where real vehicle routes cross the island boundary -- i.e. the
        actual bridges. Derived from shapes.txt rather than hard-coded
        from memory, which means it self-maintains if the feed changes
        and, more importantly, it CANNOT include a crossing no transit
        vehicle uses. That is exactly what excludes the Imbaba railway
        bridge: it carries no bus route, so no shape crosses there, so a
        pedestrian route over it is rejected.
        """
        if not self.shapes:
            return
        lats = [p[0] for p in self.ZAMALEK_ISLAND_POLYGON]
        lons = [p[1] for p in self.ZAMALEK_ISLAND_POLYGON]
        # Generous bbox pre-filter so the point-in-polygon test only runs
        # on the handful of shape points anywhere near the island, rather
        # than all ~530k of them.
        lat_lo, lat_hi = min(lats) - 0.01, max(lats) + 0.01
        lon_lo, lon_hi = min(lons) - 0.01, max(lons) + 0.01

        raw = []
        for points in self.shapes.values():
            prev = None
            for lat, lon in points:
                if not (lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi):
                    prev = None
                    continue
                cur = self._point_in_island(lat, lon)
                if prev is not None and cur != prev[0]:
                    raw.append(((prev[1] + lat) / 2.0, (prev[2] + lon) / 2.0))
                prev = (cur, lat, lon)

        clusters = []
        for lat, lon in raw:
            for cl in clusters:
                if self._haversine(lat, lon, cl['lat'], cl['lon']) <= self.ISLAND_BRIDGE_CLUSTER_M:
                    cl['pts'].append((lat, lon))
                    cl['lat'] = sum(p[0] for p in cl['pts']) / len(cl['pts'])
                    cl['lon'] = sum(p[1] for p in cl['pts']) / len(cl['pts'])
                    break
            else:
                clusters.append({'lat': lat, 'lon': lon, 'pts': [(lat, lon)]})

        self.island_bridges = [(c['lat'], c['lon'], len(c['pts'])) for c in clusters]
        if self.island_bridges:
            print(f"Island crossings: {len(self.island_bridges)} bridge(s) derived from "
                  f"{len(raw)} route-shape crossings of the Zamalek boundary.")

    @staticmethod
    def _segments_intersect(p1, p2, p3, p4):
        """Standard orientation test, in lat/lon treated as a plane --
        fine at this scale (a few hundred metres)."""
        def orient(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
        d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
        return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))

    def _crosses_water_illegally(self, lat1, lon1, lat2, lon2):
        """
        True if the straight line between two points crosses the Zamalek
        island boundary somewhere that isn't a real bridge.

        Uses the straight line rather than OSRM's returned geometry on
        purpose: OSRM's geometry is exactly what we do not trust here, and
        a candidate stop whose real walk has to detour kilometres to a
        bridge is not a plausible last-mile walk anyway -- rejecting it is
        the right answer regardless of which path OSRM imagines.
        """
        if not self.island_bridges:
            return False
        poly = self.ZAMALEK_ISLAND_POLYGON
        n = len(poly)
        p1, p2 = (lat1, lon1), (lat2, lon2)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            if not self._segments_intersect(p1, p2, a, b):
                continue
            # Approximate the crossing point as the midpoint of the
            # polygon edge segment that was cut -- good to well within
            # ISLAND_BRIDGE_TOLERANCE_M at this scale.
            cx, cy = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
            near_bridge = any(
                self._haversine(cx, cy, blat, blon) <= self.ISLAND_BRIDGE_TOLERANCE_M
                for blat, blon, _ in self.island_bridges
            )
            if not near_bridge:
                return True
        return False

    def find_nearest_stop(self, lat, lon):
        """
        Picks the nearest WELL-CONNECTED stop, not just the geographically
        nearest one. See _find_nearest_stop_candidates for the full
        ranking logic -- this just takes its top result. Kept as a
        separate method (rather than inlining `[0]` everywhere) since
        most callers only ever want a single stop; run_raptor_by_coords'
        destination side is the one exception that needs the ranked list
        (see its own comment for why).

        Returns (None, None) if even the closest stop is farther than
        MAX_SNAP_DISTANCE_M -- there's a hard floor here on purpose.
        Without it, this always returns *some* stop no matter how far away
        (even hundreds of km, for a query point genuinely outside GTFS
        coverage), and that absurd first/last-mile "walk" distance then
        flows straight into the trip total. Callers must check for
        (None, None) and skip transit path-finding rather than snap to a
        stop that's unreasonably far to ever actually walk to.
        """
        candidates = self._find_nearest_stop_candidates(lat, lon, k=1)
        if not candidates:
            return None, None
        return candidates[0]

    def _find_nearest_stop_candidates(self, lat, lon, k=1):
        """
        Picks up to k nearest WELL-CONNECTED stops, ranked by REAL
        walking distance -- not just the geographically nearest one(s).
        Snapping to an isolated/poorly-served stop (few or no graph
        edges) forces the router through a long detour just to reach the
        rest of the network -- exactly the "10km trip for a 2km
        destination" failure mode. Among the nearest candidates, prefer
        ones with real connectivity; only fall back to the single closest
        stop if none of the candidates have any connections at all.

        Among a wide pool of the closest connected candidates (see
        REAL_DISTANCE_CANDIDATE_POOL), the ranking is by REAL walking
        distance (via one batched OSRM Table call covering the whole
        pool), not straight-line distance. Straight-line distance is
        badly misleading near a river/canal: a stop just across the water
        from the query point can measure closer by haversine than a stop
        on the SAME side that's actually much shorter to walk to, since
        reaching the "closer" one for real means detouring to a bridge --
        and confirmed on a real case, the genuinely-best stop can rank
        dozens of places down by straight-line distance alone (see pool
        comment above), so only reranking a small top-few does not catch
        this. A single Table call for the whole pool is cheap enough to
        do at request time even at this size; falls back to individually
        checking just the closest few if the Table call fails, and to
        pure straight-line ranking if OSRM is unreachable/disabled
        entirely (walking_route/walking_table degrade to haversine
        themselves in that case -- see osrm_client.py -- so this is a
        no-op when use_osrm=False, as in the test suite).

        k > 1 exists because "nearest stop" and "best stop to route
        through" are not the same question, especially for a
        DESTINATION: a rider legitimately gets off one stop early and
        walks a bit farther when that stop is served by a much better
        route. Confirmed on a real case during the 2026-08-22 Nasr City
        investigation: for a Zamalek destination, the real-nearest stop
        was "Imbaba Police Station" across the river, and committing to
        it single-handedly forced a much worse route than getting off at
        Safaa Hegazy (the rider's confirmed-correct stop, ~700m farther).
        A single best-stop pick can never recover from a case like that
        -- see run_raptor_by_coords, which evaluates several of these
        candidates for the destination and picks whichever gives the
        best total trip, not just the shortest final walk.

        Returns a list of (stop_id, real_distance_m) sorted ascending by
        real distance, shortest first -- length up to k, possibly empty
        if nothing is within MAX_SNAP_DISTANCE_M. When OSRM is
        unavailable, "real_distance_m" degrades to the haversine
        distance instead (same fallback philosophy as elsewhere).
        """
        candidates = []
        for stop_id, data in self.stops.items():
            dist = self._haversine(lat, lon, data['lat'], data['lon'])
            # Resolve to the canonical stop_id up front -- non-canonical
            # (clustered-away) stop_ids have zero graph edges after
            # _build_stop_clusters, so returning one directly would break
            # routing entirely for whoever snaps to it.
            canonical_id = self.stop_alias.get(stop_id, stop_id)
            candidates.append((dist, canonical_id))
        candidates.sort(key=lambda x: x[0])

        if not candidates or candidates[0][0] > self.MAX_SNAP_DISTANCE_M:
            return []

        # Cap by real distance (MAX_SNAP_DISTANCE_M), not just count. In a
        # sparse-coverage area (e.g. a satellite city like 10th of
        # Ramadan or the New Administrative Capital), the Nth-nearest
        # stop by straight-line distance can be dozens of km away --
        # without this cap, the batched OSRM Table call below ends up
        # covering a huge area, which was confirmed to overload/crash the
        # OSRM connection for exactly these sparse-area cases (dropped
        # connections on live requests, not just a slow response). A stop
        # that far away was never a plausible last-mile walk anyway.
        raw_top = [
            (dist, sid) for dist, sid in candidates[:self.REAL_DISTANCE_CANDIDATE_POOL * self.REAL_DISTANCE_RAW_OVERFETCH]
            if dist <= self.MAX_SNAP_DISTANCE_M
        ]
        connected_raw = [(dist, sid) for dist, sid in raw_top if len(self.graph.get(sid, [])) >= 2]
        if not connected_raw:
            top = candidates[:self.NEAREST_STOP_CANDIDATES]
            return [(top[0][1], top[0][0])] if top else []

        connected_raw.sort(key=lambda x: x[0])
        seen = set()
        pool = []
        for dist, sid in connected_raw:
            if sid in seen:
                continue
            seen.add(sid)
            pool.append((dist, sid))
            if len(pool) >= self.REAL_DISTANCE_CANDIDATE_POOL:
                break

        # Drop candidates that sit across water from the query point with
        # no bridge between -- see _crosses_water_illegally. Done BEFORE
        # asking OSRM, since OSRM's answer for exactly these pairs is the
        # thing that can't be trusted. Skipped entirely if it would empty
        # the pool, so a query genuinely stranded across water still gets
        # a (bad) answer rather than no answer at all.
        if self.island_bridges:
            on_land = [
                (dist, sid) for dist, sid in pool
                if not self._crosses_water_illegally(
                    lat, lon, self.stops[sid]['lat'], self.stops[sid]['lon'])
            ]
            if on_land:
                pool = on_land

        if len(pool) == 1:
            return [(pool[0][1], pool[0][0])]

        coords = [(lat, lon)] + [(self.stops[sid]['lat'], self.stops[sid]['lon']) for _, sid in pool]
        table = self.osrm.walking_table(coords)
        if table and table.get('distances'):
            row = table['distances'][0]
            ranked = []
            for i, (_, sid) in enumerate(pool):
                real_dist = row[i + 1] if i + 1 < len(row) else None
                if real_dist is None:
                    continue
                ranked.append((sid, real_dist))
            if ranked:
                ranked.sort(key=lambda x: x[1])
                return ranked[:k]

        # Table call failed (OSRM overloaded/unreachable for this one
        # request) -- degrade to individually checking just the closest
        # few rather than the whole pool, which would be too slow one call
        # at a time.
        check_pool = pool[:self.REAL_DISTANCE_FALLBACK_CHECK_CANDIDATES]
        ranked = []
        for haversine_dist, sid in check_pool:
            stop_data = self.stops[sid]
            real = self.osrm.walking_route(lat, lon, stop_data['lat'], stop_data['lon'])
            ranked.append((sid, real['distance_m']))
        if not ranked:
            return []
        ranked.sort(key=lambda x: x[1])
        return ranked[:k]

    def _get_route_color(self, vehicle_type):
        return {
            "metro": "#6DA4C2",
            "monorail": "#9C6DC2",   # kept distinct from metro's blue: the
            "lrt": "#4DA89B",        # two share stations at Adly Mansour and
            "tram": "#C2A76D",       # Arts & Culture City, and a rider
            "apm": "#8895A0",        # reading the map has to tell them apart
            "bus": "#E29578",
            "minibus": "#D4A373",
            "microbus": "#C77DFF",
        }.get(vehicle_type, "#E29578")

    def _localized_stop_name(self, stop_id, lang):
        name = self.stops[stop_id]['name']
        if lang == 'ar':
            return self.stop_name_ar.get(name, name)
        return name

    def _describe_route(self, route_id, lang):
        """
        Structured route description used to build turn-by-turn text
        client-side (so it can be genuinely localized via ARB templates
        instead of parsing pre-formatted English sentences). Always
        prefers a real route number when the feed has one; falls back to
        an explicit vehicle-type label + corridor description otherwise
        -- e.g. "Bus 123" when a real number exists, "Microbus towards
        Nahda City" when it doesn't (route_short_name here is often just
        the literal word "Microbus"/"Minibus" with no assigned number).
        """
        if route_id == "WALK":
            return {"vehicle_type": "walk", "route_number": None, "route_description": None}

        route = self.routes.get(route_id, {})
        short_name = (route.get('short_name') or '').strip()
        long_name = route.get('long_name') or ''
        vehicle_type = route.get('vehicle_type', 'bus')

        # A real route number: short_name contains digits beyond just the
        # generic vehicle-type word (so "Microbus" alone doesn't count,
        # but "CTA 1073" or "Minibus 112" does).
        route_number = None
        digits = ''.join(ch for ch in short_name if ch.isdigit())
        if digits:
            route_number = digits

        description = long_name
        if lang == 'ar' and long_name in self.route_long_name_ar:
            description = self.route_long_name_ar[long_name]

        return {
            "vehicle_type": vehicle_type,
            "route_number": route_number,
            "route_description": description if description else None,
        }

    def _find_equivalent_routes(self, board_stop_id, alight_stop_id, primary_route_id, primary_direction_id,
                                 lang="en", max_results=4):
        """
        Instead of committing to one "correct" route between board_stop_id
        and alight_stop_id, find every OTHER route that also serves this
        exact corridor (stops at board_stop_id, then later at
        alight_stop_id, in that order, in its own direction) -- these are
        the routes a rider could substitute for the one RAPTOR happened to
        pick, since in Cairo's GTFS feed dozens of route_ids commonly
        share the same physical corridor and their recorded timetables
        frequently disagree or are simply unreliable (see the 2026-08-22
        sanity-check doc). Rather than betting on which one's number is
        right, surface all of them plus a live ETA where available, and
        let the rider (or real-time position) decide.

        Returns a list of candidates, PRIMARY ROUTE FIRST, each carrying
        a live ETA to the boarding stop when riders are currently
        reporting position on that route+direction (see live_tracking.py).
        The ETA's distance is measured along the CANDIDATE route's own
        real street shape, not a straight line (see
        _route_following_distance_m()) -- meaningfully more accurate on
        corridors that need a real detour. When nobody is live-tracked on
        a route, eta_sec is None and typical_headway_min carries a
        schedule-derived "runs about every N min" fallback instead (see
        route_typical_headway_min in load_data()) so there's still a
        useful signal for a rider on a route with zero current live
        coverage, rather than a bare "no data".

        DESIGN DECISION on vehicle type: a candidate is included whenever
        it covers the same board->alight corridor, REGARDLESS of vehicle
        type -- a microbus and a bus both serving the same two stops are
        genuinely interchangeable for getting the rider where they're
        going, even though they differ in fare/comfort. Each candidate
        carries same_vehicle_type (relative to the primary) so the
        frontend can badge/group on it, but it does not exclude anything.
        Ranking is live-ETA-soonest first regardless of vehicle type
        (a microbus arriving in 2 min beats a bus in 10); only among
        candidates with NO live data does same_vehicle_type break ties,
        on the reasoning that without a live signal to go on, a
        same-type substitute is the closer guess.

        Capped at max_results total including the primary, since a busy
        hub stop can have 50+ technically-matching routes and showing
        all of them would be noise, not help -- live-tracked candidates
        are always preferred when trimming.
        """
        board = self.stop_alias.get(board_stop_id, board_stop_id)
        alight = self.stop_alias.get(alight_stop_id, alight_stop_id)
        board_info = self.stops.get(board)
        if board_info is None:
            return []

        primary_vtype = self.routes.get(primary_route_id, {}).get('vehicle_type')

        def _candidate(route_id, direction_id, is_primary):
            route_desc = self._describe_route(route_id, lang)

            def _distance_fn(vlat, vlon, _route_id=route_id):
                return self._route_following_distance_m(_route_id, vlat, vlon, board_info['lat'], board_info['lon'])

            eta_list = self.live_store.get_eta(
                route_id, direction_id, board_info['lat'], board_info['lon'],
                vehicle_type=route_desc['vehicle_type'], distance_fn=_distance_fn,
            )
            best_eta = eta_list[0] if eta_list else None
            return {
                "route_id": route_id,
                "direction_id": direction_id,
                "vehicle_type": route_desc["vehicle_type"],
                "route_number": route_desc["route_number"],
                "route_description": route_desc["route_description"],
                "is_primary": is_primary,
                "same_vehicle_type": route_desc["vehicle_type"] == primary_vtype,
                "eta_sec": best_eta["eta_sec"] if best_eta else None,
                "eta_source": "live_gps" if best_eta else "no_live_data",
                "live_vehicles_tracked": len(eta_list),
                "typical_headway_min": None if best_eta else self.route_typical_headway_min.get((route_id, direction_id)),
            }

        candidates = [_candidate(primary_route_id, primary_direction_id, True)]
        seen_routes = {primary_route_id}

        for (route_id, direction_id), seq in self.route_stop_sequence.items():
            if route_id in seen_routes:
                continue
            try:
                bi = seq.index(board)
                ai = seq.index(alight)
            except ValueError:
                continue
            if bi >= ai:
                continue  # this route passes both stops but in the wrong order -- not a usable substitute
            seen_routes.add(route_id)
            candidates.append(_candidate(route_id, direction_id, False))

        # Primary always stays first. Among the rest: live-ETA-soonest
        # wins outright regardless of vehicle type; candidates with no
        # live data sort after every live-tracked one, and among THOSE,
        # a same-vehicle-type-as-primary match is preferred (see the
        # design-decision note above) -- stable order beyond that, since
        # there's nothing left to rank on.
        primary = candidates[0]
        rest = sorted(
            candidates[1:],
            key=lambda c: (c["eta_sec"] is None, c["eta_sec"] if c["eta_sec"] is not None else 0, not c["same_vehicle_type"]),
        )
        return [primary] + rest[: max_results - 1]

    def _estimate_leg_fare(self, route_id, distance_m):
        """
        Estimated fare in EGP for a single continuous leg on one route.
        No fare data exists in this GTFS feed at all (no
        fare_attributes.txt/fare_rules.txt), so this is a heuristic, not
        a lookup against an authoritative source:
          - formal bus / minibus: flat fare per boarding
          - microbus: distance-based estimate (informal microbus fares in
            Cairo are typically distance-based, not flat)
        """
        route_info = self.routes.get(route_id, {})
        vtype = route_info.get('vehicle_type', 'bus')
        if vtype in BANDED_FARE_TIERS:
            # Banded modes are priced once per journey on total hops, not
            # per leg -- see the fare block in _build_option_data. Returning
            # anything but 0 here would double-charge them.
            return 0
        if vtype == 'microbus':
            km = distance_m / 1000
            return max(MICROBUS_MIN_FARE, round(km * MICROBUS_FARE_PER_KM))
        if route_id.startswith('GRN') or route_info.get('agency_id') == 'GRN':
            return 25
        return FLAT_FARE_BY_VEHICLE.get(vtype, 20)

    def _inactive_routes_for_day(self, today_day):
        """
        Full set of route_ids that do NOT run on today_day, computed once
        and cached per day rather than checked per-edge. Dijkstra examines
        hundreds of thousands of edges per request in a graph this size,
        so even a cheap cached function call per edge adds up fast --
        this turns that into a plain 'in' set-membership check in the hot
        loop instead, with zero function-call overhead.
        """
        cached = self._route_active_cache.get(today_day)
        if cached is not None:
            return cached
        if not self.service_days:
            result = frozenset()
        else:
            result = frozenset(
                route_id for route_id, service_ids in self.route_service_ids.items()
                if not any(today_day in self.service_days.get(sid, set()) for sid in service_ids)
            )
        self._route_active_cache[today_day] = result
        return result

    # How long before a route's service window opens we still consider it
    # usable. A rider searching at 05:50 for an 06:00 first bus is making
    # a completely reasonable request, and refusing it would be worse
    # than slightly optimistic. Anything earlier than this genuinely
    # should not be offered as a ride leaving now.
    SERVICE_WINDOW_GRACE_SEC = 45 * 60

    def _routes_not_running_at(self, now_sec):
        """
        Full set of route_ids whose declared frequencies.txt service
        window does NOT cover now_sec (seconds since midnight, local).
        Same shape and rationale as _inactive_routes_for_day: computed
        once and cached per lookup bucket, so the hot Dijkstra loop only
        does an 'in' set-membership check.

        Routes the feed gives no hours for are treated as ALWAYS running
        (empty frequencies.txt coverage for a route means "no timetable
        surveyed", which in Cairo commonly means an informal or
        high-frequency route that runs all day anyway -- excluding it
        would create huge false coverage gaps).
        """
        if not hasattr(self, '_service_window_cache') or self._service_window_cache is None:
            self._service_window_cache = {}

        # Lookups are bucketed to 15-minute intervals -- service windows in
        # frequencies.txt are coarse (typically hours-wide), so sub-15m
        # precision adds zero value and destroys cache locality.
        bucket_sec = (now_sec // 900) * 900
        cached = self._service_window_cache.get(bucket_sec)
        if cached is not None:
            return cached

        if not self.route_service_windows:
            result = frozenset()
        else:
            grace = self.SERVICE_WINDOW_GRACE_SEC
            result = frozenset(
                route_id for route_id, windows in self.route_service_windows.items()
                if not any(start - grace <= now_sec <= end for start, end in windows)
            )
        self._service_window_cache[bucket_sec] = result
        return result

    def _nearest_shape_point(self, shape_id, stop_id, lat, lon):
        """Cached nearest-point lookup -- see _shape_index_cache comment."""
        key = (shape_id, stop_id)
        cached = self._shape_index_cache.get(key)
        if cached is not None:
            return cached
        points = self.shapes[shape_id]
        best_i, best_d = 0, float('inf')
        for i, (plat, plon) in enumerate(points):
            d = self._haversine(lat, lon, plat, plon)
            if d < best_d:
                best_d, best_i = d, i
        self._shape_index_cache[key] = (best_i, best_d)
        return best_i, best_d

    def _shape_segment(self, route_id, from_stop_id, to_stop_id, direction_id=None):
        """
        Real street-following points between two stops on route_id,
        sliced out of that route's shape (shapes.txt). Supports directional
        shapes when direction_id is provided. Falls back to a
        road-following driving route via OSRM if there's no GTFS shape for this
        route, guaranteeing lines follow actual streets and never cut straight
        through buildings.
        Guarantees that the returned segment starts exactly at from_stop
        and ends exactly at to_stop so endpoints are 100% connected.
        """
        from_lat, from_lon = self.stops[from_stop_id]['lat'], self.stops[from_stop_id]['lon']
        to_lat, to_lon = self.stops[to_stop_id]['lat'], self.stops[to_stop_id]['lon']
        fallback = [{"lat": from_lat, "lon": from_lon}, {"lat": to_lat, "lon": to_lon}]

        def _get_road_fallback():
            try:
                road = self.osrm.driving_route(from_lat, from_lon, to_lat, to_lon)
                geom = road.get('geometry')
                if geom and len(geom) >= 2:
                    return geom
            except Exception:
                pass
            return fallback

        shape_id = None
        if direction_id is not None:
            shape_id = self.route_direction_shape.get((route_id, str(direction_id)))
        if not shape_id:
            shape_id = self.route_shape.get(route_id)
        if not shape_id:
            return _get_road_fallback()
        points = self.shapes.get(shape_id)
        if not points or len(points) < 2:
            return _get_road_fallback()

        from_idx, from_dist = self._nearest_shape_point(shape_id, from_stop_id, from_lat, from_lon)
        to_idx, to_dist = self._nearest_shape_point(shape_id, to_stop_id, to_lat, to_lon)

        # If the nearest shape point is implausibly far from the actual
        # stop (>600m), this shape probably doesn't correspond to this
        # hop -- snap to road network instead.
        if from_dist > 600 or to_dist > 600 or from_idx == to_idx:
            return _get_road_fallback()

        if from_idx < to_idx:
            segment = points[from_idx:to_idx + 1]
        else:
            # Shape runs the opposite direction to travel -- reverse the
            # slice so the polyline still goes from -> to in order.
            segment = list(reversed(points[to_idx:from_idx + 1]))

        res = [{"lat": lat, "lon": lon} for lat, lon in segment]

        # Guarantee terminal connectivity: start at from_stop, end at to_stop
        d_start = self._haversine(from_lat, from_lon, res[0]["lat"], res[0]["lon"])
        if d_start > 10:
            res.insert(0, {"lat": from_lat, "lon": from_lon})
        else:
            res[0] = {"lat": from_lat, "lon": from_lon}

        d_end = self._haversine(to_lat, to_lon, res[-1]["lat"], res[-1]["lon"])
        if d_end > 10:
            res.append({"lat": to_lat, "lon": to_lon})
        else:
            res[-1] = {"lat": to_lat, "lon": to_lon}

        return res

    def _nearest_point_on_shape(self, shape_id, lat, lon):
        """Same linear scan as _nearest_shape_point, but uncached --
        _nearest_shape_point's cache is keyed by (shape_id, stop_id)
        because stops don't move, but a live vehicle's position changes
        on every report, so there's nothing stable to key a cache on
        here. Shapes in this feed run at most a few hundred points, so
        an uncached scan per live ETA lookup is cheap enough."""
        points = self.shapes[shape_id]
        best_i, best_d = 0, float('inf')
        for i, (plat, plon) in enumerate(points):
            d = self._haversine(lat, lon, plat, plon)
            if d < best_d:
                best_d, best_i = d, i
        return best_i, best_d

    def _route_following_distance_m(self, route_id, from_lat, from_lon, to_lat, to_lon):
        """
        Distance from (from_lat, from_lon) to (to_lat, to_lon) measured
        along route_id's real street-following shape (shapes.txt) instead
        of straight-line haversine -- used to turn a live vehicle's raw
        GPS fix into an ETA that accounts for the real road the vehicle
        has to follow, not a line drawn straight through whatever's in
        between (a building, the river, a highway median). Meaningfully
        more accurate wherever that matters: this feed's real road
        distance runs 1.63x haversine at the median and up to 5.8x for
        river-crossing/bridge-detour pairs (see the 2026-08-22
        sanity-check doc). Path length between two points on a fixed
        polyline doesn't depend on which one comes first, so this simply
        sums segment lengths between the two nearest-shape-point indices
        without needing to reason about direction of travel.

        Falls back to straight-line haversine when there's no usable
        shape for this route, or when either point snaps implausibly far
        (>500m) from the shape -- the same 500m threshold and rationale
        as _shape_segment(): don't trust a shape that doesn't actually
        correspond to this hop. Also a known simplification: routes here
        have exactly ONE representative shape (the first trip's, see
        load_data()), not one per direction_id, so a route whose two
        directions genuinely take different streets will have both
        measured against the same shape.
        """
        straight_m = self._haversine(from_lat, from_lon, to_lat, to_lon)

        shape_id = self.route_shape.get(route_id)
        if not shape_id:
            return straight_m
        points = self.shapes.get(shape_id)
        if not points or len(points) < 2:
            return straight_m

        from_idx, from_dist = self._nearest_point_on_shape(shape_id, from_lat, from_lon)
        to_idx, to_dist = self._nearest_point_on_shape(shape_id, to_lat, to_lon)
        if from_dist > 500 or to_dist > 500 or from_idx == to_idx:
            return straight_m

        lo, hi = min(from_idx, to_idx), max(from_idx, to_idx)
        segment = points[lo:hi + 1]
        route_dist_m = sum(
            self._haversine(segment[i][0], segment[i][1], segment[i + 1][0], segment[i + 1][1])
            for i in range(len(segment) - 1)
        )
        return route_dist_m

    # Fastest plausible transit speed in Cairo (metro included), used only
    # to build an admissible A* heuristic -- deliberately generous so the
    # heuristic never overestimates true remaining time (penalties like
    # transfer/metro cost are ignored by the heuristic too, which only
    # makes it a safer lower bound, never an overestimate).
    MAX_PLAUSIBLE_SPEED_MPS = 20  # 72 km/h

    def _heuristic(self, stop_id, end_stop_id):
        dist_m = self._haversine(
            self.stops[stop_id]['lat'], self.stops[stop_id]['lon'],
            self.stops[end_stop_id]['lat'], self.stops[end_stop_id]['lon']
        )
        return dist_m / self.MAX_PLAUSIBLE_SPEED_MPS

    def _find_shortest_path(self, start_stop, end_stop, profile="fastest", today_day=None, now_sec=None, exclude_metro=False, avoid_route_ids=None, min_walk=False, min_transfers=False):
        # 240s (4 min) was tuned assuming route timetables agree with each
        # other on shared corridors -- they don't. Two routes covering the
        # identical stop pair can differ by several minutes in this feed
        # (one observed case: 352s on one route's timetable vs 97s on
        # another's for the same two stops), and a too-small transfer
        # penalty lets the search chase that kind of noise -- hopping to a
        # "faster" route that's actually just measured differently, not
        # genuinely quicker -- instead of staying on a vehicle already
        # heading the right way. 480s (8 min) is a more realistic
        # real-world cost for actually catching a different bus anyway.
        TRANSFER_PENALTY_SEC = 240
        WALK_PENALTY_MULT = 1.0
        METRO_PENALTY_SEC = 0
        MICROBUS_TRANSFER_EXTRA_SEC = 300
        SURFACE_TRANSFER_EXTRA_SEC = 0
        FARE_WEIGHT_SEC_PER_EGP = 0

        if profile == "recommended":
            # Intricate balanced calculation: best bang for buck in Cairo transit.
            # Avoids grueling multi-hop transfers or 1km highway walks to save 1 minute,
            # but won't pay 3x fare just to arrive 2 minutes earlier.
            TRANSFER_PENALTY_SEC = 420
            WALK_PENALTY_MULT = 1.6
            FARE_WEIGHT_SEC_PER_EGP = 90  # 1.5 min per EGP value of time
            METRO_BOARDING_TRANSFER_SEC = 180
            MICROBUS_TRANSFER_EXTRA_SEC = 300
        elif profile == "regular":
            TRANSFER_PENALTY_SEC = 900
            WALK_PENALTY_MULT = 1.5
            METRO_BOARDING_TRANSFER_SEC = TRANSFER_PENALTY_SEC / 2
        elif profile == "cheapest":
            # Strict fare minimization: 1 EGP difference dominates 2.7 hours of travel time.
            # Guarantees the absolute lowest fare route, using travel time purely as tie-breaker.
            TRANSFER_PENALTY_SEC = 240
            WALK_PENALTY_MULT = 1.0
            FARE_WEIGHT_SEC_PER_EGP = 10000
            METRO_BOARDING_TRANSFER_SEC = 120
            MICROBUS_TRANSFER_EXTRA_SEC = 300
        else:  # fastest
            # Pure elapsed travel time minimization: 0 fare penalty, realistic transfer floors.
            TRANSFER_PENALTY_SEC = 240
            WALK_PENALTY_MULT = 1.0
            FARE_WEIGHT_SEC_PER_EGP = 0
            METRO_BOARDING_TRANSFER_SEC = 120
            MICROBUS_TRANSFER_EXTRA_SEC = 300

        if min_transfers:
            TRANSFER_PENALTY_SEC = max(TRANSFER_PENALTY_SEC, 2400)
        if min_walk:
            WALK_PENALTY_MULT = max(WALK_PENALTY_MULT, 3.5)

        # --- Known-good-corridor override -----------------------------
        # A rider who actually uses this network (not just the published
        # timetables) confirmed: from 8th District (Nasr City) toward
        # Cairo Fair Metro, Minibus 66 IS the right vehicle to stay on --
        # don't hop to a "faster-looking" competing bus first. But this
        # feed has several other routes (CTA_1053, Minibus 56, CTA 59...)
        # covering the same or an overlapping stretch of Nasr City with
        # their own independently-recorded times, and depending on
        # exactly how transfers were taxed, the search kept finding a
        # DIFFERENT one of those to be nominally faster than Minibus 66
        # -- including ones it never has to "transfer" to at all (e.g.
        # CTA 59, boarded as the very first vehicle of the trip, which no
        # transfer penalty can ever discourage). That means the
        # discrepancy isn't really about transfer costs, it's that this
        # one route's own recorded pace in the feed reads slower than
        # several competitors covering the same ground -- confirmed
        # against a verified rider's real route, this is one of the
        # spots where the feed itself is wrong, not the router.
        # Modeled as a modest speed credit (not a flat discount to 0,
        # which would let it win by an implausible margin) applied ONLY
        # to this one route_id, so it doesn't touch the other 1000+
        # routes sharing this same graph. If more corridors like this
        # get confirmed, this should grow into a small table rather than
        # more one-off route_ids inline here.
        TRUSTED_ROUTE_SPEED_DISCOUNT = {
            'sIt6Ay8Ghb65PsqmDHyii': 0.75,  # Minibus 66
        }
        # Scope the discount to just the confirmed stretch (8th District
        # Nasr City -> Cairo Fair Metro, direction 0) rather than the
        # route's full run out to Imbaba. Without this, discounting the
        # whole route made the search ALSO keep riding it past the Fair
        # Zone interchange (to Ghamra, then a Line 1 -> Line 3 metro
        # transfer) instead of getting off right at Cairo Fair Metro/Fair
        # Zone the way the confirmed route actually does -- the discount
        # made staying on the bus look cheaper than it should past the
        # point it was actually verified for.
        TRUSTED_DISCOUNT_STOP_SET = {
            '2457', '481', '484', '482', '1204', '1207', '2451', '2670',
            '709', '1436', '1763', '1634', '1248', '1250', '1630',
        }

        def _discount_applies(route_id, u, v):
            if route_id not in TRUSTED_ROUTE_SPEED_DISCOUNT:
                return False
            return u in TRUSTED_DISCOUNT_STOP_SET and v in TRUSTED_DISCOUNT_STOP_SET

        # MULTI-TARGET A*. end_stop is either a single stop_id or a dict
        # {stop_id: exit_cost_sec} of several acceptable destination
        # stops, each with the extra cost of finishing the trip from it
        # (the last-mile walk). See run_raptor_by_coords for WHY several
        # destination stops need considering.
        #
        # Doing that as K separate single-target searches is what the
        # first version did, and it was far too slow to ship: each search
        # measured ~700ms on this graph, so K=4 across 3 profiles meant
        # 12 searches and a confirmed 12.5-SECOND live API response.
        # Folding the candidates into one search per profile cuts that to
        # 3 searches total for the same answer.
        # MULTI-ORIGIN & MULTI-TARGET A*.
        # start_stop is either a single stop_id or a dict {stop_id: entry_cost_sec}
        # end_stop is either a single stop_id or a dict {stop_id: exit_cost_sec}
        targets = {end_stop: 0.0} if isinstance(end_stop, str) else dict(end_stop)
        starts = {start_stop: 0.0} if isinstance(start_stop, str) else dict(start_stop)
        if not targets or not starts:
            return None, 0

        # Admissible heuristic against MULTIPLE targets, without paying
        # for one haversine per target on every push. Measuring to a
        # single reference target and subtracting the widest spread
        # between it and any other target can only ever UNDERestimate the
        # true remaining distance (triangle inequality), which is exactly
        # the direction that keeps A* optimal. The candidates are all
        # within a few hundred metres of one destination in practice, so
        # the slack this gives up is small.
        ref_target = next(iter(targets))
        ref_info = self.stops[ref_target]
        target_spread_m = max(
            (self._haversine(ref_info['lat'], ref_info['lon'],
                             self.stops[t]['lat'], self.stops[t]['lon'])
             for t in targets),
            default=0.0,
        )

        def heuristic(stop_id):
            dist_m = self._haversine(
                self.stops[stop_id]['lat'], self.stops[stop_id]['lon'],
                ref_info['lat'], ref_info['lon']
            )
            return max(0.0, dist_m - target_spread_m) / self.MAX_PLAUSIBLE_SPEED_MPS

        heappush, heappop = heapq.heappush, heapq.heappop

        nodes = []
        queue = []
        push_count = 0
        for sid, entry_cost in starts.items():
            node_idx = len(nodes)
            nodes.append((-1, sid, None, None))
            push_count += 1
            f = entry_cost + heuristic(sid)
            heappush(queue, (f, push_count, node_idx, entry_cost, False, 0, None))

        visited = set()
        best_known = {}
        inactive_routes_today = self._inactive_routes_for_day(today_day) if today_day is not None else frozenset()
        if now_sec is not None:
            closed_now = self._routes_not_running_at(now_sec)
            if closed_now:
                inactive_routes_today = frozenset(inactive_routes_today) | closed_now

        best_path, best_cost, best_total = None, 0, float('inf')

        # HOT-LOOP LOCALS.
        graph = self.graph
        route_vtype = self._route_vtype
        metro_ids = self.metro_route_ids
        headway_map = self.route_typical_headway_min
        fallback_headway_min = self.FALLBACK_HEADWAY_MIN
        wait_cap_sec = self.EXPECTED_WAIT_CAP_SEC
        default_fare = FLAT_FARE_BY_VEHICLE['bus']
        fare_by_vtype = {
            vt: (MICROBUS_TYPICAL_FARE_EGP if vt == 'microbus'
                 else FLAT_FARE_BY_VEHICLE.get(vt, default_fare))
            for vt in set(route_vtype.values()) | {'bus', 'minibus', 'microbus', 'metro'}
            if vt
        }
        # First-boarding estimate for each banded mode: its cheapest band.
        # The search can't know the final hop count yet, and understating is
        # the safer error -- the real banded fare is applied exactly in
        # _build_option_data once the path is known.
        banded_board_fare = {vt: tiers[0][1]
                             for vt, tiers in BANDED_FARE_TIERS.items()}
        banded_bit = self._banded_bit
        avoid_set = set(avoid_route_ids) if avoid_route_ids else None

        def rebuild(node_idx):
            chain = []
            i = node_idx
            while i != -1:
                parent, stop_id, r, d = nodes[i]
                chain.append((stop_id, r, d))
                i = parent
            chain.reverse()
            # Callers expect (stop, route LEAVING that stop, direction),
            # but nodes store the edge INTO each stop -- so shift by one
            # and terminate with the (stop, None, None) sentinel.
            return [(chain[k][0], chain[k + 1][1], chain[k + 1][2])
                    for k in range(len(chain) - 1)] + [(chain[-1][0], None, None)]

        while queue:
            f, _, node_idx, cost, has_ridden_transit, banded_ridden, last_vtype = heappop(queue)
            _parent, current, into_route, into_direction = nodes[node_idx]

            # The heap is ordered by f, which lower-bounds the true total
            # of any route still to be explored. Once that lower bound
            # reaches the best complete answer already found, nothing
            # left in the queue can beat it.
            if f >= best_total:
                break

            if current in targets:
                total = cost + targets[current]
                if total < best_total:
                    best_path = rebuild(node_idx)
                    best_cost = cost
                    best_total = total
                # Deliberately NOT returning here: with several targets,
                # continuing through this one can still reach a different
                # target whose shorter last-mile walk more than pays for
                # the extra riding.

            prev_route = into_route
            prev_direction = into_direction
            state = (current, prev_route, prev_direction)

            if state in visited: continue
            visited.add(state)

            # has_ridden_transit, banded_ridden and last_vtype all used to
            # be recomputed here by scanning the whole path on every pop.
            # They are simple folds over the edges taken, so they now ride
            # along on the heap entry and are updated in O(1) when a
            # neighbour is pushed -- see the push site below.

            for neighbor, route_id, direction_id, time_sec in graph[current]:
                # Don't immediately walk back to the stop we just came
                # from. Previously `path[-1][0]`; with parent pointers the
                # previous stop is the parent node's stop.
                if _parent != -1 and neighbor == nodes[_parent][1]:
                    continue

                if route_id != "WALK":
                    if route_id in inactive_routes_today:
                        continue
                    if exclude_metro and (route_id in metro_ids or route_vtype.get(route_id) == 'metro'):
                        continue

                actual_time = time_sec

                is_walk = route_id == "WALK"
                if is_walk:
                    actual_time *= WALK_PENALTY_MULT
                elif (route_id in TRUSTED_ROUTE_SPEED_DISCOUNT
                      and current in TRUSTED_DISCOUNT_STOP_SET
                      and neighbor in TRUSTED_DISCOUNT_STOP_SET):
                    # Inlined from the old _discount_applies() helper: it
                    # was a Python function call on every one of those
                    # ~1.6M neighbours, almost always just to fail the
                    # first membership test.
                    actual_time *= TRUSTED_ROUTE_SPEED_DISCOUNT[route_id]

                penalty = actual_time

                if not is_walk:
                    if route_id in metro_ids:
                        penalty += METRO_PENALTY_SEC
                    if avoid_set and route_id in avoid_set:
                        penalty += 300

                # Same route_id but a DIFFERENT direction_id is not a
                # continuous ride -- it's the outbound trip handing off to
                # the return trip (or vice versa) at a shared/clustered
                # stop, which means physically disembarking and boarding a
                # different vehicle. Only same route_id AND same
                # direction_id counts as staying on the same run.
                same_run = (prev_route == route_id and prev_direction == direction_id)
                # A real transfer is: boarding a non-WALK run that isn't a
                # continuation of the one you're already on, AND you've
                # already ridden at least one vehicle before now (so this
                # isn't just your first boarding of the trip). This used
                # to also require prev_route != "WALK", which meant any
                # transfer reached by walking to a different stop -- the
                # normal case, since two different routes rarely share a
                # stop_id -- paid NO wait penalty at all, only the walk
                # time itself. That silently modeled every such transfer
                # as "arrive and board instantly," which is why multi-hop
                # paths kept coming out cheaper than they'd actually feel:
                # none of their transfers were paying for the time spent
                # waiting for the next vehicle.
                if not is_walk and not same_run:
                    headway_min = headway_map.get((route_id, direction_id)) or fallback_headway_min
                    wait = headway_min * 30.0
                    if wait > wait_cap_sec:
                        wait = wait_cap_sec

                    if has_ridden_transit:
                        new_vtype = route_vtype.get(route_id)
                        if new_vtype in GATED_RAIL_VTYPES:
                            floor = METRO_BOARDING_TRANSFER_SEC
                        else:
                            floor = TRANSFER_PENALTY_SEC
                        if new_vtype == 'microbus':
                            floor += MICROBUS_TRANSFER_EXTRA_SEC
                        if (new_vtype not in GATED_RAIL_VTYPES
                                and last_vtype not in GATED_RAIL_VTYPES):
                            floor += SURFACE_TRANSFER_EXTRA_SEC

                        penalty += wait if wait > floor else floor
                    else:
                        # First boarding wait: reflects the realistic time standing at the stop,
                        # ensuring search objective aligns with _build_option_data.
                        penalty += wait

                # Boarding penalty for routes in avoid_set (encourages alternative lines)
                if not is_walk and not same_run and avoid_set and route_id in avoid_set:
                    penalty += 1800

                # Money cost of transit, converted into time-equivalent seconds
                if FARE_WEIGHT_SEC_PER_EGP and not is_walk:
                    fare_vtype = route_vtype.get(route_id) or 'bus'
                    if not same_run:
                        bit = banded_bit.get(fare_vtype)
                        if bit is not None:
                            board_fare = (0 if banded_ridden & bit
                                          else banded_board_fare[fare_vtype])
                        elif fare_vtype == 'microbus':
                            board_fare = MICROBUS_MIN_FARE
                        else:
                            board_fare = fare_by_vtype.get(fare_vtype, default_fare)
                        penalty += board_fare * FARE_WEIGHT_SEC_PER_EGP
                    elif fare_vtype == 'microbus':
                        # Microbus incremental distance fare: accounts for distance-based pricing
                        c_s = self.stops[current]
                        n_s = self.stops[neighbor]
                        hop_km = self._haversine(c_s['lat'], c_s['lon'], n_s['lat'], n_s['lon']) / 1000.0
                        penalty += (hop_km * MICROBUS_FARE_PER_KM) * FARE_WEIGHT_SEC_PER_EGP

                new_cost = cost + penalty
                new_state = (neighbor, route_id, direction_id)
                if new_state in best_known and best_known[new_state] <= new_cost:
                    continue
                best_known[new_state] = new_cost

                is_transit = not is_walk
                nodes.append((node_idx, neighbor, route_id, direction_id))
                push_count += 1
                f = new_cost + heuristic(neighbor)
                heappush(queue, (
                    f, push_count, len(nodes) - 1, new_cost,
                    has_ridden_transit or is_transit,
                    (banded_ridden | banded_bit.get(route_vtype.get(route_id), 0)
                     if is_transit else banded_ridden),
                    (route_vtype.get(route_id) if is_transit else last_vtype),
                ))

        # Cost returned EXCLUDES the winning target's exit cost -- callers
        # add their own real last-mile leg (via OSRM) rather than the
        # estimate used to pick between candidates here.
        return best_path, best_cost

    def _build_option_data(self, path, profile_type, start_stop_id, start_lat, start_lon, end_lat, end_lon, start_walk, end_walk, lang="en"):
        segments = []
        instructions = []
        station_markers = []

        segments.append({
            "color": "#83C5BE",
            "is_walk": True,
            "vehicle_type": "walk",
            "points": start_walk["geometry"]
        })
        instructions.append({
            "action": "walk_to_station",
            "vehicle_type": "walk",
            "route_number": None,
            "route_description": None,
            "distance_m": round(start_walk['distance_m']),
            "lat": start_lat, "lon": start_lon,
            "station": self._localized_stop_name(start_stop_id, lang)
        })

        current_route = None
        current_direction = None
        transit_points = []
        transit_distance = 0
        total_transit_time_sec = 0
        
        metro_stops = 0
        banded_hops = defaultdict(int)
        first_banded_instruction = {}
        current_leg_instruction = None
        current_leg_distance_m = 0
        current_leg_board_stop = None
        total_fare = 0
        boarding_count = 0
        total_wait_sec = 0.0

        for i in range(len(path) - 1):
            curr_stop, route, direction = path[i]
            next_stop = path[i + 1][0]
            stop_info = self.stops[curr_stop]
            next_info = self.stops[next_stop]

            if route == "WALK":
                walk_geom = self.osrm.walking_route(
                    stop_info['lat'], stop_info['lon'],
                    next_info['lat'], next_info['lon']
                ).get('geometry', [])
                if walk_geom:
                    if transit_points and transit_points[-1] == walk_geom[0]:
                        transit_points.extend(walk_geom[1:])
                    else:
                        transit_points.extend(walk_geom)
                else:
                    transit_points.append({"lat": stop_info['lat'], "lon": stop_info['lon']})
            else:
                hop_shape = self._shape_segment(route, curr_stop, next_stop, direction_id=direction)
                if transit_points and hop_shape and transit_points[-1] == hop_shape[0]:
                    transit_points.extend(hop_shape[1:])
                else:
                    transit_points.extend(hop_shape)
            station_markers.append({"lat": stop_info['lat'], "lon": stop_info['lon']})

            hop_dist = self._haversine(stop_info['lat'], stop_info['lon'], next_info['lat'], next_info['lon'])
            transit_distance += hop_dist
            edge_time = next((t for n, r, d, t in self.graph[curr_stop]
                               if n == next_stop and r == route and d == direction), 10)
            total_transit_time_sec += edge_time

            if route != "WALK" and route in self.metro_route_ids:
                metro_stops += 1
            if route != "WALK":
                hop_vtype = self._route_vtype.get(route)
                if hop_vtype in BANDED_FARE_TIERS:
                    banded_hops[hop_vtype] += 1

            if (route, direction) != (current_route, current_direction):
                if current_leg_instruction is not None and current_route not in (None, "WALK"):
                    leg_fare = self._estimate_leg_fare(current_route, current_leg_distance_m)
                    current_leg_instruction["fare_egp"] = leg_fare
                    total_fare += leg_fare
                    if current_leg_board_stop is not None:
                        current_leg_instruction["boarding_options"] = self._find_equivalent_routes(
                            current_leg_board_stop, curr_stop, current_route, current_direction, lang=lang,
                        )
                current_leg_distance_m = 0

                route_desc = self._describe_route(route, lang)
                if current_route is not None:
                    prev_vtype = "walk" if current_route == "WALK" else self.routes.get(current_route, {}).get('vehicle_type', 'bus')
                    segments.append({
                        "color": self._get_route_color(prev_vtype) if current_route != "WALK" else "#2A9D8F",
                        "is_walk": current_route == "WALK",
                        "vehicle_type": prev_vtype,
                        "points": transit_points,
                    })
                    if route == "WALK":
                        instructions.append({
                            "action": "walk_to_transfer",
                            "vehicle_type": "walk",
                            "route_number": None, "route_description": None,
                            "distance_m": None, "fare_egp": None,
                            "lat": stop_info['lat'], "lon": stop_info['lon'],
                            "station": self._localized_stop_name(curr_stop, lang)
                        })
                        current_leg_instruction = None
                    else:
                        transfer_instr = {
                            "action": "transfer",
                            "vehicle_type": route_desc["vehicle_type"],
                            "route_number": route_desc["route_number"],
                            "route_description": route_desc["route_description"],
                            "route_id": route,
                            "direction_id": direction,
                            "distance_m": None, "fare_egp": None,
                            "lat": stop_info['lat'], "lon": stop_info['lon'],
                            "station": self._localized_stop_name(curr_stop, lang)
                        }
                        instructions.append(transfer_instr)
                        current_leg_instruction = transfer_instr
                        current_leg_board_stop = curr_stop
                        boarding_count += 1
                        wait_sec = self._expected_wait_sec(route, direction)
                        total_wait_sec += wait_sec
                        transfer_instr["wait_min"] = round(wait_sec / 60)
                    transit_points = [{"lat": stop_info['lat'], "lon": stop_info['lon']}]
                else:
                    board_instr = {
                        "action": "continue_walk" if route == "WALK" else "board",
                        "vehicle_type": route_desc["vehicle_type"],
                        "route_number": route_desc["route_number"],
                        "route_description": route_desc["route_description"],
                        "route_id": route if route != "WALK" else None,
                        "direction_id": direction if route != "WALK" else None,
                        "distance_m": None, "fare_egp": None,
                        "lat": stop_info['lat'], "lon": stop_info['lon'],
                        "station": self._localized_stop_name(curr_stop, lang)
                    }
                    instructions.append(board_instr)
                    current_leg_instruction = board_instr if route != "WALK" else None
                    if route != "WALK":
                        current_leg_board_stop = curr_stop
                        boarding_count += 1
                        wait_sec = self._expected_wait_sec(route, direction)
                        total_wait_sec += wait_sec
                        board_instr["wait_min"] = round(wait_sec / 60)
                if current_leg_instruction is not None and route != "WALK":
                    board_vtype = self._route_vtype.get(route)
                    if (board_vtype in BANDED_FARE_TIERS
                            and board_vtype not in first_banded_instruction):
                        first_banded_instruction[board_vtype] = current_leg_instruction

                current_route = route
                current_direction = direction

            if route not in (None, "WALK"):
                current_leg_distance_m += hop_dist

        last_stop = path[-1][0]
        if current_leg_instruction is not None and current_route not in (None, "WALK"):
            leg_fare = self._estimate_leg_fare(current_route, current_leg_distance_m)
            current_leg_instruction["fare_egp"] = leg_fare
            total_fare += leg_fare
            if current_leg_board_stop is not None:
                current_leg_instruction["boarding_options"] = self._find_equivalent_routes(
                    current_leg_board_stop, last_stop, current_route, current_direction, lang=lang,
                )

        transit_points.append({"lat": self.stops[last_stop]['lat'], "lon": self.stops[last_stop]['lon']})
        station_markers.append({"lat": self.stops[last_stop]['lat'], "lon": self.stops[last_stop]['lon']})
        last_vtype = "walk" if current_route == "WALK" else self.routes.get(current_route, {}).get('vehicle_type', 'bus')
        segments.append({
            "color": self._get_route_color(last_vtype) if current_route != "WALK" else "#2A9D8F",
            "is_walk": current_route == "WALK",
            "vehicle_type": last_vtype,
            "points": transit_points,
        })
        
        instructions.append({
            "action": "arrive",
            "vehicle_type": None, "route_number": None, "route_description": None,
            "distance_m": None, "fare_egp": None,
            "lat": self.stops[last_stop]['lat'], "lon": self.stops[last_stop]['lon'],
            "station": self._localized_stop_name(last_stop, lang)
        })

        segments.append({
            "color": "#83C5BE",
            "is_walk": True,
            "vehicle_type": "walk",
            "points": end_walk["geometry"]
        })
        instructions.append({
            "action": "walk_to_destination",
            "vehicle_type": "walk", "route_number": None, "route_description": None,
            "distance_m": round(end_walk['distance_m']), "fare_egp": None,
            "lat": end_lat, "lon": end_lon,
            "station": None
        })

        total_time_mins = math.ceil(
            (start_walk['duration_sec'] + total_transit_time_sec
             + total_wait_sec + end_walk['duration_sec']) / 60
        )
        total_distance = round(start_walk['distance_m'] + transit_distance + end_walk['distance_m'])

        # Banded modes use a hop-count aggregate rather than per-leg
        # distance, since their fare tiers are defined by total stops ridden
        # on that mode across the whole journey -- even across a transfer
        # between two of its lines -- not by distance or per-boarding like
        # bus/minibus. Tier tables and the reasoning behind their boundaries
        # live in BANDED_FARE_TIERS.
        #
        # Charged once PER MODE, not once per journey: metro to Adly Mansour
        # then LRT then monorail is three tickets, and pooling those hops
        # into one band would have quoted a rider roughly a third of what
        # they will actually be asked to pay.
        #
        # Each mode's fare is attached to the step where the rider buys it,
        # so the per-step figures on the recap screen sum to the total shown
        # on the card. Before this existed for the metro, a confirmed real
        # case showed "Minibus 66: 14 EGP" + "Metro 3: 0 EGP" against a
        # 24 EGP total, with the missing 10 EGP unexplained anywhere in the
        # UI. Only the FIRST leg of each mode gets it, so a Line 1 -> Line 3
        # metro change correctly still shows 0 on the second metro leg.
        for vtype, hops in banded_hops.items():
            mode_fare = banded_journey_fare(vtype, hops)
            if not mode_fare:
                continue
            total_fare += mode_fare
            instr = first_banded_instruction.get(vtype)
            if instr is not None:
                instr["fare_egp"] = mode_fare

        # NO per-tier price fudging here, deliberately. This used to read
        #     if profile_type == "Fastest": base_price += 5
        #     if profile_type == "Cheapest": base_price = max(5, base_price - 5)
        # which fabricated a 5 EGP swing on EVERY option purely from its
        # tier LABEL, not from anything about the actual route: the
        # Fastest card showed 29 EGP for a trip whose own legs summed to
        # 24, and Cheapest looked 5 EGP cheaper than it really was. That
        # both misinforms the rider and visibly contradicts the per-step
        # breakdown right underneath it. This is the same class of bug as
        # the tier TIME fudge fixed in the 2026-08-21 session (see
        # test_independently_computed_tie_is_not_fabricated) -- a
        # genuinely computed option keeps its real numbers even when two
        # tiers happen to land on the same price. The separate
        # duplicate-padding nudge in run_raptor_by_coords still applies,
        # but only to options that are literal copies of another tier
        # because no distinct real path existed for them.
        base_price = max(5, total_fare)

        # A short summary description built from the FIRST transit leg's
        # vehicle/route info, used for the route-option card subtitle.
        first_transit_route = next((r for _, r, _ in path if r and r != "WALK"), None)
        summary = self._describe_route(first_transit_route, lang) if first_transit_route else {"vehicle_type": "walk", "route_number": None, "route_description": None}

        return {
            "type": profile_type,
            "raw_time": total_time_mins,
            # Broken out so the app can show "45 min riding + 22 min
            # waiting" if it wants to, without having to re-derive it from
            # the per-instruction wait_min values. Both are already
            # INCLUDED in raw_time -- they are a breakdown of it, not
            # something to add on top.
            "wait_min": round(total_wait_sec / 60),
            "riding_min": total_time_mins - round(total_wait_sec / 60),
            "raw_price": base_price,
            "fare_total_egp": total_fare,
            "desc": f"Via {self._localized_stop_name(start_stop_id, lang)}",
            "vehicle_type": summary["vehicle_type"],
            "route_number": summary["route_number"],
            "route_description": summary.get("route_description"),
            "distance_m": total_distance,
            "metro_stops": metro_stops,
            "segments": segments,
            "instructions": instructions,
            "station_markers": station_markers
        }

    def _build_walk_only_option(self, start_lat, start_lon, end_lat, end_lon, lang="en"):
        """A direct walking option, bypassing transit entirely. Offered
        when it's genuinely competitive with (or the only viable
        alternative to) transit for short trips -- see run_raptor_by_coords."""
        walk = self.osrm.walking_route(start_lat, start_lon, end_lat, end_lon)
        return {
            "type": "Walk",
            "raw_time": math.ceil(walk['duration_sec'] / 60),
            "raw_price": 0,
            "desc": "Direct walking route",
            "vehicle_type": "walk",
            "route_number": None,
            "distance_m": round(walk['distance_m']),
            "segments": [{"color": "#2A9D8F", "points": walk["geometry"]}],
            "instructions": [{
                "action": "walk_to_destination",
                "vehicle_type": "walk", "route_number": None, "route_description": None,
                "distance_m": round(walk['distance_m']), "fare_egp": None,
                "lat": end_lat, "lon": end_lon,
                "station": None
            }],
            "station_markers": []
        }

    def _expected_wait_sec(self, route_id, direction_id, floor_sec=0.0):
        """How long a rider actually stands there before this vehicle
        turns up, in seconds.

        Half the headway is the standard result for a rider who arrives
        without consulting a timetable -- which is the only realistic
        assumption for most of this network, where the "timetable" is a
        headway band in frequencies.txt rather than departure times.

        `floor_sec` carries the hand-tuned transfer penalties forward as a
        LOWER BOUND rather than replacing them. Those constants
        (TRANSFER_PENALTY_SEC, MICROBUS_TRANSFER_EXTRA_SEC, the metro's
        halved boarding cost) were each tuned against a specific reported
        routing bug -- the Minibus 66 corridor case among them -- and they
        cover more than waiting: disembarking, crossing the road, finding
        where the next vehicle actually stops, and a deliberate reluctance
        to chase small differences between two routes whose timetables
        disagree by more than the saving. Real headway data can say a wait
        is LONGER than we assumed. It should not be able to argue a
        transfer is cheaper than the tuning already established.
        """
        headway_min = self.route_typical_headway_min.get((route_id, direction_id))
        if not headway_min:
            headway_min = self.FALLBACK_HEADWAY_MIN
        wait = min(headway_min * 30.0, self.EXPECTED_WAIT_CAP_SEC)  # *30 = /2 then *60
        return max(wait, floor_sec)

    def _walk_only_is_plausible(self, walk_distance_m, direct_distance_m):
        """Is OSRM's walking distance believable enough to put in front of
        a rider as a recommendation?

        Two independent tests, both of which must pass:

          1. An absolute ceiling. Nobody opens a transit app to be told to
             walk 9 km, however real that walk is.
          2. Circuity. A credible urban walk is roughly 1.1-1.5x the
             straight line; Google's answer for the AASTMT case is 1.22x.
             Ours was 2.76x, which is the signature of the foot graph
             detouring around an unmapped crossing rather than of a
             genuinely winding street.

        The slack term keeps short trips honest: a 120 m straight line can
        legitimately need a 300 m walk around one block, which is 2.5x but
        entirely reasonable. Circuity only starts to mean anything once
        the trip is long enough for the ratio to be signal rather than
        noise.

        Note what this deliberately does NOT do: substitute a haversine
        estimate for the distance it rejected. A long walk can be long for
        a real reason -- a river, a rail corridor, a closed military
        perimeter -- and quietly replacing it with a straight line would
        reintroduce exactly the "walk 500 m over the Nile" bug the Zamalek
        guard exists to prevent. Rejecting means dropping the option, not
        rewriting it.
        """
        if walk_distance_m > self.WALK_ONLY_MAX_REAL_M:
            return False
        allowed = max(
            direct_distance_m * self.WALK_ONLY_MAX_CIRCUITY,
            direct_distance_m + self.WALK_CIRCUITY_SLACK_M,
        )
        return walk_distance_m <= allowed

    # --- Partial-coverage fallback -------------------------------------
    #
    # This feed is an incomplete extract of a network that is itself only
    # partly formalised: 502 of the CTA minibus numbers between 1 and 606
    # are absent, and OpenStreetMap has exactly one bus relation for all
    # of Greater Cairo, so there is no second source to fill the holes
    # with. "No route found" is therefore not a rare edge case here, it
    # is the rider's normal experience for any trip that crosses a gap --
    # and a dead-end error screen teaches them the app doesn't work.
    #
    # So when the real search fails, get them as far as the network
    # honestly goes and SAY what isn't covered. A rider who is told
    # "this minibus gets you to Kilo 4 & Half, the last 2.4 km isn't
    # covered yet" can finish the trip themselves. A rider shown
    # "we couldn't find a way to get there" cannot.

    # How many stops around the destination are offered to the search as
    # possible exit points. Deliberately far wider than
    # DESTINATION_CANDIDATE_K: the stop we want is by definition NOT one
    # of the four nearest to a destination the normal search just failed
    # to reach.
    PARTIAL_EXIT_CANDIDATES = 200
    # A partial answer must leave the rider meaningfully closer than they
    # started. At 0.75, an option whose uncovered tail is more than
    # three-quarters of the original straight-line trip is suppressed --
    # a bus that drops you almost exactly where you began is worse than
    # an honest "we don't cover this", because it looks like an answer.
    PARTIAL_MIN_PROGRESS = 0.75
    # And the tail has to be walkable-or-taxiable. Past this the option
    # is just a long ride ending in an impossible walk.
    PARTIAL_MAX_UNCOVERED_M = 15000

    def _build_partial_option(self, start_stop_id, start_walk, start_lat, start_lon,
                              end_lat, end_lon, direct_distance_m,
                              today_day, now_sec, lang="en", exclude_metro=False,
                              min_walk=False, min_transfers=False):
        """Best transit option that gets CLOSE to an unreachable
        destination, or None if even that isn't worth showing.

        Reuses the ordinary path search rather than inventing a second
        algorithm: the only difference from the normal last-mile search
        is the size of the exit-candidate set and the absence of a cap on
        how far the final walk may be. The search itself still trades
        riding time against walking time exactly as it always does, so
        the stop it picks is the genuine best compromise and not merely
        the one closest to the destination as the crow flies.
        """
        if start_stop_id is None:
            return None

        # Canonical stops only -- non-canonical (clustered-away) ids have
        # no graph edges after _build_stop_clusters, so offering one as an
        # exit point would silently make it unreachable.
        ranked = sorted(
            (
                (self._haversine(s['lat'], s['lon'], end_lat, end_lon), sid)
                for sid, s in self.stops.items()
                if self.stop_alias.get(sid, sid) == sid
            ),
            key=lambda x: x[0],
        )[: self.PARTIAL_EXIT_CANDIDATES]
        if not ranked:
            return None

        # Straight-line here on purpose: this is a selection pass over
        # hundreds of stops, and only the winner gets a real OSRM walk.
        walk_mult = 3.5 if min_walk else 1.0
        exit_costs = {
            sid: max(dist_m / self.WALK_SPEED_MPS, 10) * walk_mult
            for dist_m, sid in ranked
            if sid != start_stop_id
        }
        if not exit_costs:
            return None

        path, _ = self._find_shortest_path(
            start_stop_id, exit_costs, profile="fastest",
            today_day=today_day, now_sec=now_sec,
            exclude_metro=exclude_metro,
            min_walk=min_walk, min_transfers=min_transfers,
        )
        # len < 2 means the search "arrived" without boarding anything --
        # that is a walk, not a partial route, and the walk-only option
        # already had its chance above.
        if not path or len(path) < 2:
            return None

        end_sid = path[-1][0]
        stop = self.stops[end_sid]
        end_walk = self.osrm.walking_route(stop['lat'], stop['lon'], end_lat, end_lon)
        uncovered_m = end_walk['distance_m']

        if uncovered_m > self.PARTIAL_MAX_UNCOVERED_M:
            return None
        if uncovered_m > direct_distance_m * self.PARTIAL_MIN_PROGRESS:
            return None

        # start_walk is None when the destination never snapped at all,
        # because the block that computes it was skipped entirely.
        if start_walk is None:
            start_walk = self.osrm.walking_route(
                start_lat, start_lon,
                self.stops[start_stop_id]['lat'], self.stops[start_stop_id]['lon'],
            )

        opt = self._build_option_data(
            path, "Partial", start_stop_id, start_lat, start_lon,
            end_lat, end_lon, start_walk, end_walk, lang=lang,
        )
        # The app needs both facts to render this honestly: that the
        # option is incomplete, and exactly how much of the trip it does
        # not cover. Neither is inferable from the instruction list --
        # a long final walk looks identical to a legitimate one.
        opt['partial'] = True
        opt['uncovered_m'] = round(uncovered_m)
        opt['last_covered_stop'] = self._localized_stop_name(end_sid, lang)
        return opt

    def _nearest_hub_names(self, start_lat, start_lon, end_lat, end_lon, lang="en"):
        """Names of the closest covered stops to each end, for the case
        where not even a partial route exists. Being told the nearest
        point the network reaches at all is the difference between "this
        app is broken" and "this app doesn't go there yet"."""
        def nearest(lat, lon):
            best = None
            for sid, s in self.stops.items():
                if self.stop_alias.get(sid, sid) != sid:
                    continue
                d = self._haversine(s['lat'], s['lon'], lat, lon)
                if best is None or d < best[0]:
                    best = (d, sid)
            if best is None:
                return None
            return {
                "name": self._localized_stop_name(best[1], lang),
                "distance_m": round(best[0]),
                "lat": self.stops[best[1]]['lat'],
                "lon": self.stops[best[1]]['lon'],
            }

        return {"from_origin": nearest(start_lat, start_lon),
                "from_destination": nearest(end_lat, end_lon)}

    def run_raptor_by_coords(self, start_lat, start_lon, end_lat, end_lon, lang="en", now=None, exclude_metro=False, min_walk=False, min_transfers=False):
        """`now` is injectable so callers -- and the test suite -- can ask
        "what does this trip look like at 09:00?" rather than being at the
        mercy of the wall clock. Routes are filtered against their declared
        service window, so a query at 04:00 legitimately returns nothing,
        which made the suite fail whenever it ran overnight."""
        calc_start_time = time.time()
        direct_distance_m = self._haversine(start_lat, start_lon, end_lat, end_lon)

        orig_candidates = self._find_nearest_stop_candidates(start_lat, start_lon, k=3)
        start_stop_id = orig_candidates[0][0] if orig_candidates else None
        # Multiple candidate destination stops, not just the single
        # nearest -- see _find_nearest_stop_candidates for why: the
        # real-nearest stop by walking distance isn't always the stop
        # that gives the best OVERALL trip (a rider legitimately gets
        # off one stop early and walks farther when that stop has a much
        # better route). DESTINATION_CANDIDATE_K bounds how many of
        # these get a full path search per profile -- kept modest since
        # each candidate costs a full Dijkstra run per profile.
        end_candidates = self._find_nearest_stop_candidates(end_lat, end_lon, k=self.DESTINATION_CANDIDATE_K)

        # Neither point has any GTFS stop within a reasonable walking
        # distance (find_nearest_stop/_find_nearest_stop_candidates
        # return nothing past MAX_SNAP_DISTANCE_M rather than snapping to
        # something absurdly far away), or the two points are close
        # enough together that routing through transit at all would be
        # pointless -- either way, skip straight to a walk-only answer
        # instead of computing a nonsensical "transit" option built on a
        # first/last-mile walk of several hundred km.
        no_stop_nearby = not orig_candidates or not end_candidates
        trivially_close = direct_distance_m <= 80  # ~a minute's walk; not worth involving transit at all

        final_options = []
        start_walk = None

        # Hoisted out of the transit block below because the no-options
        # error path at the end of this method also needs now_sec, to tell
        # "nothing runs at this hour" apart from "we don't cover here".
        now = now or datetime.datetime.now()
        today_day = now.strftime('%A').lower()
        # Seconds since local midnight, so routes outside their
        # frequencies.txt hours are excluded -- see
        # _routes_not_running_at. Without this the engine quoted a normal
        # daytime trip duration at 02:00 for routes whose first vehicle
        # was hours away.
        now_sec = now.hour * 3600 + now.minute * 60 + now.second

        if not no_stop_nearby and not trivially_close:
            min_orig_dist = orig_candidates[0][1] if orig_candidates else 0
            valid_origs = [c for c in orig_candidates if c[1] <= 600 or c[1] <= min_orig_dist + 200]

            # The three base profiles:
            # 1. "Recommended": Balanced composite calculation offering the best bang for buck
            #    (reasonable fare, low transfer friction, smooth transit).
            # 2. "Fastest": Absolute fastest route (minimal elapsed door-to-door duration).
            # 3. "Cheapest": Absolute lowest fare in EGP (lexicographic fare minimization).
            profiles = [("Recommended", "recommended"), ("Fastest", "fastest"), ("Cheapest", "cheapest")]

            # Path-finding (Dijkstra per profile) is CPU-bound and stays
            # sequential -- three independent runs over the same engine
            # state, not worth the complexity of multiprocessing here. But
            # the walking-direction lookup for each resulting path's two
            # endpoints IS independent, I/O-bound work, and running all of
            # it one call at a time was the single biggest reason trip
            # cards could take several seconds to populate (see
            # OSRM_FALLBACK_TIMEOUT_SEC and walking_routes_batch's
            # docstring). So: find every profile's path first, THEN
            # resolve every walking leg for all of them in one concurrent
            # batch, THEN build the option cards.
            profile_paths = []
            for display_name, p in profiles:
                # Hand the search every destination candidate and origin candidate at once,
                # each priced with walking duration scaled by WALK_PENALTY_MULT.
                walk_mult = 3.5 if min_walk else (1.6 if p == "recommended" else 1.0)
                exit_costs = {
                    sid: max(real_dist_m / self.WALK_SPEED_MPS, 10) * walk_mult
                    for sid, real_dist_m in end_candidates
                }
                start_costs = {
                    sid: max(real_dist_m / self.WALK_SPEED_MPS, 10) * walk_mult
                    for sid, real_dist_m in valid_origs
                }
                path, _ = self._find_shortest_path(
                    start_costs, exit_costs, profile=p, today_day=today_day, now_sec=now_sec,
                    exclude_metro=exclude_metro, min_walk=min_walk, min_transfers=min_transfers,
                )
                if path:
                    profile_paths.append((display_name, path))

            walk_pairs = []
            for display_name, path in profile_paths:
                win_start_sid = path[0][0]
                win_end_sid = path[-1][0]
                walk_pairs.append((
                    start_lat, start_lon,
                    self.stops[win_start_sid]['lat'], self.stops[win_start_sid]['lon'],
                ))
                walk_pairs.append((
                    self.stops[win_end_sid]['lat'], self.stops[win_end_sid]['lon'],
                    end_lat, end_lon,
                ))
            walk_results = self.osrm.walking_routes_batch(walk_pairs)

            for i, (display_name, path) in enumerate(profile_paths):
                win_start_sid = path[0][0]
                win_start_walk = walk_results[i * 2]
                win_end_walk = walk_results[i * 2 + 1]
                opt = self._build_option_data(
                    path, display_name, win_start_sid,
                    start_lat, start_lon, end_lat, end_lon,
                    win_start_walk, win_end_walk, lang=lang
                )
                final_options.append(opt)

            # Complementary alternative search:
            # If we found at least one transit option, see if an alternative transit
            # route exists that serves the corridor using distinct lines/modes.
            primary_routes = set()
            for opt in final_options:
                for inst in opt.get('instructions', []):
                    rid = inst.get('route_id')
                    if rid and inst.get('action') in ('board', 'transfer'):
                        primary_routes.add(rid)

            if primary_routes:
                walk_mult = 3.5 if min_walk else 1.0
                exit_costs = {
                    sid: max(real_dist_m / self.WALK_SPEED_MPS, 10) * walk_mult
                    for sid, real_dist_m in end_candidates
                }
                start_costs = {
                    sid: max(real_dist_m / self.WALK_SPEED_MPS, 10) * walk_mult
                    for sid, real_dist_m in valid_origs
                }
                alt_path, _ = self._find_shortest_path(
                    start_costs, exit_costs, profile="fastest",
                    today_day=today_day, now_sec=now_sec,
                    exclude_metro=exclude_metro,
                    avoid_route_ids=primary_routes,
                    min_walk=min_walk,
                    min_transfers=min_transfers,
                )
                if alt_path and len(alt_path) >= 2:
                    alt_start_sid = alt_path[0][0]
                    alt_end_sid = alt_path[-1][0]
                    alt_start_walk, alt_end_walk = self.osrm.walking_routes_batch([
                        (start_lat, start_lon,
                         self.stops[alt_start_sid]['lat'], self.stops[alt_start_sid]['lon']),
                        (self.stops[alt_end_sid]['lat'], self.stops[alt_end_sid]['lon'],
                         end_lat, end_lon),
                    ])
                    alt_opt = self._build_option_data(
                        alt_path, "Alternative", alt_start_sid,
                        start_lat, start_lon, end_lat, end_lon,
                        alt_start_walk, alt_end_walk, lang=lang
                    )
                    alt_routes = {
                        inst.get('route_id')
                        for inst in alt_opt.get('instructions', [])
                        if inst.get('route_id') and inst.get('action') in ('board', 'transfer')
                    }
                    if alt_routes and not alt_routes.issubset(primary_routes):
                        min_raw_time = min(o['raw_time'] for o in final_options if 'raw_time' in o)
                        if alt_opt['raw_time'] <= min_raw_time * 1.8 + 900:
                            final_options.append(alt_opt)

        # Straight-line distance for the whole trip, used to sanity-check
        # whether the best transit option is a reasonable detour ratio or
        # a nonsensical one forced by sparse graph connectivity.

        should_offer_walk = direct_distance_m <= self.DIRECT_WALK_MAX_M
        if final_options:
            best_transit_distance = min(o['distance_m'] for o in final_options)
            detour_ratio = best_transit_distance / max(direct_distance_m, 1)
            # Transit's real distance is a bad multiple of the direct
            # distance -- this is exactly the "10km trip for a 2km
            # destination" failure mode. Surface walking as a genuine
            # alternative rather than silently forcing the bad route.
            if detour_ratio > 2.2:
                should_offer_walk = True
        else:
            # No transit path at all -- walking may be the only option.
            should_offer_walk = direct_distance_m <= self.DIRECT_WALK_MAX_M * 1.5

        if should_offer_walk:
            walk_option = self._build_walk_only_option(start_lat, start_lon, end_lat, end_lon, lang=lang)
            # Everything above decides on the STRAIGHT-LINE distance. This
            # is the only place we know what the rider would actually be
            # asked to walk, so the final say belongs here -- and note
            # that the detour_ratio branch above sets should_offer_walk
            # unconditionally, with no distance bound of its own, so
            # without this gate a badly-connected transit answer could
            # offer an arbitrarily long walk as its "alternative".
            if self._walk_only_is_plausible(walk_option['distance_m'], direct_distance_m):
                final_options.append(walk_option)
            else:
                print(f"[walk] Dropped walk-only option: OSRM says "
                      f"{walk_option['distance_m']:.0f}m for a {direct_distance_m:.0f}m "
                      f"straight line ({walk_option['distance_m'] / max(direct_distance_m, 1):.2f}x) "
                      f"-- not credible, see _walk_only_is_plausible.")

        if not final_options:
            # Nothing complete exists. Before giving up entirely, try to
            # get the rider as close as the network actually goes -- see
            # _build_partial_option for why that matters more here than
            # it would on a complete feed. Appended to final_options
            # rather than returned directly so it goes through the same
            # tier sort, dedupe and price/time formatting as every other
            # option; a second return path here is how response shapes
            # drift apart.
            partial = self._build_partial_option(
                start_stop_id, start_walk, start_lat, start_lon,
                end_lat, end_lon, direct_distance_m,
                today_day, now_sec, lang=lang,
                exclude_metro=exclude_metro,
                min_walk=min_walk,
                min_transfers=min_transfers,
            )
            if partial:
                final_options.append(partial)

        if not final_options:
            # Distinguish "nothing runs at this hour" from "we don't cover
            # this place". Both used to return the same coverage-area
            # message, which is actively misleading at 02:00 -- the route
            # exists and will run in a few hours, and telling the rider
            # they're outside the coverage area invites them to give up
            # on the app entirely for a trip it handles fine at 09:00.
            if not no_stop_nearby:
                closed_now = self._routes_not_running_at(now_sec)
                serviced = len(self.route_service_windows)
                if serviced and len(closed_now) >= serviced:
                    return {
                        "success": False,
                        "error": "No services are running at this hour. Most routes in this "
                                 "network run roughly 06:00-23:00 -- try again during service hours.",
                        "reason": "outside_service_hours",
                    }
            return {
                "success": False,
                "error": "No viable route found -- this trip may be outside our coverage area.",
                "reason": "no_coverage",
                # Not decoration: naming the closest point the network
                # does reach turns "broken" into "doesn't go there yet",
                # and gives the rider somewhere to aim for.
                "nearest_hubs": self._nearest_hub_names(start_lat, start_lon, end_lat, end_lon, lang=lang),
            }

        # ONE CARD PER DISTINCT ROUTE. No padding, no copies, no fudging.
        #
        # This used to pad a missing tier with a literal relabeled copy of
        # another option and then nudge its time and price apart so the
        # two cards didn't look identical. That is fabricated data shown
        # to a rider as fact -- the same class of bug as the +/-5 EGP tier
        # fudge and the tier TIME fudge, both removed earlier. The last
        # instance goes here.
        #
        # Two tiers genuinely landing on the same route is normal and
        # expected: on the Maadi -> Zamalek trip the metro is honestly
        # both the fastest AND the cheapest way to go. The truthful
        # presentation of that is one card, not two identical ones (which
        # is also why "Regular" was dropped -- see the profiles comment).
        # So: collapse exact duplicates, keep whichever tier ranks first,
        # and let the app show a single option when only one real route
        # exists.
        def _route_signature(opt):
            return (
                tuple((i['action'], i.get('route_id'), i.get('direction_id'), i.get('station'))
                      for i in opt.get('instructions', [])),
                opt.get('fare_total_egp'),
                opt.get('raw_time'),
            )

        tier_order = {"Recommended": 0, "Fastest": 1, "Cheapest": 2, "Alternative": 3, "Walk": 4, "Partial": 5}
        final_options.sort(key=lambda x: tier_order.get(x['type'], 9))

        deduped, seen_signatures = [], set()
        for opt in final_options:
            if opt['type'] == 'Walk':
                deduped.append(opt)
                continue
            sig = _route_signature(opt)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            deduped.append(opt)
        final_options = deduped

        for opt in final_options:
            opt['time'] = str(opt['raw_time'])
            # 'price' is a pre-formatted ENGLISH string and is kept only so
            # an older build of the app doesn't lose its fare display. It is
            # deprecated: a backend has no business formatting a display
            # string it cannot localize, and an Arabic rider seeing "20 EGP"
            # in the middle of an Arabic screen is the visible cost of it.
            #
            # 'fare_egp' is the number. The app formats it, in whichever
            # language it is running in. It used to be deleted here, which
            # left the app with no choice but to print the English string.
            opt['price'] = "Free" if opt['type'] == 'Walk' else f"{opt['raw_price']} EGP"
            opt['fare_egp'] = 0 if opt['type'] == 'Walk' else opt['raw_price']
            del opt['raw_time']
            del opt['raw_price']
            opt.pop('_is_duplicate', None)

        transit_options = [o for o in final_options if o['type'] not in ('Walk', 'Partial')]
        is_only_route = len(transit_options) == 1 or len(final_options) == 1

        return {
            "success": True,
            "backend_compute_time_ms": round((time.time() - calc_start_time) * 1000, 2),
            "is_only_route": is_only_route,
            "options": final_options
        }

if __name__ == "__main__":
    engine = GTFSRaptorEngine(".")
    engine.load_data()
