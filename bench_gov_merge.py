"""Does adding the 64 reconstructed routes make existing answers worse?

A new route can only ever ADD an option, so no trip should get slower. If one
does, the synthetic stop_times are contaminating the shared edge statistics --
the engine derives cross-route medians and implausible-timing guards from
every trip in the feed, so 5 972 invented stop_times can move the estimates
that surveyed routes are scored with.
"""
import datetime, random, sys
from raptor_engine import GTFSRaptorEngine

random.seed(7)
import csv
stops = [(float(r['stop_lat']), float(r['stop_lon']))
         for r in csv.DictReader(open('gtfs_data/stops.txt', encoding='utf-8'))]
pairs = []
while len(pairs) < 12:
    a, b = random.choice(stops), random.choice(stops)
    if abs(a[0]-b[0]) + abs(a[1]-b[1]) > 0.05:
        pairs.append((a, b))

NOON = datetime.datetime(2026, 9, 9, 12, 0, 0)
res = {}
for feed in ("feed_base", "feed_new"):
    e = GTFSRaptorEngine(feed); e.load_data()
    out = []
    for O, D in pairs:
        r = e.run_raptor_by_coords(O[0], O[1], D[0], D[1], lang="en", now=NOON)
        if r.get("success") and r["options"]:
            best = min(r["options"], key=lambda o: int(o["time"]))
            out.append((int(best["time"]), best.get("fare_egp"),
                        any(str(i.get("route_id","")).startswith("GOV_") for i in best["instructions"])))
        else:
            out.append(None)
    res[feed] = out

worse = better = same = failed = gov_used = 0
for i, (a, b) in enumerate(zip(res["feed_base"], res["feed_new"])):
    if a is None or b is None:
        failed += 1; continue
    if b[2]: gov_used += 1
    if b[0] > a[0]: worse += 1
    elif b[0] < a[0]: better += 1
    else: same += 1
print(f"\n{len(pairs)} random trips, best-option journey time:")
print(f"  improved : {better}")
print(f"  unchanged: {same}")
print(f"  WORSE    : {worse}")
print(f"  no answer either side: {failed}")
print(f"  best option used a GOV route: {gov_used}")
if worse:
    print("\n  worst regressions:")
    rows = [(b[0]-a[0], a[0], b[0]) for a, b in zip(res['feed_base'], res['feed_new']) if a and b and b[0] > a[0]]
    for d, x, y in sorted(rows, reverse=True)[:5]:
        print(f"    +{d:3} min   {x} -> {y}")
