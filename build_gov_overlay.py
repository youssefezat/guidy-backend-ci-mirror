"""Turns matched governorate corridors into a GTFS overlay -- for REVIEW.

Writes to gtfs_gov_overlay/, never to gtfs_data/. Nothing here should be
merged into the live feed until the two review sheets from
gov_review_sheets.py have been filled in, because roughly one landmark in
twenty is currently resolved by a fuzzy string match that no human has
looked at, and a fuzzy match that lands on the wrong stop does not produce
a missing route -- it produces a route that confidently goes to the wrong
place. That is strictly worse than the gap it was meant to fill.

WHAT IS REAL HERE AND WHAT IS ESTIMATED

Real, from the governorate documents:
  route number, the ordered list of landmarks, and (for the bus document)
  the fare.

Estimated, by this script:
  every travel time, and every headway. The documents contain no timetable
  of any kind. Times come from the FEED'S OWN median inter-stop speed
  (5.61 m/s straight-line, measured over 41 227 existing inter-stop hops)
  and headways from its median (900 s), so a synthesised route is neither
  systematically faster nor slower than the surveyed routes it will be
  ranked against. Anything else would quietly bias the router toward or
  against every route added this way.

  Marking these as estimates is not decoration. `agency_id = GOV` exists so
  a later reader -- and the app -- can tell a surveyed route from a
  reconstructed one without archaeology.

QUALITY GATES

  MIN_RESOLVED_FRACTION  a corridor most of whose landmarks did not resolve
                         is not a route, it is a few scattered points.
  MIN_STOPS              two points make a straight line, not a corridor.
  MAX_DETOUR_RATIO       the one that actually catches bad fuzzy matches: a
                         landmark resolved to a stop on the wrong side of
                         the city turns the path into a zigzag, which shows
                         up as path length over endpoint distance long
                         before anyone reads the stop names.
"""

import collections
import csv
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gov_match import load_stops  # noqa: E402

OUT = os.path.join(HERE, "gtfs_gov_overlay")

MIN_RESOLVED_FRACTION = 0.8
MIN_STOPS = 4
MAX_DETOUR_RATIO = 2.5

# Both measured from this feed -- see the module docstring.
SPEED_MPS = 5.61
HEADWAY_SEC = 900
# Straight-line underestimates road distance; the engine already applies a
# comparable correction to surveyed edges (see raptor_engine's OSRM pass).
ROAD_FACTOR = 1.30
DWELL_SEC = 20


def hav(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def hhmmss(s):
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def main():
    stops = load_stops()
    match = json.load(open(os.path.join(HERE, "gov_match.json"), encoding="utf-8"))

    accepted, rejected = [], collections.Counter()
    for tag, recs in match["routes"].items():
        kind = "Minibus" if tag == "nakl_gama3y" else "CTA"
        for rec in recs:
            seq, seen_last = [], None
            for lm in rec["landmarks"]:
                sid = lm["stop_id"]
                if not sid or sid == seen_last:
                    continue
                seq.append(sid)
                seen_last = sid

            if rec["n_landmarks"] and rec["n_resolved"] / rec["n_landmarks"] < MIN_RESOLVED_FRACTION:
                rejected["too few landmarks resolved"] += 1
                continue
            if len(seq) < MIN_STOPS:
                rejected["fewer than %d stops" % MIN_STOPS] += 1
                continue

            pts = [(stops[s]["lat"], stops[s]["lon"]) for s in seq]
            path = sum(hav(a, b) for a, b in zip(pts, pts[1:]))
            span = hav(pts[0], pts[-1])
            if span > 0 and path / span > MAX_DETOUR_RATIO:
                rejected["zigzag (likely bad match)"] += 1
                continue

            accepted.append({"tag": tag, "kind": kind, "rec": rec, "seq": seq,
                             "path_m": path, "detour": path / span if span else 0})

    os.makedirs(OUT, exist_ok=True)

    with open(os.path.join(OUT, "routes.txt"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["route_id", "agency_id", "route_long_name", "route_short_name",
                    "route_type", "continuous_pickup", "continuous_drop_off"])
        for a in accepted:
            r = a["rec"]
            first = stops[a["seq"][0]]["name_en"]
            last = stops[a["seq"][-1]]["name_en"]
            w.writerow([f"GOV_{a['kind'][:3].upper()}_{r['route']}", "GOV",
                        f"{first} - {last}", f"{a['kind']} {r['route']}", 3, 1, 1])

    with open(os.path.join(OUT, "trips.txt"), "w", encoding="utf-8", newline="") as tf, \
         open(os.path.join(OUT, "stop_times.txt"), "w", encoding="utf-8", newline="") as sf, \
         open(os.path.join(OUT, "frequencies.txt"), "w", encoding="utf-8", newline="") as ff:
        tw, sw, fw = csv.writer(tf), csv.writer(sf), csv.writer(ff)
        tw.writerow(["route_id", "service_id", "trip_headsign", "direction_id", "shape_id", "trip_id"])
        sw.writerow(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
        fw.writerow(["trip_id", "start_time", "end_time", "headway_secs", "exact_times"])

        for a in accepted:
            rid = f"GOV_{a['kind'][:3].upper()}_{a['rec']['route']}"
            for direction, order in ((0, a["seq"]), (1, list(reversed(a["seq"])))):
                tid = f"{rid}_{direction}"
                tw.writerow([rid, "ALL", stops[order[-1]]["name_en"], direction, "", tid])
                t = 6 * 3600
                for i, sid in enumerate(order):
                    if i:
                        prev = (stops[order[i - 1]]["lat"], stops[order[i - 1]]["lon"])
                        cur = (stops[sid]["lat"], stops[sid]["lon"])
                        t += int(hav(prev, cur) * ROAD_FACTOR / SPEED_MPS) + DWELL_SEC
                    sw.writerow([tid, hhmmss(t), hhmmss(t), sid, i + 1])
                fw.writerow([tid, "06:00:00", "23:00:00", HEADWAY_SEC, 0])

    with open(os.path.join(OUT, "agency.txt"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["agency_id", "agency_name", "agency_url", "agency_timezone"])
        w.writerow(["GOV", "Cairo Governorate published routes (reconstructed)",
                    "https://www.cairo.gov.eg/", "Africa/Cairo"])

    print(f"accepted {len(accepted)} routes -> {OUT}/")
    for reason, n in rejected.most_common():
        print(f"  rejected {n:4}  {reason}")
    print()
    print(f"{'route':10}{'stops':>6}{'km':>8}{'detour':>8}  corridor")
    for a in sorted(accepted, key=lambda x: -x["detour"])[:8]:
        print(f"{a['kind'][:3]+' '+a['rec']['route']:10}{len(a['seq']):6}"
              f"{a['path_m']/1000:8.1f}{a['detour']:8.2f}  {a['rec']['corridor'][:60]}")


if __name__ == "__main__":
    main()
