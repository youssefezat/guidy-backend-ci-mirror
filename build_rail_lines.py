"""
Turn the raw OSM extract into a small, reviewed description of the Greater
Cairo rail modes Guidy's GTFS feed is missing.

WHAT IS IN AND WHAT IS OUT, AND WHY
-----------------------------------
IN -- these have a complete ordered station sequence in OSM, real
coordinates, published fares and published operating hours:

  * East Nile Monorail   22 stations, opened to passengers 6 May 2026
  * Cairo LRT            12 stations over two branches, opened 3 July 2022
  * Cairo Airport APM    4 stops, free inter-terminal shuttle

OUT -- present in OSM but NOT to a standard that justifies routing on it:

  * Egyptian National Railways. The `route=train` relations for Egypt carry
    3-17 node members against lines with dozens of stations; they are drawn
    as ways without an enumerated stop sequence. There is no open timetable.
    Publishing a suburban rail service off this would mean inventing
    departure times, which is the exact failure mode the feed's own sanity
    checks exist to catch.
  * Alexandria tram. Out of the chosen coverage area, and the Raml line shut
    for reconstruction in January 2026 -- the mapped network describes a
    service that is not running.
  * Ferries. The only ferry relation in the extract is Nuweiba-Aqaba, an
    international Red Sea crossing, not Cairo river transport.
  * West Nile (October) Monorail. Under construction, no passenger service.

STATION NAMING
--------------
OSM's name:en is preferred, with name:ar carried through for the app's
Arabic locale. Press coverage of the monorail uses a completely different
set of names for several stations (One Ninety appears as "Cairo Festival",
Al Lotus as "Mohammed Naguib"); OSM's are used because they match the
station signage and because they come with coordinates attached.
"""

import json
import sys
from math import radians, sin, cos, asin, sqrt

RAW = sys.argv[1] if len(sys.argv) > 1 else "osm_transit_raw.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "gcr_rail_lines.json"

raw = json.load(open(RAW, encoding="utf-8"))
ROUTES = {r["id"]: r for r in raw["routes"]}
NODES = raw["nodes"]


def node(ref):
    return NODES.get(str(ref)) or NODES.get(ref)


def seq(rel_id):
    """Ordered station list for a route relation."""
    out = []
    for m in ROUTES[rel_id]["members"]:
        if m["type"] != "n":
            continue
        n = node(m["ref"])
        if not n or n["lat"] is None:
            continue
        t = n["tags"]
        out.append({
            "osm_node": n["id"],
            "name_en": t.get("name:en") or t.get("name") or f"node {n['id']}",
            "name_ar": t.get("name:ar") or "",
            "lat": round(n["lat"], 6),
            "lon": round(n["lon"], 6),
        })
    return out


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(radians, [a["lat"], a["lon"], b["lat"], b["lon"]])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


def timed(stations, total_minutes):
    """
    Spread a published end-to-end journey time across the hops in
    proportion to straight-line distance.

    Operators publish one end-to-end figure and nothing per hop. Dividing it
    evenly would give a 1.5 km inner-city hop the same time as a 4 km desert
    hop; weighting by distance at least respects the shape of the line.

    DWELL IS FOLDED IN RATHER THAN HELD BACK. An earlier version subtracted
    30s per intermediate station first and distributed only the remaining
    run time, which is the more literal reading of the GTFS. It produced a
    monorail quoted at 50 minutes against a published 60, because the
    engine builds graph edges from arrival(v) - departure(u): time spent
    standing at a platform falls between an arrival and a departure at the
    SAME stop, so it is not on any edge and silently disappears.

    That understates every rail journey by the dwell it never counts, in a
    codebase whose own route-comparison audit found systematic optimism was
    the main accuracy problem. The published figure is what a rider is told
    the trip takes, stops included, so the whole of it is distributed and
    stop_times carries no separate dwell.
    """
    legs = [haversine(stations[i], stations[i + 1]) for i in range(len(stations) - 1)]
    total_m = sum(legs)
    if total_minutes <= 0 or total_m <= 0:
        raise SystemExit(f"implausible timing for a {len(stations)}-station line")
    return [round(total_minutes * 60 * (d / total_m)) for d in legs], legs


