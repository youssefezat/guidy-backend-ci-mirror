"""
Merge the Greater Cairo rail lines in gtfs_data/gcr_rail_lines.json into the
Transport for Cairo GTFS feed.

IDEMPOTENT BY CONSTRUCTION
--------------------------
Every row this script writes carries an `RL_` id prefix, and the first thing
it does is drop every existing `RL_` row from each file. Re-running it is
therefore a rebuild, not an append. That matters because the alternative --
"append if not present" -- silently accumulates half-updated state the moment
a line's station list changes.

WHY NEW STOPS INSTEAD OF REUSING NEARBY ONES
--------------------------------------------
Eleven of the new stations sit within 150m of an existing feed stop, so
merging them into those stops was tempting. It would have been wrong. The
existing stop at 113m from El Musheer Ahmed Ismail is a kerbside bus stop
called "Future School (Nasr City)"; the monorail station is an elevated
platform reached through a paid gate. Collapsing them into one node tells
the router a rider can move between bus and monorail in zero seconds with no
walk, which is the single most common way a transit graph produces routes
nobody can actually follow.

Kept separate, the engine's own `_add_interchange_edges` pass builds a real
walking edge between them (WALK_LINK_RADIUS_M is 500m, and it prefers OSRM's
street-network duration over straight-line), so the transfer is modelled at
the walking time it actually takes.

SERVICE IS FREQUENCY-BASED, NOT TIMETABLED
------------------------------------------
None of these operators publish a stop-by-stop timetable. What they publish
is an operating window and, informally, a headway. That is exactly what
frequencies.txt is for: stop_times.txt carries one template trip whose
absolute times are meaningless but whose *deltas* are the real run times, and
frequencies.txt carries the window and headway. The engine already reads the
window to answer "is this running right now" -- so writing the window here is
what stops the app from proposing a monorail trip at 02:00, when the line has
been shut for eight hours.
"""

import csv
import json
import os
import sys
from collections import OrderedDict

FOLDER = sys.argv[1] if len(sys.argv) > 1 else "gtfs_data"
PREFIX = "RL_"

doc = json.load(open(f"{FOLDER}/gcr_rail_lines.json", encoding="utf-8"))
LINES = doc["lines"]


