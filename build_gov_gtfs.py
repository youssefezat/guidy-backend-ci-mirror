"""GTFS for the governorate corridors no existing route already covers.

Reads compared.json (reconstructed stop sequences + overlap with the
existing feed) and writes a GTFS overlay for the routes whose best single
existing route covers less than COVERAGE_MAX of them -- i.e. combinations
this feed cannot currently offer as one vehicle.

Supersedes build_gov_overlay.py, which joined anchors with straight hops and
produced 20 km stopless edges. The stop sequences here come from
reconstruct.py and are real: every consecutive pair is a hop some surveyed
route already makes.

WHAT IS REAL AND WHAT IS ESTIMATED -- unchanged from the earlier attempt,
and still the thing to remember:

  real       route number, ordered stops, fare (bus document only)
  estimated  every travel time, every headway

The documents contain no timetable. Times use the feed's OWN median
inter-stop speed and headway, measured over its 41 227 existing hops, so a
reconstructed route is neither systematically faster nor slower than the
surveyed routes it will be ranked against. Any other choice quietly biases
the router for or against everything added this way.

agency_id = GOV so a surveyed route and a reconstructed one stay tellable
apart without archaeology.
"""

import collections
import csv
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "gtfs_gov_new")

# "Genuinely new" = no single existing route covers half of the corridor.
#
# This started at 0.40 and was raised on evidence, not taste. Minibus 37 --
# the route a rider reported as missing, which the governorate document
# confirms exists and which Google Maps also lacks -- sits at 48% coverage.
# A threshold that excludes the one route we have independent ground truth
# for is the wrong threshold. 0.50 also states something defensible on its
# own: less than half of this corridor is served by any one vehicle today.
COVERAGE_MAX = 0.50

# PLAUSIBILITY, CALIBRATED AGAINST THE SURVEYED FEED ITSELF
#
# The feed's own routes run to a median of 15.0 km, p90 34.4 km, max 76.0 km,
# with a median of 22 stops. A reconstruction claiming 171 km and 169 stops is
# not a minibus route, whatever the metrics say about its stop spacing -- and
# the first cut produced several, because a landmark name that also exists in
# a satellite city (10th of Ramadan, Sadat City) drags an anchor a hundred
# kilometres and the graph dutifully finds a path to it.
#
# Two independent failures, so two gates:
#
#   MAX_WANDER   path length over the straight-line chain through the anchors.
#                Catches a corridor that reaches its anchors by a ridiculous
#                route -- Minibus 50 came back at 64.7 km for an 11.6 km
#                anchor chain, a wander of 5.6.
#   MAX_LENGTH_KM
#                catches the opposite case, where the wander is low because
#                the ANCHORS themselves are wrong. Set just above the longest
#                real route in the feed; anything past it is a mismatch, not a
#                long route.
MAX_WANDER = 1.6
MAX_LENGTH_KM = 80.0
MAX_OD_RATIO = 3.2
MAX_SPUR_METERS = 3500
MAX_SPUR_RETURN_GAP_METERS = 1200
SPEED_MPS = 5.61             # feed median, straight-line
HEADWAY_SEC = 900            # feed median
# NO road factor. SPEED_MPS above was measured as STRAIGHT-LINE distance over
# recorded time across the feed's 41 227 existing hops, so the detour real
# roads take is already inside that number. Multiplying by a road factor as
# well double-counts it and made every reconstructed route ~30% slower than
# the surveyed routes it is ranked against -- which is precisely how a route
# gets added to the feed and then never chosen.
ROAD_FACTOR = 1.0
DWELL_SEC = 20
SERVICE_START, SERVICE_END = "06:00:00", "23:00:00"

# Matches the wording already in translations.txt: 229 existing routes use
# أتوبيس for CTA and 104 use مينى باص for Minibus. Inventing a third spelling
# would make the new lines look like a different network.
AR_MODE = {"Minibus": "مينى باص", "CTA": "أتوبيس"}