LINES = []

# ---------------------------------------------------------------- monorail
mono_out = seq(13186211)      # Stadium -> New Capital
mono_in = seq(20384125)       # New Capital -> Stadium
assert len(mono_out) == 22, len(mono_out)
# Wikipedia and Alstom both give ~60 min end to end for the 56.5 km line.
mono_times, mono_legs = timed(mono_out, 60)
LINES.append({
    "key": "monorail_east_nile",
    "short_name": "Monorail",
    "long_name_en": "East Nile Monorail: Cairo Stadium - Justice City",
    "long_name_ar": "مونوريل شرق النيل: استاد القاهرة - مدينة العدالة",
    "route_type": 12,                       # GTFS: monorail
    "agency_id": "NAT",                     # National Authority for Tunnels
    "colour": "9C27B0",
    "opened": "2026-05-06",
    "service_start": "06:00", "service_end": "18:00",   # published: daily 6am-6pm
    "headway_sec": 600,
    "dwell_sec": 0,   # see timed(): dwell is folded into hop_seconds
    "fare_model": "banded_hops",
    "fare_bands": [[5, 20], [10, 40], [15, 55], [None, 80]],
    "length_km": 56.5,
    "journey_minutes": 60,
    "directions": [
        {"direction_id": 0, "osm_rel": 13186211,
         "headsign_en": "Justice City", "headsign_ar": "مدينة العدالة",
         "stations": mono_out, "hop_seconds": mono_times},
        {"direction_id": 1, "osm_rel": 20384125,
         "headsign_en": "Cairo Stadium", "headsign_ar": "استاد القاهرة",
         "stations": mono_in, "hop_seconds": list(reversed(mono_times))},
    ],
    "sources": [
        "OSM relations 13186211 / 20384125 (ODbL)",
        "Fares + 06:00-18:00 hours: Egyptian Streets, 9 May 2026",
        "Length, station count, 60 min journey: Wikipedia / Alstom",
    ],
})

# --------------------------------------------------------------------- LRT
# Two branches share Adly Mansour -> Badr, then diverge. Modelled as two
# routes rather than one, because a rider at Badr genuinely has to pick.
lrt_nac = seq(14350112)       # Adly Mansour -> Arts and Culture City
lrt_kc = seq(14350101)[::-1]  # reversed: Adly Mansour -> Knowledge City
                              # (this direction has 8 stops; the other
                              #  relation omits Industrial Park)
assert lrt_nac[0]["name_en"] == "Adly Mansour", lrt_nac[0]
assert lrt_kc[0]["name_en"] == "Adly Mansour", lrt_kc[0]

for key, stations, minutes, hs_en, hs_ar, rel in [
    ("lrt_capital", lrt_nac, 60, "Arts and Culture City", "مدينة الفنون والثقافة", 14350112),
    ("lrt_ramadan", lrt_kc, 40, "Knowledge City", "مدينة المعارف", 14350101),
]:
    times, _ = timed(stations, minutes)
    LINES.append({
        "key": key,
        "short_name": "LRT",
        "long_name_en": f"Cairo LRT: Adly Mansour - {hs_en}",
        "long_name_ar": f"القطار الكهربائي الخفيف: عدلي منصور - {hs_ar}",
        "route_type": 2,                    # GTFS: rail
        "agency_id": "NAT",
        "colour": "00897B",
        "opened": "2022-07-03",
        "service_start": "05:30", "service_end": "23:30",
        "headway_sec": 900,
        "dwell_sec": 0,   # see timed(): dwell is folded into hop_seconds
        "fare_model": "banded_hops",
        "fare_bands": [[3, 10], [7, 15], [None, 20]],
        "journey_minutes": minutes,
        "directions": [
            {"direction_id": 0, "osm_rel": rel,
             "headsign_en": hs_en, "headsign_ar": hs_ar,
             "stations": stations, "hop_seconds": times},
            {"direction_id": 1, "osm_rel": rel,
             "headsign_en": "Adly Mansour", "headsign_ar": "عدلي منصور",
             "stations": stations[::-1], "hop_seconds": list(reversed(times))},
        ],
        "sources": [
            f"OSM relation {rel} (ODbL)",
            "05:30-23:30 hours + fare bands: Egypt Independent",
            "Station list cross-checked against Wikipedia (Cairo Light Rail Transit)",
        ],
    })

