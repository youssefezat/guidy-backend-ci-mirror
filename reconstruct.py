"""Reconstructs governorate routes as dense stop sequences.

THE PROBLEM THIS SOLVES

The governorate publishes each route as 8-10 landmarks. A route that really
has 30-60 stops therefore arrives as a handful of widely separated points,
and joining them with straight hops (the earlier attempt, see
gov-data-import-assessment-2026-09-09.md) produced 20 km stopless edges that
a router would happily treat as express service.

THE IDEA

The intermediate stops are not missing from the world -- they are already in
stops.txt. They were simply not named in the PDF. So instead of drawing a
line between two anchors, WALK THE EXISTING TRANSIT GRAPH between them: the
stops that real surveyed routes string together along that corridor are the
stops the governorate route passes too.

This deliberately uses no OSRM and no external routing. The osrm_data in
this repo is a FOOT extract, which ignores one-ways and motorway access and
is the wrong network for a bus corridor; and the transit graph has a
property a road network does not -- every edge in it is a hop some real
vehicle actually makes, so a reconstructed path is guaranteed to be
drivable by a bus, not merely by a car.

THE LIMITATION, STATED UP FRONT

A corridor can only be reconstructed along roads some existing route already
serves. Where the governorate route uses a road nothing else in the feed
touches, no path exists and the segment is reported unconnected rather than
guessed. That is the honest failure mode and it is measured below
(`segments_connected`), not hidden.
"""

import collections
import csv
import heapq
import json
import math
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "gtfs_data")

# Stops closer together than this are the same place with different ids --
# this feed has many (the engine clusters 1311 of them at load). Without
# merging, a path breaks at every hub because the arriving route and the
# departing route use different stop_ids for the same kerb.
CLUSTER_M = 60
# A reconstructed segment longer than this multiple of the straight line
# between its two anchors is not a corridor, it is the graph going the long
# way round. Reported unconnected instead.
MAX_SEGMENT_DETOUR = 2.0