def hav(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def hhmmss(s):
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def has_spur_detour(stops_seq, stops_dict, min_spur_m=MAX_SPUR_METERS, max_gap_m=MAX_SPUR_RETURN_GAP_METERS):
    """Detects if a route doubles back on itself by deviating far to a bad anchor and returning."""
    coords = [stops_dict[s][:2] for s in stops_seq if s in stops_dict]
    n = len(coords)
    if n < 4:
        return False
    for i in range(0, n - 4):
        for k in range(i + 4, n):
            if hav(coords[i], coords[k]) < max_gap_m:
                max_dev = max(hav(coords[i], coords[m]) for m in range(i + 1, k))
                if max_dev >= min_spur_m:
                    return True
    return False



def main():
    stops = {r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"]), r["stop_name"])
             for r in csv.DictReader(open(f"{HERE}/gtfs_data/stops.txt", encoding="utf-8"))}
    ar = {}
    for t in csv.DictReader(open(f"{HERE}/gtfs_data/translations.txt", encoding="utf-8")):
        if t.get("table_name") == "stops" and t.get("field_name") == "stop_name":
            ar[t["field_value"]] = t["translation"]

    data = json.load(open(f"{HERE}/compared.json", encoding="utf-8"))

    surveyed_numbers = set()
    with open(f"{HERE}/gtfs_data/routes.txt", encoding="utf-8") as f:
        for rt in csv.DictReader(f):
            if not rt["route_id"].startswith("GOV_"):
                sn = rt.get("route_short_name", "").strip()
                if sn:
                    surveyed_numbers.add(sn.lower())

    picked, skipped = [], collections.Counter()
    for r in data:
        bare_num = str(r["route"]).lower()
        full_name = f"{r['kind']} {r['route']}".lower()
        in_surveyed = any(bare_num in s.split() or s == full_name for s in surveyed_numbers)

        if in_surveyed and r["best_coverage"] >= COVERAGE_MAX:
            skipped["already covered by an existing surveyed route with this number"] += 1
            continue
        elif not in_surveyed and r["best_coverage"] >= 0.98 and r.get("best_existing_name", "").lower().startswith(r["kind"].lower()):
            skipped["near-identical duplicate of an existing surveyed route"] += 1
            continue
        if r["segments_connected"] < r["segments_total"]:
            # A gap means two consecutive anchors could not be joined on the
            # transit graph, so the sequence jumps. Publishing that would
            # reintroduce exactly the phantom express edge this whole
            # approach exists to avoid.
            skipped["corridor has an unconnected gap"] += 1
            continue
        if len(r["stops"]) < 5:
            skipped["too few stops"] += 1
            continue
        anc = [stops[a][:2] for a in r["anchors"]]
        chain = sum(hav(a, b) for a, b in zip(anc, anc[1:])) / 1000
        if chain > 0.5 and r["length_km"] / chain > MAX_WANDER:
            skipped["wanders (bad path to a good anchor)"] += 1
            continue
        if r["length_km"] > MAX_LENGTH_KM:
            skipped["longer than any real route (bad anchor)"] += 1
            continue
        od_km = hav(stops[r["stops"][0]][:2], stops[r["stops"][-1]][:2]) / 1000
        if od_km > 2.0 and r["length_km"] / od_km > MAX_OD_RATIO:
            skipped["wanders across metropolitan area (bad anchor zigzag)"] += 1
            continue
        if has_spur_detour(r["stops"], stops) or has_spur_detour(list(reversed(r["stops"])), stops):
            skipped["contains out-and-back spur detour (bad intermediate anchor)"] += 1
            continue
        picked.append(r)

    os.makedirs(OUT, exist_ok=True)

    with open(f"{OUT}/agency.txt", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["agency_id", "agency_name", "agency_url", "agency_timezone"])
        # Two agencies, not one. agency_id is what resolve_vehicle_type()
        # reads, so a single "GOV" would make every reconstructed route a
        # plain bus -- wrong icon, wrong fare, wrong microbus wait penalty.
        # Splitting by mode keeps vehicle_type right while both ids stay
        # recognisable as reconstructed.
        w.writerow(["GOV_CTA", "Cairo Governorate published bus routes (reconstructed)",
                    "https://www.cairo.gov.eg/", "Africa/Cairo"])
        w.writerow(["GOV_CTA_M", "Cairo Governorate published minibus routes (reconstructed)",
                    "https://www.cairo.gov.eg/", "Africa/Cairo"])

    rows_rt, rows_tr, rows_st, rows_fq, rows_tx = [], [], [], [], []
    for r in picked:
        rid = f"GOV_{r['kind'][:3].upper()}_{r['route']}"
        a_en, b_en = stops[r["stops"][0]][2], stops[r["stops"][-1]][2]
        agency = "GOV_CTA_M" if r["kind"] == "Minibus" else "GOV_CTA"
        rows_rt.append([rid, agency, f"{a_en} - {b_en}", f"{r['kind']} {r['route']}", 3, 1, 1])
        rows_tx.append(["routes", "route_long_name", "ar", f"{a_en} - {b_en}",
                        f"{ar.get(a_en, a_en)} - {ar.get(b_en, b_en)}"])
        # ...and the SHORT name, which is what the Lines list actually
        # prints. Without this an Arabic rider sees "Minibus 37" sitting
        # among "مينى باص 112" and "أتوبيس 1145" -- and it is exactly the
        # NEW numbers that break, because the existing translations only
        # cover numbers the feed already had. 26 of the first 64 did.
        short = f"{r['kind']} {r['route']}"
        rows_tx.append(["routes", "route_short_name", "ar", short,
                        f"{AR_MODE[r['kind']]} {r['route']}"])

        for direction, order in ((0, r["stops"]), (1, list(reversed(r["stops"])))):
            tid = f"{rid}_{direction}"
            # Ground_Daily, not a fresh id: service_id must name a row that
            # already exists in calendar.txt. A trip whose service_id is
            # unknown to the calendar is silently never in service -- the
            # first merge used "ALL" and all 128 trips were dropped without
            # a single warning, which looked exactly like "the routes are
            # there but the router prefers something else".
            rows_tr.append([rid, "Ground_Daily", stops[order[-1]][2], direction, "", tid])
            t = 6 * 3600
            for i, sid in enumerate(order):
                if i:
                    d = hav(stops[order[i - 1]][:2], stops[sid][:2])
                    t += int(d * ROAD_FACTOR / SPEED_MPS) + DWELL_SEC
                rows_st.append([tid, hhmmss(t), hhmmss(t), sid, i + 1])
            rows_fq.append([tid, SERVICE_START, SERVICE_END, HEADWAY_SEC, 0])

    def dump(name, header, rows):
        with open(f"{OUT}/{name}", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    dump("routes.txt", ["route_id", "agency_id", "route_long_name", "route_short_name",
                        "route_type", "continuous_pickup", "continuous_drop_off"], rows_rt)
    dump("trips.txt", ["route_id", "service_id", "trip_headsign", "direction_id",
                       "shape_id", "trip_id"], rows_tr)
    dump("stop_times.txt", ["trip_id", "arrival_time", "departure_time",
                            "stop_id", "stop_sequence"], rows_st)
    dump("frequencies.txt", ["trip_id", "start_time", "end_time",
                             "headway_secs", "exact_times"], rows_fq)
    dump("translations.txt", ["table_name", "field_name", "language",
                              "field_value", "translation"], rows_tx)

    print(f"{len(picked)} routes -> {OUT}/")
    for k, n in skipped.most_common():
        print(f"  skipped {n:4}  {k}")
    tot = sum(len(r["stops"]) for r in picked)
    print(f"\n{len(rows_tr)} trips, {len(rows_st)} stop_times, "
          f"{tot / max(len(picked),1):.0f} stops/route average")
    print("\nlongest 6:")
    for r in sorted(picked, key=lambda x: -x["length_km"])[:6]:
        print(f"  {r['kind'][:3]} {r['route']:5} {len(r['stops']):3} stops "
              f"{r['length_km']:6.1f} km  gap {r['mean_gap_m']:4}m  "
              f"cov {r['best_coverage']:.0%}")


if __name__ == "__main__":
    main()