# --------------------------------------------------------------------- APM
apm = seq(11728293)
# Cairo Airport's own description: free, four stations, "within only 5
# minutes". Four stations is exactly what OSM maps, which is a good sign
# both describe the same system.
apm_times, _ = timed(apm, 5)
LINES.append({
    "key": "airport_apm",
    "short_name": "APM",
    "long_name_en": "Cairo Airport Shuttle: TB1 - TB2 & TB3",
    "long_name_ar": "مكوك مطار القاهرة: مبنى ١ - مبنى ٢ و ٣",
    "route_type": 12,
    "agency_id": "CAI_APM",
    "colour": "455A64",
    "opened": None,
    # ASSUMED, NOT PUBLISHED -- the weakest figure in this file. Cairo
    # Airport publishes that the APM is free and takes 5 minutes, but not
    # when it runs. Round-the-clock is the reasonable assumption for a
    # people mover at an airport handling overnight flights, and it is the
    # assumption that fails safe: a rider told the shuttle is running when
    # it isn't walks between terminals, which is what they would have done
    # anyway. The opposite error strands them.
    "service_start": "00:00", "service_end": "23:59",
    "hours_verified": False,
    "headway_sec": 420,
    "dwell_sec": 0,   # see timed(): dwell is folded into hop_seconds
    "fare_model": "free",
    "fare_bands": [[None, 0]],
    "journey_minutes": 5,
    "directions": [
        {"direction_id": 0, "osm_rel": 11728293,
         "headsign_en": "TB2 & TB3", "headsign_ar": "مبنى ٢ و ٣",
         "stations": apm, "hop_seconds": apm_times},
        {"direction_id": 1, "osm_rel": 11728294,
         "headsign_en": "TB1", "headsign_ar": "مبنى ١",
         "stations": apm[::-1], "hop_seconds": list(reversed(apm_times))},
    ],
    "sources": [
        "OSM relations 11728293 / 11728294 (ODbL)",
        "Free, 4 stations, 5 minutes: Cairo International Airport",
        "Operating hours: NOT PUBLISHED -- assumed 24h, see comment above",
    ],
})

doc = {
    "generated_from": raw["source"],
    "licence": raw["licence"],
    "excluded": {
        "egyptian_national_railways":
            "route=train relations carry 3-17 node members against lines with "
            "dozens of stations, and no open timetable exists. Routing on it "
            "would require inventing departure times.",
        "alexandria_tram":
            "Outside the chosen coverage area; Raml line shut for "
            "reconstruction January 2026.",
        "ferries": "Only Nuweiba-Aqaba is mapped; not Cairo river transport.",
        "west_nile_monorail": "Under construction, no passenger service.",
    },
    "lines": LINES,
}
json.dump(doc, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

for ln in LINES:
    d0 = ln["directions"][0]
    print(f"{ln['key']:<20} type={ln['route_type']:<3} "
          f"{len(d0['stations'])} stations  "
          f"{sum(d0['hop_seconds'])/60:.0f} min run  "
          f"{ln['service_start']}-{ln['service_end']}")
print(f"\nwrote {OUT}")