# --------------------------------------------------------------- file helpers
def read(name):
    path = f"{FOLDER}/{name}"
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def write(name, fields, rows):
    with open(f"{FOLDER}/{name}", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def strip_ours(rows, key):
    """Drop rows this script previously wrote, so a re-run rebuilds."""
    return [r for r in rows if not str(r.get(key, "")).startswith(PREFIX)]


def hhmmss(sec):
    # GTFS allows hours past 24 for after-midnight service; none of these
    # lines run that late, but the formatting stays correct if one ever does.
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def to_sec(hhmm):
    h, m = hhmm.split(":")[:2]
    return int(h) * 3600 + int(m) * 60


# ------------------------------------------------------------------ new stops
# ONE STOP PER STATION, NOT PER PLATFORM.
#
# OSM models each direction of the monorail with its own stop node, so the
# two relations for the East Nile line share zero node ids even though they
# describe the same 22 stations -- the paired nodes sit 8-10m apart with
# identical names, one per platform. Keying on the node id therefore produced
# 44 monorail "stations", and by the same mechanism the two LRT branches
# duplicated the six stops they share between Adly Mansour and Badr.
#
# Two platforms of one station are one station to a router: a rider changing
# direction walks across a platform, not between stops. The feed's existing
# metro rows follow the same convention (84 stations for 3 lines, not 168).
# So stations are matched on name within STATION_MERGE_M, and the OSM node
# ids that resolved to each one are recorded for traceability.
STATION_MERGE_M = 300


def haversine(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


stations = []            # in creation order
stops_by_node = {}       # osm node id -> station dict
line_seq = {"monorail_east_nile": "MONO", "lrt_capital": "LRT",
            "lrt_ramadan": "LRT", "airport_apm": "APM"}
merged = 0

for ln in LINES:
    tag = line_seq[ln["key"]]
    for d in ln["directions"]:
        for st in d["stations"]:
            node = st["osm_node"]
            if node in stops_by_node:
                continue
            hit = None
            for s in stations:
                if s["stop_name"] != st["name_en"]:
                    continue
                if haversine(float(s["stop_lat"]), float(s["stop_lon"]),
                             st["lat"], st["lon"]) <= STATION_MERGE_M:
                    hit = s
                    break
            if hit is not None:
                hit["osm_nodes"].append(node)
                stops_by_node[node] = hit
                merged += 1
                continue
            n = sum(1 for v in stations if v["tag"] == tag) + 1
            s = {
                "stop_id": f"{PREFIX}{tag}_{n:02d}",
                "stop_name": st["name_en"],
                "stop_lat": f"{st['lat']:.6f}",
                "stop_lon": f"{st['lon']:.6f}",
                "name_ar": st["name_ar"],
                "tag": tag,
                "osm_nodes": [node],
            }
            stations.append(s)
            stops_by_node[node] = s

stops_by_id = OrderedDict((s["stop_id"], s) for s in stations)
print(f"{len(stations)} new stations "
      f"({merged} duplicate platform/branch nodes merged)")

# ---------------------------------------------------------------------- stops
fields, rows = read("stops.txt")
rows = strip_ours(rows, "stop_id")
existing_ids = {r["stop_id"] for r in rows}
for s in stations:
    assert s["stop_id"] not in existing_ids, f"id collision: {s['stop_id']}"
    rows.append({"stop_id": s["stop_id"], "stop_name": s["stop_name"],
                 "stop_lat": s["stop_lat"], "stop_lon": s["stop_lon"]})
write("stops.txt", fields, rows)
print(f"  stops.txt      -> {len(rows)} rows")

# --------------------------------------------------------------------- agency
fields, rows = read("agency.txt")
rows = strip_ours(rows, "agency_id")
have = {r["agency_id"] for r in rows}
for aid, name, url in [
    ("CAI_APM", "Cairo Airport Shuttle", "https://www.cairo-airport.com/"),
]:
    if aid not in have:
        rows.append({"agency_id": aid, "agency_name": name,
                     "agency_url": url, "agency_timezone": "Africa/Cairo"})
write("agency.txt", fields, rows)
print(f"  agency.txt     -> {len(rows)} rows")

# --------------------------------------------------------------------- routes
route_ids = {"monorail_east_nile": f"{PREFIX}MONO_EN",
             "lrt_capital": f"{PREFIX}LRT_CAP",
             "lrt_ramadan": f"{PREFIX}LRT_RAM",
             "airport_apm": f"{PREFIX}APM"}

fields, rows = read("routes.txt")
rows = strip_ours(rows, "route_id")
for ln in LINES:
    rows.append({
        "route_id": route_ids[ln["key"]],
        "agency_id": ln["agency_id"],
        "route_long_name": ln["long_name_en"],
        "route_short_name": ln["short_name"],
        "route_type": str(ln["route_type"]),
        "continuous_pickup": "1",     # 1 = not available; these are
        "continuous_drop_off": "1",   # station-to-station, never hail-and-ride
    })
write("routes.txt", fields, rows)
print(f"  routes.txt     -> {len(rows)} rows")

# ------------------------------------------------------- trips and stop_times
tfields, trips = read("trips.txt")
trips = strip_ours(trips, "trip_id")
stfields, stimes = read("stop_times.txt")
stimes = strip_ours(stimes, "trip_id")
ffields, freqs = read("frequencies.txt")
freqs = strip_ours(freqs, "trip_id")

n_st = 0
for ln in LINES:
    rid = route_ids[ln["key"]]
    start = to_sec(ln["service_start"])
    end = to_sec(ln["service_end"])
    dwell = ln["dwell_sec"]
    for d in ln["directions"]:
        tid = f"{rid}_D{d['direction_id']}"
        trips.append({
            "route_id": rid,
            "service_id": "Ground_Daily",
            "trip_headsign": d["headsign_en"],
            "direction_id": str(d["direction_id"]),
            "shape_id": "",          # no shape: the app draws these from
            "trip_id": tid,          # the stop sequence, and a wrong shape
        })                           # is worse than none
        t = start
        prev_stop = None
        for i, st in enumerate(d["stations"]):
            node_stop = stops_by_node[st["osm_node"]]["stop_id"]
            # Merging platforms into stations could in principle collapse two
            # consecutive entries into one, which would put a zero-length
            # self-loop in the graph. It doesn't on this data, but a future
            # line with a reversal move would hit it silently.
            assert node_stop != prev_stop, (
                f"{tid} visits {node_stop} twice in a row -- "
                f"STATION_MERGE_M may be too generous")
            prev_stop = node_stop
            arrival = t
            departure = t if i == 0 else t + dwell
            stimes.append({
                "trip_id": tid,
                "stop_id": node_stop,
                "stop_sequence": str(i + 1),
                "arrival_time": hhmmss(arrival),
                "departure_time": hhmmss(departure),
            })
            n_st += 1
            if i < len(d["hop_seconds"]):
                t = departure + d["hop_seconds"][i]
        freqs.append({
            "trip_id": tid,
            "start_time": ln["service_start"] + ":00",
            "end_time": ln["service_end"] + ":00",
            "headway_secs": str(ln["headway_sec"]),
        })
        # A template trip whose last arrival runs past the service window
        # would be filtered out as "not running" for its whole final hour.
        assert t <= end + 3600, f"{tid} template ends {hhmmss(t)} vs window {ln['service_end']}"

write("trips.txt", tfields, trips)
write("stop_times.txt", stfields, stimes)
write("frequencies.txt", ffields, freqs)
print(f"  trips.txt      -> {len(trips)} rows")
print(f"  stop_times.txt -> {len(stimes)} rows (+{n_st})")
print(f"  frequencies.txt-> {len(freqs)} rows")

# --------------------------------------------------------------- translations
# This feed uses the legacy translations layout, matched on field_value
# rather than record_id -- so one row per distinct English string.
fields, rows = read("translations.txt")
seen = {(r["table_name"], r["field_name"], r["language"], r["field_value"])
        for r in rows}


def add_tr(table, field, value, ar):
    if not ar or not value:
        return
    key = (table, field, "ar", value)
    if key in seen:
        return
    seen.add(key)
    rows.append({"table_name": table, "field_name": field, "language": "ar",
                 "field_value": value, "translation": ar})


for s in stations:
    add_tr("stops", "stop_name", s["stop_name"], s["name_ar"])
for ln in LINES:
    add_tr("routes", "route_long_name", ln["long_name_en"], ln["long_name_ar"])
    for d in ln["directions"]:
        add_tr("trips", "trip_headsign", d["headsign_en"], d["headsign_ar"])
add_tr("agency", "agency_name", "Cairo Airport Shuttle", "مكوك مطار القاهرة")
add_tr("routes", "route_short_name", "Monorail", "مونوريل")
add_tr("routes", "route_short_name", "LRT", "القطار الكهربائي الخفيف")
write("translations.txt", fields, rows)
print(f"  translations   -> {len(rows)} rows")

print("\nlines written:")
for ln in LINES:
    d0 = ln["directions"][0]
    print(f"  {route_ids[ln['key']]:<12} type={ln['route_type']:<3} "
          f"{len(d0['stations']):>2} stations  "
          f"{ln['service_start']}-{ln['service_end']}  "
          f"every {ln['headway_sec'] // 60} min")
