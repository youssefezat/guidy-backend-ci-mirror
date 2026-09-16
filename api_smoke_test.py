"""
Boot the API and exercise every GET endpoint against the real feed.

WHY THIS EXISTS
---------------
The unittest suite covers the routing engine. Nothing covered main.py, so
the FastAPI layer was never executed anywhere except a running Docker
container on one machine -- and FastAPI does a lot of work at import time
(decorators, Query validators, response models) that a unit test of the
engine never touches. Two rail endpoints were added without ever being
started once.

A wrong Query pattern is the sharpest example: `vehicle_type` is validated
against a regex whitelist, so a mode missing from it is not merely
unfiltered -- the request is rejected with a 422. The Lines browser's new
Monorail and LRT chips would have returned errors rather than results, and
no test in this repo could have noticed.

Run locally the same way CI does:
    USE_OSRM=false uvicorn main:app --port 8000 &
    python3 api_smoke_test.py
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")

failures = []
checks = 0


def get(path, **params):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return e.code, body
    except urllib.error.URLError as e:
        # Server never came up, or died mid-run. Report it as a failed
        # check rather than a traceback -- in CI the traceback buries the
        # one line that matters under 20 frames of urllib internals.
        return 0, f"connection failed: {e.reason}"


def check(label, condition, detail=""):
    global checks
    checks += 1
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


print("health")
status, body = get("/api/health")
check("health responds 200", status == 200, f"got {status}")
check("graph loaded", isinstance(body, dict) and body.get("stops_loaded", 0) > 3000,
      f"stops_loaded={body.get('stops_loaded') if isinstance(body, dict) else body}")

print("\nroute browser -- the new modes must survive the vehicle_type whitelist")
for mode, expected in [("monorail", 1), ("lrt", 2), ("apm", 1), ("metro", 3)]:
    status, body = get("/api/routes", vehicle_type=mode, limit=50)
    # A 422 here means the mode is missing from main.py's Query pattern.
    check(f"/api/routes vehicle_type={mode} accepted", status == 200, f"got {status}: {body}")
    if status == 200:
        n = len(body.get("routes", body if isinstance(body, list) else []))
        check(f"/api/routes vehicle_type={mode} -> {expected} route(s)", n == expected, f"got {n}")

print("\nroute detail")
status, body = get("/api/routes/RL_MONO_EN")
check("monorail route detail 200", status == 200, f"got {status}")
if status == 200:
    check("monorail vehicle_type", body.get("vehicle_type") == "monorail", body.get("vehicle_type"))
    # Banded modes must report null for a single fare and expose the bands
    # instead -- one number would be a guess dressed up as a fact.
    check("monorail fare_egp is null", body.get("fare_egp") is None, body.get("fare_egp"))
    check("monorail exposes fare bands", bool(body.get("fare_bands")), body.get("fare_bands"))
    dirs = body.get("directions") or []
    check("monorail has 2 directions", len(dirs) == 2, len(dirs))
    if dirs:
        check("monorail direction has 22 stations",
              len(dirs[0].get("stops", [])) == 22, len(dirs[0].get("stops", [])))

print("\narabic localisation (892 route names were parsed and never used before)")
status, body = get("/api/routes/RL_MONO_EN", lang="ar")
if status == 200:
    desc = body.get("description", "")
    check("monorail description is Arabic", any("؀" <= c <= "ۿ" for c in desc), desc[:40])
    first = (body.get("directions") or [{}])[0].get("stops") or [{}]
    check("station name is Arabic",
          any("؀" <= c <= "ۿ" for c in first[0].get("name", "")),
          first[0].get("name"))

print("\nrail stations")
status, rail = get("/api/rail/stations")
check("/api/rail/stations 200", status == 200, f"got {status}")
status_m, metro = get("/api/metro/stations")
check("/api/metro/stations 200", status_m == 200, f"got {status_m}")
if status == 200 and status_m == 200:
    rs, ms = rail.get("stations", []), metro.get("stations", [])
    check("rail station set is larger than metro-only", len(rs) > len(ms), f"{len(rs)} vs {len(ms)}")
    check("metro-only really is metro-only",
          all("metro" in s.get("modes", []) for s in ms), "a non-metro station leaked in")
    modes = {m for s in rs for m in s.get("modes", [])}
    check("rail covers metro+monorail+lrt", {"metro", "monorail", "lrt"} <= modes, sorted(modes))

    by_name = {s["name"]: s["stop_id"] for s in rs}

    print("\nrail planner -- the interchange the general walk radius could not build")
    if "Adly Mansour" in by_name and "Justice City" in by_name:
        status, plan = get("/api/rail/plan",
                           from_stop=by_name["Adly Mansour"], to_stop=by_name["Justice City"])
        check("LRT -> monorail journey plans", status == 200, f"got {status}: {plan}")
        if status == 200:
            legs = {l["line"]["vehicle_type"] for l in plan.get("legs", [])}
            check("journey uses lrt and monorail", legs == {"lrt", "monorail"}, legs)
            fb = plan.get("fare_breakdown", [])
            # Two modes ridden is two tickets. Pooling them into one band
            # would quote roughly a third of the real price.
            check("fare is itemised per mode", len(fb) == 2, fb)
            check("total equals the sum of its tickets",
                  plan.get("fare_egp") == sum(f["fare_egp"] for f in fb),
                  f"{plan.get('fare_egp')} vs {fb}")

        # The Metro tab must stay metro-only: a rider asking for the metro
        # is asking for the certainty of a train they already know.
        status, _ = get("/api/metro/plan",
                        from_stop=by_name["Adly Mansour"], to_stop=by_name["Justice City"])
        check("metro-only planner refuses a monorail destination", status == 400, f"got {status}")
    else:
        check("Adly Mansour and Justice City present", False, sorted(by_name)[:5])

print("\ntrip planner")
# Deliberately NOT asserting which modes come back. /api/route has no
# injectable clock, so it filters against real service windows -- the
# monorail runs 06:00-18:00 Cairo, and CI runs at arbitrary UTC hours. A
# mode assertion here would pass or fail based on the time of day, which
# is worse than no assertion at all. What must hold at any hour is that
# the endpoint answers coherently.
status, body = get("/api/route",
                   start_lat=30.0900, start_lon=31.3300,
                   end_lat=30.0059, end_lon=31.7701)
check("/api/route answers 200", status == 200, f"got {status}")
if status == 200:
    ok = body.get("success") is True and body.get("options")
    outside_hours = body.get("success") is False and body.get("reason")
    check("/api/route returns options or an explained refusal", bool(ok or outside_hours), body)
    if ok:
        check("/api/route exposes is_only_route flag", "is_only_route" in body, body)

status_no_metro, body_no_metro = get(
    "/api/route",
    start_lat=30.0538, start_lon=31.3653,
    end_lat=30.0385, end_lon=31.2117,
    exclude_metro="true",
)
check("/api/route exclude_metro=true answers 200", status_no_metro == 200, f"got {status_no_metro}")
if status_no_metro == 200 and body_no_metro.get("success"):
    options = body_no_metro.get("options", [])
    metro_in_options = any(
        i.get("vehicle_type") == "metro" or i.get("route_id") in {"NAT_L1", "NAT_L2", "NAT_L3"}
        for opt in options
        for i in opt.get("instructions", [])
    )
    check("/api/route exclude_metro=true contains no metro legs", not metro_in_options, options)

status_prefs, body_prefs = get(
    "/api/route",
    start_lat=30.0538, start_lon=31.3653,
    end_lat=30.0385, end_lon=31.2117,
    min_walk="true",
    min_transfers="true",
)
check("/api/route with min_walk and min_transfers answers 200", status_prefs == 200, f"got {status_prefs}")

print("\nstations")
status, body = get("/api/stations")
check("/api/stations 200", status == 200, f"got {status}")

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("\nfailed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("API smoke test passed.")
