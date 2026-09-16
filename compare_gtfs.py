"""How much of each reconstructed corridor an EXISTING route already covers.

CONFOUND, STATED FIRST

The reconstruction walks edges taken from existing routes, so overlap with
existing routes is guaranteed and means nothing on its own. What is
informative is whether the corridor is covered by ONE existing route
end-to-end, or stitched together from many:

  one route covers most of it   -> the governorate route is probably already
                                   in the feed under a different number, and
                                   adding it would duplicate a line.
  no single route covers much   -> the corridor is a genuine combination the
                                   feed does not offer as one vehicle. This
                                   is the set worth adding, and it is exactly
                                   what a rider means by "there is a direct
                                   bus and your app makes me change twice".
"""
import collections, csv, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "gtfs_data")

stops = {r["stop_id"]: r["stop_name"] for r in csv.DictReader(open(f"{GTFS}/stops.txt", encoding="utf-8"))}
routes = {r["route_id"]: r for r in csv.DictReader(open(f"{GTFS}/routes.txt", encoding="utf-8"))}
trips = {t["trip_id"]: t for t in csv.DictReader(open(f"{GTFS}/trips.txt", encoding="utf-8"))}

# canonicalise the same way reconstruct.py did, so the sets are comparable
import reconstruct as R
_stops, _trips, _seq = R.load()
canon = R.cluster(_stops)

# SURVEYED routes only. Once the reconstructed routes are merged into the
# feed, comparing against everything means each one finds ITSELF covering
# itself 100% -- every route reads as a duplicate of a line that only exists
# because we built it, and the next build skips them all. Silent and total.
route_stops = collections.defaultdict(set)
for tid, rows in _seq.items():
    rid = trips[tid]["route_id"]
    if str(rid).startswith("GOV_"):
        continue
    for _, sid in rows:
        route_stops[rid].add(canon.get(sid, sid))

recon = json.load(open(f"{HERE}/reconstructed.json", encoding="utf-8"))
rows = []
for r in recon:
    mine = set(r["stops"])
    best_rid, best_cov = None, 0.0
    for rid, ss in route_stops.items():
        cov = len(mine & ss) / len(mine)
        if cov > best_cov:
            best_rid, best_cov = rid, cov
    rows.append((r, best_rid, best_cov))

buckets = collections.Counter()
for r, rid, cov in rows:
    buckets[">=90% (duplicate)" if cov >= .9 else
            "70-90% (near-duplicate)" if cov >= .7 else
            "40-70% (partly new)" if cov >= .4 else
            "<40% (genuinely new)"] += 1
print("best single existing route's coverage of each reconstructed corridor:")
for k in (">=90% (duplicate)", "70-90% (near-duplicate)", "40-70% (partly new)", "<40% (genuinely new)"):
    print(f"  {k:26} {buckets[k]:4}")

json.dump([{**r, "best_existing_route": rid,
            "best_existing_name": routes[rid]["route_short_name"] + " | " + routes[rid]["route_long_name"] if rid else None,
            "best_coverage": round(cov, 3)}
           for r, rid, cov in rows],
          open(f"{HERE}/compared.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

print("\nthe rider's three routes:")
for r, rid, cov in rows:
    if r["kind"] == "Minibus" and r["route_num"] in (10, 37, 140):
        nm = (routes[rid]["route_short_name"] + " | " + routes[rid]["route_long_name"]) if rid else "-"
        print(f"  [{r['route']:4}] {r['n_anchors']} anchors -> {r['n_stops']:3} stops, "
              f"{r['length_km']:5.1f} km, gap {r['mean_gap_m']:4}m, "
              f"segs {r['segments_connected']}/{r['segments_total']}")
        print(f"          closest existing: {cov:.0%}  {nm[:70]}")