def hav(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def load():
    stops = {}
    for r in csv.DictReader(open(os.path.join(GTFS, "stops.txt"), encoding="utf-8")):
        stops[r["stop_id"]] = (float(r["stop_lat"]), float(r["stop_lon"]), r["stop_name"])
    trips = {t["trip_id"]: t for t in csv.DictReader(open(os.path.join(GTFS, "trips.txt"), encoding="utf-8"))}
    seq = collections.defaultdict(list)
    for r in csv.DictReader(open(os.path.join(GTFS, "stop_times.txt"), encoding="utf-8")):
        seq[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"]))
    return stops, trips, seq


def cluster(stops):
    """stop_id -> canonical stop_id, merging anything within CLUSTER_M."""
    # Grid buckets so this stays O(n) rather than O(n^2) over 3119 stops.
    cell = CLUSTER_M / 111320.0
    grid = collections.defaultdict(list)
    for sid, (lat, lon, _) in stops.items():
        grid[(int(lat / cell), int(lon / cell))].append(sid)
    canon = {}
    for sid, (lat, lon, _) in stops.items():
        if sid in canon:
            continue
        gx, gy = int(lat / cell), int(lon / cell)
        group = [sid]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((gx + dx, gy + dy), ()):
                    if other != sid and other not in canon:
                        if hav((lat, lon), stops[other][:2]) <= CLUSTER_M:
                            group.append(other)
        for g in group:
            canon[g] = sid
    return canon


# Reconstructed routes live in the same feed once merged, and feeding them
# back in would let each run build on the last one's guesses -- a corridor
# invented on Monday becomes evidence on Tuesday. Only SURVEYED routes are
# real evidence, so the graph is built from those alone.
RECONSTRUCTED_PREFIX = "GOV_"


def build_graph(stops, seq, canon, trips=None):
    """Undirected stop graph. Every edge is a hop a real SURVEYED vehicle makes.

    Undirected on purpose: a one-way outbound leg almost always has a
    return leg on a parallel street, and for reconstructing which STOPS a
    corridor passes, direction is noise. Direction is re-imposed later by
    the order of the anchors themselves.
    """
    g = collections.defaultdict(dict)
    for tid, rows in seq.items():
        if trips is not None:
            rid = trips.get(tid, {}).get("route_id", "")
            if str(rid).startswith(RECONSTRUCTED_PREFIX):
                continue
        rows.sort()
        ids = [canon.get(s, s) for _, s in rows]
        for a, b in zip(ids, ids[1:]):
            if a == b:
                continue
            d = hav(stops[a][:2], stops[b][:2])
            if d <= 0:
                continue
            if b not in g[a] or d < g[a][b]:
                g[a][b] = d
                g[b][a] = d
    return g


def shortest(g, src, dst, limit_m):
    """Dijkstra, abandoned once the best remaining option exceeds limit_m."""
    if src == dst:
        return [src], 0.0
    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        if u == dst:
            break
        if d > limit_m:
            return None, None
        for v, w in g[u].items():
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if dst not in dist or dist[dst] > limit_m:
        return None, None
    path, cur = [dst], dst
    while cur != src:
        cur = prev[cur]
        path.append(cur)
    return path[::-1], dist[dst]


def pick_anchors(cand_lists, stops):
    """One stop per landmark, chosen to make the corridor as short as it can be.

    A corridor is a journey: each landmark should be near the one before it.
    Choosing greedily left-to-right -- each landmark's candidate nearest the
    previous choice -- collapses the out-and-back detours that arbitrary
    picks produce, without needing to know which landmarks are streets and
    which are points.

    The first landmark has nothing before it, so it is seeded from whichever
    of its own candidates is closest to the SECOND landmark's candidate set;
    otherwise a route starting on a long street begins at a random end of it.
    """
    lists = [c for c in cand_lists if c]
    if not lists:
        return []
    if len(lists) == 1:
        return [lists[0][0]]

    first = min(
        lists[0],
        key=lambda a: min(hav(stops[a][:2], stops[b][:2]) for b in lists[1]),
    )
    chosen = [first]
    for cands in lists[1:]:
        prev = stops[chosen[-1]][:2]
        chosen.append(min(cands, key=lambda c: hav(prev, stops[c][:2])))

    out = []
    for c in chosen:
        if not out or c != out[-1]:
            out.append(c)
    return drop_outliers(out, stops)


# A corridor's steps are of a kind: consecutive landmarks on a real route sit
# a few kilometres apart at most. One anchor sitting far outside that pattern
# is a bad match, not a long leg -- containment matching produced exactly this
# (كهرباء الأهرام in Giza matched شارع الكهرباء (مدينة نصر), ~20 km wrong), and
# a single such anchor drags the whole reconstructed line across the city.
#
# Detected against the route's OWN median step rather than a fixed distance,
# because a Cairo minibus corridor and a satellite-city one legitimately have
# different scales. Only interior anchors are dropped: a terminus is supposed
# to be at the end, so "far from its one neighbour" is normal there.
OUTLIER_STEP_MULTIPLE = 4.0
OUTLIER_MIN_ANCHORS = 4


def drop_outliers(anchors, stops):
    if len(anchors) < OUTLIER_MIN_ANCHORS:
        return anchors
    while len(anchors) >= OUTLIER_MIN_ANCHORS:
        steps = [hav(stops[a][:2], stops[b][:2])
                 for a, b in zip(anchors, anchors[1:])]
        med = sorted(steps)[len(steps) // 2]
        if med <= 0:
            return anchors
        worst, worst_gain = None, 0.0
        for i in range(1, len(anchors) - 1):
            detour = steps[i - 1] + steps[i]
            direct = hav(stops[anchors[i - 1]][:2], stops[anchors[i + 1]][:2])
            gain = detour - direct
            if detour > med * OUTLIER_STEP_MULTIPLE and gain > worst_gain:
                worst, worst_gain = i, gain
        if worst is None:
            return anchors
        anchors = anchors[:worst] + anchors[worst + 1:]
    return anchors


# --- Road fallback -------------------------------------------------------
#
# The transit graph can only join two anchors along roads some EXISTING route
# already serves. Where a governorate corridor uses a road nothing else in the
# feed touches, no path exists and the segment was reported unconnected --
# 26 corridors were dropped for exactly this.
#
# With a car OSRM profile we can route the ROAD between the two anchors and
# then pick up whichever stops lie along it. The stops are still real stops
# from stops.txt; only the ordering evidence changes, from "a vehicle makes
# this hop" to "this stop sits on the road between these two anchors".
#
# Weaker evidence, so it is a FALLBACK, tried only after the transit graph and
# the sibling retry have both failed, and every stop it contributes is
# recorded as such (`via_road`) rather than being silently mixed in.
#
# A CAR profile, not foot: a bus obeys one-ways and cannot use footpaths.
# Confirmed on the same two points in Tahrir -- foot returns 856 m, car
# returns 1 576 m, because the car has to go around.
OSRM_CAR_URL = os.environ.get("OSRM_CAR_URL", "http://localhost:5001")
# How close a stop must sit to the driven line to count as "on" this corridor.
# Cairo blocks are large and stop coordinates are survey-grade, so this is
# tight on purpose: widen it and a parallel street's stops get swept in.
ROAD_CORRIDOR_M = 120
# Beyond this the road answer is not a corridor between two anchors, it is a
# detour, and the segment is better reported unconnected than guessed.
ROAD_MAX_DETOUR = 2.5


def road_path(a, b, stops):
    """Driven road geometry between two stops, as [(lat, lon), ...], or None."""
    if not OSRM_CAR_URL:
        return None
    lat1, lon1 = stops[a][:2]
    lat2, lon2 = stops[b][:2]
    url = (f"{OSRM_CAR_URL}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
           "?overview=full&geometries=geojson")
    try:
        with urllib.request.urlopen(url, timeout=10) as fh:
            data = json.load(fh)
    except Exception:
        # Same fallback philosophy as OSRMClient for walking legs: an absent
        # or unreachable car server degrades this to the old behaviour
        # (segment reported unconnected), never to a crash.
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    coords = data["routes"][0]["geometry"]["coordinates"]
    return [(c[1], c[0]) for c in coords]


def stops_along(line, stops, canon, exclude=()):
    """Canonical stops within ROAD_CORRIDOR_M of `line`, in travel order.

    Ordered by distance ALONG the line rather than by distance from either
    end, so a corridor that doubles back still yields its stops in the order a
    vehicle would meet them.
    """
    if len(line) < 2:
        return []
    lat0 = math.radians(line[0][0])
    mx, my = 111320 * math.cos(lat0), 110540

    pts = [(lon * mx, lat * my) for lat, lon in line]
    # Cumulative length at each vertex, for the along-track position.
    cum = [0.0]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(x2 - x1, y2 - y1))

    lats = [p[0] for p in line]
    lons = [p[1] for p in line]
    pad = ROAD_CORRIDOR_M / 110540 * 2
    lo_lat, hi_lat = min(lats) - pad, max(lats) + pad
    lo_lon, hi_lon = min(lons) - pad, max(lons) + pad

    found = []
    seen = set()
    for sid, (slat, slon, _name) in stops.items():
        if canon.get(sid, sid) != sid or sid in exclude:
            continue
        if not (lo_lat <= slat <= hi_lat and lo_lon <= slon <= hi_lon):
            continue
        px, py = slon * mx, slat * my
        best_d, best_s = None, 0.0
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(pts, pts[1:])):
            dx, dy = x2 - x1, y2 - y1
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg2))
            qx, qy = x1 + t * dx, y1 + t * dy
            d = math.hypot(px - qx, py - qy)
            if best_d is None or d < best_d:
                best_d, best_s = d, cum[i] + t * math.hypot(dx, dy)
        if best_d is not None and best_d <= ROAD_CORRIDOR_M and sid not in seen:
            seen.add(sid)
            found.append((best_s, sid))
    found.sort()
    return [sid for _, sid in found]


