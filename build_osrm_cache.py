"""
Offline enrichment pass: fetch REAL road-network driving distance/time for
GTFS stop pairs whose recorded transit time looks suspiciously fast, and
cache the results to gtfs_data/osrm_edge_cache.json so raptor_engine.py can
correct implausible edges at load time without any live network dependency
in production (see load_data()'s use of OSRM_EDGE_CACHE_PATH).

WHY THIS EXISTS
----------------
The GTFS-load pipeline already guards against implausible speed using
HAVERSINE (straight-line) distance -- but a 2026-08-22 sanity check against
real OSRM driving data (100 stop pairs, see gtfs_sanity_check.xlsx) found
real road distance runs 1.63x haversine at the median in this feed, and up
to 5.8x for pairs needing a bridge/ring-road detour. A hop that needs a real
detour can look perfectly plausible against straight-line distance while
being physically impossible on the actual street network. That same check
also found roughly 1 in 5 sampled corridors had at least one route recorded
as FASTER than a car can drive the same real distance -- including cases
where 20+ different route_ids all shared the exact same (wrong) time,
which the cross-route disagreement check in raptor_engine.py can't catch
either, since there's no disagreement between routes to detect when they're
all equally wrong.

This script is the fix for both blind spots: it gets the GROUND-TRUTH real
road distance for candidate edges, so raptor_engine.py can apply a real
physical speed cap instead of an approximate straight-line one.

USAGE
------
  # Against the public OSRM demo server (default, please be considerate --
  # see RATE_LIMIT_SEC below; this is a shared, free, rate-limited service):
  python3 build_osrm_cache.py

  # Against your own self-hosted OSRM instance (see osrm_client.py's
  # docstring for how to stand one up) -- no rate limit needed, much faster
  # for a full-coverage run over all ~1,700+ candidate pairs in this feed:
  OSRM_BASE_URL=http://localhost:5000 python3 build_osrm_cache.py --fast

Resumable: re-running only fetches pairs missing from the existing cache
file, so it's safe to Ctrl-C and restart, or run it periodically in small
batches (e.g. a nightly cron with LIMIT set) to build up coverage over time
without ever hammering the public demo server in one sitting.

SCOPE
------
Only checks (u, v) stop pairs where the FASTEST bus/minibus/microbus route
serving that pair implies >HAVERSINE_SPEED_THRESHOLD_MPS -- i.e. plausible
candidates for being wrong, not the whole graph (~7,500+ edges; most are
already well under this threshold and don't need real-distance verification).
As of 2026-08-22 that's ~1,738 unique stop pairs at the default 8 m/s
threshold. Lower the threshold for wider (slower) coverage; raise it to
focus on the worst offenders first.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from raptor_engine import GTFSRaptorEngine
import requests

HAVERSINE_SPEED_THRESHOLD_MPS = 8.0  # ~29 km/h -- see module docstring
MIN_HAVERSINE_DIST_M = 300  # skip near-adjacent stops; noise at this scale, see the sanity-check report
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
RATE_LIMIT_SEC = 1.0  # be considerate to the shared public demo server; irrelevant for a self-hosted instance
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtfs_data", "osrm_edge_cache.json")


def find_candidate_pairs(engine, limit=None):
    pair_best = {}
    for u, edges in engine.graph.items():
        for v, route_id, direction_id, t in edges:
            if route_id == "WALK":
                continue
            vtype = engine.routes.get(route_id, {}).get("vehicle_type")
            if vtype not in ("bus", "minibus", "microbus"):
                continue
            if u not in engine.stops or v not in engine.stops:
                continue
            hav = engine._haversine(
                engine.stops[u]["lat"], engine.stops[u]["lon"],
                engine.stops[v]["lat"], engine.stops[v]["lon"],
            )
            if hav < MIN_HAVERSINE_DIST_M:
                continue
            speed = hav / t if t > 0 else 0
            key = (u, v)
            if key not in pair_best or t < pair_best[key][1]:
                pair_best[key] = (speed, t, hav)

    candidates = [k for k, (speed, t, hav) in pair_best.items() if speed > HAVERSINE_SPEED_THRESHOLD_MPS]
    # Worst (fastest-implied-speed) first, so a partial/interrupted run still
    # covers the highest-value edges.
    candidates.sort(key=lambda k: -pair_best[k][0])
    if limit:
        candidates = candidates[:limit]
    return candidates


def fetch_osrm(u_lat, u_lon, v_lat, v_lon):
    url = f"{OSRM_BASE_URL}/route/v1/driving/{u_lon},{u_lat};{v_lon},{v_lat}?overview=false"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    route = data["routes"][0]
    return {"duration_s": route["duration"], "distance_m": route["distance"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=200, help="Max NEW pairs to fetch this run (default 200; use a self-hosted OSRM + --fast for full coverage in one pass).")
    parser.add_argument("--fast", action="store_true", help="Skip the rate-limit sleep -- only use this against a self-hosted OSRM instance, never the public demo server.")
    args = parser.parse_args()

    engine = GTFSRaptorEngine("gtfs_data", use_osrm=False)
    engine.load_data()

    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)
    print(f"Existing cache: {len(cache)} entries")

    candidates = find_candidate_pairs(engine)
    print(f"Candidate stop pairs (haversine-implied speed > {HAVERSINE_SPEED_THRESHOLD_MPS} m/s): {len(candidates)} total")

    todo = [(u, v) for (u, v) in candidates if f"{u}|{v}" not in cache]
    print(f"Not yet cached: {len(todo)}")
    todo = todo[: args.limit]
    print(f"Fetching {len(todo)} this run (--limit {args.limit})")

    fetched = 0
    for i, (u, v) in enumerate(todo):
        su, sv = engine.stops[u], engine.stops[v]
        try:
            result = fetch_osrm(su["lat"], su["lon"], sv["lat"], sv["lon"])
        except Exception as e:
            print(f"  [{i+1}/{len(todo)}] {su['name']} -> {sv['name']}: FAILED ({e})")
            continue
        if result is None:
            print(f"  [{i+1}/{len(todo)}] {su['name']} -> {sv['name']}: no route found")
            continue
        cache[f"{u}|{v}"] = {**result, "source": "osrm_" + ("self_hosted" if "localhost" in OSRM_BASE_URL or "127.0.0.1" in OSRM_BASE_URL else "public_demo")}
        fetched += 1
        print(f"  [{i+1}/{len(todo)}] {su['name']} -> {sv['name']}: {result['duration_s']:.0f}s / {result['distance_m']:.0f}m")

        if (i + 1) % 20 == 0:
            with open(CACHE_PATH, "w") as f:
                json.dump(cache, f, indent=1)
            print(f"  -- checkpoint saved ({len(cache)} total entries) --")

        if not args.fast:
            time.sleep(RATE_LIMIT_SEC)

    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=1)
    print(f"Done. Fetched {fetched} new entries this run. Cache now has {len(cache)} total entries "
          f"({len(cache)}/{len(candidates)} of known candidates covered).")


if __name__ == "__main__":
    main()
