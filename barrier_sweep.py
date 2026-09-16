"""
Feed-wide sweep for WALKABLE-ON-PAPER, IMPOSSIBLE-IN-REALITY transfers.

Finds interchange (walk) edges whose straight line passes through space
the road network does not occupy -- water, rail yards, walled compounds.
Those are the pairs where OSRM's foot profile can quietly report a short
walk no pedestrian can make (the Imbaba->Zamalek case: 538m claimed,
5.2km real).

Uses shapes.txt (532k points of real vehicle route geometry) as a proxy
for "where roads are". No OSRM and no network access needed.

KEY DISCRIMINATOR: a real barrier has roads at BOTH ENDS and a gap in the
MIDDLE. If the endpoints are themselves far from any road, that is just a
thinly-mapped part of the feed (Sheikh Zayed, 6th October) and says
nothing about walkability -- those are reported separately, not flagged.
"""
import json
from collections import defaultdict
from raptor_engine import GTFSRaptorEngine

SAMPLE_EVERY_M = 40
ENDPOINT_NEAR_M = 150     # both stops must be this close to a road to trust the probe
REPORT_RATIO = 2.5        # middle gap must exceed endpoint distance by this factor
REPORT_GAP_M = 180

eng = GTFSRaptorEngine('gtfs_data', use_osrm=False)
eng.load_data()

CELL = 0.0025
grid = defaultdict(list)
for pts in eng.shapes.values():
    for lat, lon in pts:
        grid[(int(lat / CELL), int(lon / CELL))].append((lat, lon))

def dist_to_road(lat, lon, rings=1):
    gx, gy = int(lat / CELL), int(lon / CELL)
    best = 1e9
    for dx in range(-rings, rings + 1):
        for dy in range(-rings, rings + 1):
            for rlat, rlon in grid.get((gx + dx, gy + dy), ()):
                dlat = (rlat - lat) * 111000
                dlon = (rlon - lon) * 96000
                d2 = dlat * dlat + dlon * dlon
                if d2 < best:
                    best = d2
    return best ** 0.5

cell_deg = eng.WALK_LINK_RADIUS_M / 111000
sgrid = defaultdict(list)
for sid, info in eng.stops.items():
    sgrid[(round(info['lon'] / cell_deg), round(info['lat'] / cell_deg))].append(sid)

seen, pairs = set(), []
for (gx, gy), sids in sgrid.items():
    nb = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            nb.extend(sgrid.get((gx + dx, gy + dy), []))
    for s1 in sids:
        for s2 in set(nb):
            if s1 >= s2 or (s1, s2) in seen:
                continue
            seen.add((s1, s2))
            a, b = eng.stops[s1], eng.stops[s2]
            d = eng._haversine(a['lat'], a['lon'], b['lat'], b['lon'])
            if 0 < d <= eng.WALK_LINK_RADIUS_M:
                pairs.append((s1, s2, d))
print(f"{len(pairs)} interchange pairs to probe")

flagged, sparse = [], 0
for s1, s2, d in pairs:
    a, b = eng.stops[s1], eng.stops[s2]
    ea = dist_to_road(a['lat'], a['lon'], rings=2)
    eb = dist_to_road(b['lat'], b['lon'], rings=2)
    if ea > ENDPOINT_NEAR_M or eb > ENDPOINT_NEAR_M:
        sparse += 1
        continue                       # thinly-mapped area, not a barrier signal
    steps = max(3, int(d / SAMPLE_EVERY_M))
    worst, worst_pt = 0.0, None
    for i in range(1, steps):
        t = i / steps
        plat = a['lat'] + (b['lat'] - a['lat']) * t
        plon = a['lon'] + (b['lon'] - a['lon']) * t
        g = dist_to_road(plat, plon, rings=2)
        if g > worst:
            worst, worst_pt = g, (plat, plon)
    endpoint_ref = max(ea, eb, 30.0)
    if worst >= REPORT_GAP_M and worst >= endpoint_ref * REPORT_RATIO:
        flagged.append((worst, d, s1, s2, worst_pt, ea, eb))

flagged.sort(reverse=True)
print(f"skipped {sparse} pairs in thinly-mapped areas (endpoints not near any road)")
print(f"\n{len(flagged)} pairs look like a genuine BARRIER (roads at both ends, gap in the middle)\n")
for worst, d, s1, s2, pt, ea, eb in flagged:
    print(f"  gap {worst:5.0f}m / {d:5.0f}m line (ends {ea:4.0f}m,{eb:4.0f}m from road) | "
          f"{eng.stops[s1]['name'][:30]:30s} <-> {eng.stops[s2]['name'][:30]:30s} "
          f"[{s1},{s2}]")

json.dump(
    [{'gap_m': round(w, 1), 'straight_m': round(d, 1), 'stop_a': s1, 'stop_b': s2,
      'name_a': eng.stops[s1]['name'], 'name_b': eng.stops[s2]['name'],
      'a': [eng.stops[s1]['lat'], eng.stops[s1]['lon']],
      'b': [eng.stops[s2]['lat'], eng.stops[s2]['lon']],
      'endpoint_road_dist_m': [round(ea, 1), round(eb, 1)]}
     for w, d, s1, s2, pt, ea, eb in flagged],
    open('barrier_sweep_results.json', 'w'), indent=2)
print(f"\nwrote barrier_sweep_results.json ({len(flagged)} entries)")
