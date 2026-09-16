"""
Compares Guidy's own route recommendations against Google's real transit
directions for a stratified sample of trips across the GTFS coverage
area. Resumable/checkpointed like build_osrm_cache.py -- safe to
interrupt (Ctrl+C) and rerun; already-checked pairs are skipped.

CAVEAT (read before interpreting results): Google's transit data for
Cairo most likely only covers the Metro and a handful of official bus
lines -- it does not know about the informal minibus/microbus network
that most of this GTFS feed (from Transport for Cairo) is built from.
Expect "no transit route" from Google for a lot of minibus-heavy pairs --
that's a coverage gap on Google's side, not automatically a Guidy bug.
Those are recorded separately from genuine mismatches, not conflated
with them.

Usage (PowerShell):
    $env:GOOGLE_ROUTES_API_KEY = "your key here"
    python route_comparison_audit.py

Requires:
  - The Guidy backend already running locally (this hits its real
    /api/route endpoint, exercising the exact same code path the app
    uses -- not the engine directly).
  - `requests` (already a dependency, used by osrm_client.py).

Options:
    --guidy-url URL       default http://localhost:8000
    --grid-size N         geographic grid resolution, default 6 (6x6=36 cells)
    --per-point N         destinations checked per origin point, default 3
    --rate-limit-sec N    pause between Google API calls, default 0.3
"""
import argparse
import csv
import datetime
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import requests

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = ",".join([
    "routes.duration",
    "routes.distanceMeters",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.distanceMeters",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.transitDetails.transitLine.nameShort",
    "routes.legs.steps.transitDetails.transitLine.name",
    "routes.legs.steps.transitDetails.transitLine.vehicle.type",
    "routes.legs.steps.transitDetails.stopCount",
])