def main():
    stops, trips, seq = load()
    canon = cluster(stops)
    g = build_graph(stops, seq, canon, trips)
    print(f"graph: {len(g)} nodes, {sum(len(v) for v in g.values()) // 2} undirected edges "
          f"({len(stops)} raw stops clustered to {len(set(canon.values()))})")

    match = json.load(open(os.path.join(HERE, "gov_match.json"), encoding="utf-8"))
    out = []
    for tag, recs in match["routes"].items():
        kind = "Minibus" if tag == "nakl_gama3y" else "CTA"
        for rec in recs:
            # Candidate sets, not fixed points. A landmark that names a
            # long street matches every stop along it, and the right one
            # depends on where the rest of the corridor goes -- see
            # pick_anchors.
            cand_lists = []
            for lm in rec["landmarks"]:
                if not lm["stop_id"]:
                    continue
                cs = [canon.get(c, c) for c in lm.get("candidates") or [lm["stop_id"]]]
                seen, uniq = set(), []
                for c in cs:
                    if c not in seen:
                        seen.add(c)
                        uniq.append(c)
                cand_lists.append(uniq)
            anchors = pick_anchors(cand_lists, stops)
            if len(anchors) < 2:
                continue
            # Keep the candidate sets alongside the greedy choice: when a
            # segment fails to connect, the shortest candidate is often
            # simply not on the graph, while a sibling stop 200 m away is.
            # Retrying with siblings recovers the segment without giving up
            # the shorter corridor the greedy pass found.
            by_anchor = {}
            for cl in cand_lists:
                for c in cl:
                    by_anchor.setdefault(c, cl)

            full, segs_ok, segs_total, gap_m = [], 0, 0, 0.0
            road_used, road_stops = 0, 0
            for a, b in zip(anchors, anchors[1:]):
                segs_total += 1
                straight = hav(stops[a][:2], stops[b][:2])
                path, d = shortest(g, a, b, max(straight * MAX_SEGMENT_DETOUR, 1500))
                if not path:
                    alts = [x for x in by_anchor.get(b, []) if x != b]
                    alts.sort(key=lambda x: hav(stops[a][:2], stops[x][:2]))
                    for alt in alts[:6]:
                        s2 = hav(stops[a][:2], stops[alt][:2])
                        path, d = shortest(g, a, alt, max(s2 * MAX_SEGMENT_DETOUR, 1500))
                        if path:
                            b = alt
                            break
                if not path:
                    # Last resort: drive the road between the two anchors and
                    # take the stops that sit on it. See road_path().
                    road = road_path(a, b, stops)
                    if road:
                        road_len = sum(hav(p, q) for p, q in zip(road, road[1:]))
                        if straight <= 0 or road_len / straight <= ROAD_MAX_DETOUR:
                            via = stops_along(road, stops, canon, exclude={a, b})
                            # The anchors themselves bookend it: OSRM snaps to
                            # the nearest road, which can land tens of metres
                            # off the stop, so they are added explicitly rather
                            # than relying on the corridor filter to catch them.
                            path = [a] + via + [b]
                            road_used += 1
                            road_stops += len(via)

                if path:
                    segs_ok += 1
                    full.extend(path if not full else path[1:])
                else:
                    gap_m += straight
                    if not full:
                        full.append(a)
                    full.append(b)

            pts = [stops[s][:2] for s in full]
            gaps = [hav(x, y) for x, y in zip(pts, pts[1:])] or [0]
            out.append({
                "kind": kind, "route": rec["route"], "route_num": rec["route_num"],
                "fare_egp": rec.get("fare_egp"), "corridor": rec["corridor"],
                "anchors": anchors, "stops": full,
                "n_anchors": len(anchors), "n_stops": len(full),
                "segments_connected": segs_ok, "segments_total": segs_total,
                "length_km": round(sum(gaps) / 1000, 1),
                "mean_gap_m": round(sum(gaps) / len(gaps)),
                "max_gap_m": round(max(gaps)),
                "unconnected_m": round(gap_m),
                # Provenance within the corridor: how much of it came from the
                # weaker road evidence rather than from hops a real vehicle
                # makes. Kept per route so a reviewer can see at a glance
                # which reconstructions lean on it.
                "road_segments": road_used,
                "road_stops": road_stops,
            })

    json.dump(out, open(os.path.join(HERE, "reconstructed.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    full_ok = [r for r in out if r["segments_connected"] == r["segments_total"]]
    via_road = [r for r in out if r["road_segments"]]
    print(f"\nreconstructed {len(out)} routes; {len(full_ok)} fully connected")
    print(f"  road fallback used on {len(via_road)} routes, "
          f"{sum(r['road_segments'] for r in out)} segments, "
          f"{sum(r['road_stops'] for r in out)} stops"
          f"{'  (car OSRM not reachable)' if not via_road else ''}")
    print(f"{'':22}{'before':>10}{'after':>10}")
    import statistics as st
    print(f"{'median stops/route':22}{st.median([r['n_anchors'] for r in out]):10.0f}"
          f"{st.median([r['n_stops'] for r in out]):10.0f}")
    print(f"{'median mean gap (m)':22}"
          f"{'-':>10}{st.median([r['mean_gap_m'] for r in out]):10.0f}")
    print(f"{'routes w/ mean gap>4km':22}{'-':>10}{sum(1 for r in out if r['mean_gap_m'] > 4000):10}")


if __name__ == "__main__":
    main()