CACHE_PATH = Path("route_comparison_cache.json")


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_stops(gtfs_path="gtfs_data/stops.txt"):
    stops = []
    with open(gtfs_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float(row["stop_lat"])
                lon = float(row["stop_lon"])
            except (TypeError, ValueError, KeyError):
                continue
            stops.append({"id": row.get("stop_id"), "name": row.get("stop_name"), "lat": lat, "lon": lon})
    return stops


def build_grid_representatives(stops, grid_size=6):
    """Pick one representative stop per cell of a grid_size x grid_size
    grid over the stops' bounding box -- gives geographically spread
    trip endpoints across the whole coverage area without needing
    separately curated addresses."""
    lats = [s["lat"] for s in stops]
    lons = [s["lon"] for s in stops]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    lat_step = (max_lat - min_lat) / grid_size or 1.0
    lon_step = (max_lon - min_lon) / grid_size or 1.0

    reps = {}
    for s in stops:
        row = min(int((s["lat"] - min_lat) / lat_step), grid_size - 1)
        col = min(int((s["lon"] - min_lon) / lon_step), grid_size - 1)
        cell_lat = min_lat + (row + 0.5) * lat_step
        cell_lon = min_lon + (col + 0.5) * lon_step
        d = haversine(s["lat"], s["lon"], cell_lat, cell_lon)
        key = (row, col)
        if key not in reps or d < reps[key][0]:
            reps[key] = (d, s)
    return [s for _, s in reps.values()]


def build_pairs(representatives, per_point=3, seed=42):
    """Pairs each representative with a few geographically distant
    others -- favors real cross-city trips over trivial short hops,
    since those are the interesting case for a routing-quality audit."""
    rng = random.Random(seed)
    pairs = []
    seen = set()
    for origin in representatives:
        others = sorted(
            (d for d in representatives if d["id"] != origin["id"]),
            key=lambda d: haversine(origin["lat"], origin["lon"], d["lat"], d["lon"]),
            reverse=True,
        )
        picks = others[: max(per_point * 2, 1)]
        rng.shuffle(picks)
        for dest in picks[:per_point]:
            key = tuple(sorted([origin["id"], dest["id"]]))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((origin, dest))
    return pairs


def call_guidy(guidy_url, o, d):
    try:
        resp = requests.get(
            f"{guidy_url}/api/route",
            params={
                "start_lat": o["lat"], "start_lon": o["lon"],
                "end_lat": d["lat"], "end_lon": d["lon"],
                "lang": "en",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        options = data.get("options", [])
        fastest = next((op for op in options if op.get("type") == "Fastest"), options[0] if options else None)
        if not fastest:
            return {"error": data.get("error", "no options returned")}
        vehicles = [
            step.get("vehicle_type") for step in fastest.get("instructions", [])
            if step.get("vehicle_type") and step.get("vehicle_type") != "walk"
        ]
        # The response's display-ready field is "time" (a formatted
        # string, e.g. "70"), not "raw_time" -- that key doesn't exist in
        # the final API response at all (see RouteOptionsScreen.dart /
        # TripRecapScreen.dart, which read option['time'] the same way).
        try:
            time_min = float(fastest.get("time"))
        except (TypeError, ValueError):
            time_min = None
        return {"time_min": time_min, "distance_m": fastest.get("distance_m"), "vehicles": vehicles}
    except Exception as e:
        return {"error": str(e)}


def next_departure_rfc3339(hour_local=9, tz_offset_hours=3):
    """
    A fixed, always-in-the-future departure instant for Google to plan
    against: the next occurrence of hour_local (default 09:00) Cairo time
    (UTC+3, no DST as of 2026), formatted as RFC3339 UTC.

    Pinning this matters far more than it looks. Google's Routes API
    departs NOW when no departureTime is given, and a TRANSIT duration
    then INCLUDES however long you would wait for service to resume. The
    2026-08-22 audit ran at ~21:35 Cairo and Google returned 400-760
    MINUTE trips (7-12 hours) for journeys it had priced at ~130 minutes
    earlier the same day -- it was routing riders through an overnight
    wait for the 06:00 first service. Guidy's own numbers barely moved
    between the two runs, so the median ratio appeared to collapse from
    0.66 to 0.24 and read like a catastrophic Guidy regression when
    nothing about Guidy had changed. Without a pinned departure time this
    audit is not comparable across runs at all.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    cairo = now + datetime.timedelta(hours=tz_offset_hours)
    target = cairo.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target <= cairo:
        target += datetime.timedelta(days=1)
    return (target - datetime.timedelta(hours=tz_offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def call_google(api_key, o, d, departure_time=None):
    body = {
        "origin": {"location": {"latLng": {"latitude": o["lat"], "longitude": o["lon"]}}},
        "destination": {"location": {"latLng": {"latitude": d["lat"], "longitude": d["lon"]}}},
        "travelMode": "TRANSIT",
        "languageCode": "en-US",
        "units": "METRIC",
    }
    if departure_time:
        body["departureTime"] = departure_time
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    try:
        resp = requests.post(GOOGLE_ROUTES_URL, json=body, headers=headers, timeout=15)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        routes = data.get("routes", [])
        if not routes:
            # Very likely a Google transit-coverage gap, not a real
            # "no route exists" -- see module docstring caveat.
            return {"no_google_transit_data": True}
        route = routes[0]
        duration_raw = route.get("duration", "0s")
        try:
            duration_min = round(int(str(duration_raw).rstrip("s")) / 60, 1)
        except ValueError:
            duration_min = None
        vehicles = []
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                if step.get("travelMode") == "TRANSIT":
                    line = step.get("transitDetails", {}).get("transitLine", {})
                    vt = line.get("vehicle", {}).get("type")
                    name = line.get("nameShort") or line.get("name")
                    vehicles.append(f"{vt}:{name}")
        return {"time_min": duration_min, "distance_m": route.get("distanceMeters"), "vehicles": vehicles}
    except Exception as e:
        return {"error": str(e)}


def load_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    tmp = CACHE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    tmp.replace(CACHE_PATH)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--guidy-url", default="http://localhost:8000")
    parser.add_argument("--grid-size", type=int, default=6)
    parser.add_argument("--per-point", type=int, default=3)
    parser.add_argument("--rate-limit-sec", type=float, default=0.3)
    parser.add_argument(
        "--departure-hour", type=int, default=9,
        help="Cairo-local hour to pin Google's departure time to (default 9, "
             "i.e. the next 09:00). See next_departure_rfc3339 -- leaving this "
             "unpinned makes runs incomparable.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("GOOGLE_ROUTES_API_KEY")
    if not api_key:
        print('ERROR: set GOOGLE_ROUTES_API_KEY first (PowerShell: $env:GOOGLE_ROUTES_API_KEY = "...")')
        sys.exit(1)

    departure_time = next_departure_rfc3339(hour_local=args.departure_hour)
    print(f"Pinning Google departure time to {departure_time} "
          f"(next {args.departure_hour:02d}:00 Cairo) so runs stay comparable.")

    stops = load_stops()
    print(f"Loaded {len(stops)} stops.")
    reps = build_grid_representatives(stops, args.grid_size)
    print(f"{len(reps)} geographic representative points (grid {args.grid_size}x{args.grid_size}).")
    pairs = build_pairs(reps, per_point=args.per_point)
    print(f"{len(pairs)} OD pairs to check.")

    cache = load_cache()
    if cache:
        print(f"Resuming -- {len(cache)} pairs already cached from a previous run.")

    checked = 0
    for o, d in pairs:
        key = f"{o['id']}__{d['id']}"
        if key in cache:
            continue
        guidy = call_guidy(args.guidy_url, o, d)
        google = call_google(api_key, o, d, departure_time=departure_time)
        cache[key] = {
            "origin": {"id": o["id"], "name": o["name"], "lat": o["lat"], "lon": o["lon"]},
            "destination": {"id": d["id"], "name": d["name"], "lat": d["lat"], "lon": d["lon"]},
            "guidy": guidy,
            "google": google,
        }
        checked += 1
        if checked % 10 == 0:
            save_cache(cache)
            print(f"  ...{checked} new pairs checked this run, {len(cache)} total cached (of {len(pairs)})")
        time.sleep(args.rate_limit_sec)

    save_cache(cache)
    print(f"Done. {len(cache)} pairs total in {CACHE_PATH}.")


if __name__ == "__main__":
    main()
