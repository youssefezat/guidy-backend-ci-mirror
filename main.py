import logging
import os
import sys

from fastapi import FastAPI, HTTPException, Query, Request

import coverage_gaps
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import uvicorn

from raptor_engine import GTFSRaptorEngine
from lookups import TransitLookups

# --- Logging ---------------------------------------------------------------
# Replaces the scattered print() calls with real logging so output can be
# filtered/redirected in production (e.g. to a file or log aggregator)
# instead of only ever going to stdout.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("guidy")

app = FastAPI(title="Guidy Transit API")

# --- Rate limiting ---------------------------------------------------------
# /api/route is unauthenticated and calls out to OSRM (and, once wired up,
# Google Maps) -- both metered/costly enough that this needs abuse
# protection now that the app is public rather than a local demo.
RATE_LIMIT_PER_MINUTE = os.environ.get("RATE_LIMIT_PER_MINUTE", "30")
# Riders reporting live position while actively riding a leg send one
# fix roughly every 5-15s (see live_tracking_service.dart) -- a much
# higher per-minute allowance than /api/route needs, since it's not a
# heavyweight compute call, but still capped to stop a runaway/abusive
# client from hammering the position store.
LIVE_POSITION_RATE_LIMIT_PER_MINUTE = os.environ.get("LIVE_POSITION_RATE_LIMIT_PER_MINUTE", "20")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- CORS --------------------------------------------------------------
# allow_credentials=True combined with a wildcard origin is actually
# invalid per the CORS spec (browsers will reject it) -- it was silently
# doing nothing useful before. Since this API currently only serves the
# Flutter mobile app (which isn't subject to CORS at all), credentials
# aren't needed. If a web client is ever added, replace allow_origins
# with the specific domain(s) that should be allowed rather than "*".
# POST is now needed alongside GET for the live-position reporting
# endpoints (crowdsourced GPS while riding -- see live_tracking.py).
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- Config --------------------------------------------------------------
# GTFS_PATH: override with the GTFS_PATH environment variable in
# production/deployment. The default resolves to the gtfs_data folder
# sitting right next to this file -- NOT a hardcoded absolute path from
# whatever machine this was first built on. That previous hardcoded path
# (C:\Users\Joeyyy\Downloads\...) broke the moment this folder was moved
# or re-cloned anywhere else, since it pointed at a location that only
# ever existed on one dev machine. Resolving relative to __file__ means
# this keeps working no matter where the repo lives, without needing the
# env var set at all for local dev.
GTFS_PATH = os.environ.get(
    "GTFS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtfs_data"),
)

# Set USE_OSRM=false to skip OSRM entirely and use straight-line haversine
# walking estimates (e.g. if you haven't set up a self-hosted OSRM
# instance yet -- see osrm_client.py for setup steps). OSRM_BASE_URL
# defaults to http://localhost:5000 if not set.
USE_OSRM = os.environ.get("USE_OSRM", "true").lower() != "false"
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL")

# Loose bounding box around Egypt, used only to reject obviously-bad
# coordinates (swapped lat/lon, (0,0) "null island", a client bug sending
# a different country's location) before wasting engine time on them.
# This is intentionally wider than the GTFS feed's actual stop coverage
# (~29.85-30.30 lat, ~30.88-31.76 lon around Greater Cairo) so it doesn't
# reject legitimate requests from just outside that cluster.
EGYPT_LAT_RANGE = (22.0, 32.0)
EGYPT_LON_RANGE = (24.0, 37.0)

logger.info("Booting up Guidy Backend...")
logger.info(
    "OSRM walking directions: %s",
    f"enabled ({OSRM_BASE_URL or 'http://localhost:5000'})" if USE_OSRM else "disabled -- using haversine fallback",
)

try:
    transit_engine = GTFSRaptorEngine(GTFS_PATH, use_osrm=USE_OSRM, osrm_base_url=OSRM_BASE_URL)
    transit_engine.load_data()
    # Route browser + metro planner. Reads structures the engine already
    # built, so this adds startup time in milliseconds, not seconds.
    lookups = TransitLookups(transit_engine)
    logger.info(
        "Lookups ready: %d routes browsable, %d metro stations.",
        len(transit_engine.route_stop_sequence), len(lookups.metro_stations()),
    )
    logger.info("Backend ready to receive coordinates!")
except FileNotFoundError as e:
    logger.critical(
        "Could not find GTFS data at '%s'. Set the GTFS_PATH environment variable "
        "to the folder containing stops.txt/routes.txt/trips.txt/stop_times.txt. (%s)",
        GTFS_PATH, e,
    )
    sys.exit(1)
except Exception as e:
    logger.critical("Failed to initialize the routing engine: %s", e)
    sys.exit(1)


def _validate_coords(start_lat, start_lon, end_lat, end_lon):
    for label, lat, lon in [("start", start_lat, start_lon), ("end", end_lat, end_lon)]:
        if not (EGYPT_LAT_RANGE[0] <= lat <= EGYPT_LAT_RANGE[1]) or not (EGYPT_LON_RANGE[0] <= lon <= EGYPT_LON_RANGE[1]):
            # `detail` is an OBJECT, not a string, on every error the app
            # shows to a rider. A server cannot localize its own prose --
            # it has no idea the client is running in Arabic -- so it sends
            # a stable `code` for the app to translate, plus an English
            # `message` as the fallback for anything that doesn't know the
            # code yet. This is why fares and errors used to appear in
            # English on an otherwise Arabic screen.
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "outside_coverage",
                    "message": f"The {label} coordinate ({lat}, {lon}) is outside "
                               f"Guidy's supported area (Egypt).",
                },
            )


def _validate_single_coord(lat, lon):
    if not (EGYPT_LAT_RANGE[0] <= lat <= EGYPT_LAT_RANGE[1]) or not (EGYPT_LON_RANGE[0] <= lon <= EGYPT_LON_RANGE[1]):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "outside_coverage",
                "message": f"The coordinate ({lat}, {lon}) is outside "
                           f"Guidy's supported area (Egypt).",
            },
        )


# --- Live position reporting ------------------------------------------
# See live_tracking.py's module docstring for the full design rationale:
# riders' own phones, while actively riding a leg the app told them to
# board, periodically report their GPS. There is no other source of
# real-time vehicle position in this system.
class LivePositionReport(BaseModel):
    # Client-generated identifier for "one continuous ride" (minted when
    # the rider starts navigating a transit leg, cleared on transfer or
    # arrival) -- a proxy for one physical vehicle, since crowdsourced
    # phone data can't know a real vehicle ID.
    session_id: str = Field(..., min_length=1, max_length=128)
    route_id: str = Field(..., min_length=1, max_length=128)
    direction_id: str = Field("", max_length=8)
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class LiveSessionEnd(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)


@app.get("/api/route")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
def get_route(
    request: Request,
    start_lat: float = Query(..., ge=-90, le=90),
    start_lon: float = Query(..., ge=-180, le=180),
    end_lat: float = Query(..., ge=-90, le=90),
    end_lon: float = Query(..., ge=-180, le=180),
    lang: str = Query("en", pattern="^(en|ar)$"),
    exclude_metro: bool = Query(False),
    min_walk: bool = Query(False),
    min_transfers: bool = Query(False),
):
    """
    Calculates the fastest multi-modal route using GPS coordinates.
    The RAPTOR engine automatically handles the First/Last mile snapping
    to the nearest GTFS stops on each end of the trip.
    Station/route names in the response are localized per `lang` when
    the GTFS feed's translations.txt has a matching entry.
    """
    _validate_coords(start_lat, start_lon, end_lat, end_lon)

    try:
        result = transit_engine.run_raptor_by_coords(
            start_lat, start_lon, end_lat, end_lon,
            lang=lang, exclude_metro=exclude_metro,
            min_walk=min_walk, min_transfers=min_transfers,
        )

        if result.get("success"):
            # A partial answer is still a coverage gap, and the most
            # actionable kind: a missing link with a known start and end.
            # Recorded here rather than in the engine so the engine stays
            # a pure calculation with no I/O in it.
            for opt in result.get("options", []):
                if opt.get("partial"):
                    coverage_gaps.record(
                        "partial", start_lat, start_lon, end_lat, end_lon,
                        uncovered_m=opt.get("uncovered_m"),
                        last_covered_stop=opt.get("last_covered_stop"),
                        lang=lang,
                    )
                    break

        if not result.get("success"):
            coverage_gaps.record(
                "no_route", start_lat, start_lon, end_lat, end_lon,
                reason=result.get("reason"), lang=lang,
            )
            # A known, "safe" failure (e.g. no route found) -- fine to
            # pass straight through to the client.
            # The engine already labels why (`reason`): outside_service_hours
            # when the whole network is closed for the night, no_coverage when
            # it genuinely can't get there. Those are different messages to a
            # rider, so the label travels rather than being flattened to prose.
            raise HTTPException(
                status_code=400,
                detail={
                    "code": result.get("reason", "no_route_found"),
                    "message": result.get("error", "Route calculation failed"),
                    # May be absent (e.g. outside_service_hours, where the
                    # nearest hub is not the useful fact). The app treats
                    # it as optional.
                    "nearest_hubs": result.get("nearest_hubs"),
                },
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        # Anything unexpected: log the full detail server-side, but don't
        # leak internal exception text/stack info to the client.
        logger.exception("Unexpected error calculating route (%s,%s -> %s,%s)", start_lat, start_lon, end_lat, end_lon)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "route_failed",
                "message": "Something went wrong calculating your route. Please try again.",
            },
        )


@app.post("/api/live/position")
@limiter.limit(f"{LIVE_POSITION_RATE_LIMIT_PER_MINUTE}/minute")
def report_live_position(request: Request, report: LivePositionReport):
    """
    Called periodically (every ~5-15s) by a rider's phone while they're
    actively riding a transit leg (see live_tracking_service.dart on the
    frontend). Feeds the crowdsourced live-position store that
    _find_equivalent_routes() reads from to attach a real-time ETA to
    each interchangeable route on a leg -- see live_tracking.py.

    Deliberately forgiving: an unrecognized route_id is rejected (a
    client bug, not something to silently store), but this endpoint
    never fails the request over normal live-data noise -- a late/out-
    of-order fix, a route the rider transferred off of, etc. are all
    handled by report_position()'s own logic, not surfaced as errors.
    """
    _validate_single_coord(report.lat, report.lon)
    if report.route_id not in transit_engine.routes:
        raise HTTPException(status_code=400, detail=f"Unknown route_id '{report.route_id}'.")

    transit_engine.live_store.report_position(
        report.session_id, report.route_id, report.direction_id, report.lat, report.lon,
    )
    return {"success": True}


@app.post("/api/live/end")
@limiter.limit(f"{LIVE_POSITION_RATE_LIMIT_PER_MINUTE}/minute")
def end_live_session(request: Request, payload: LiveSessionEnd):
    """
    Called once when a rider stops actively sharing position for a leg
    -- they arrived, transferred, or cancelled navigation. Removes the
    session immediately rather than waiting for it to expire on its own
    (see POSITION_TTL_SEC in live_tracking.py), so a just-alighted
    rider's last known position doesn't linger as a false "the vehicle
    is still here" signal for other riders checking ETAs.
    """
    transit_engine.live_store.end_session(payload.session_id)
    return {"success": True}


@app.get("/api/routes")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
def browse_routes(
    request: Request,
    q: str = Query("", max_length=120),
    # monorail / lrt / apm added with the Greater Cairo rail lines. The
    # pattern is a whitelist, so a mode missing from it is not merely
    # unfiltered -- the request is rejected with a 422, and the app's new
    # Monorail and LRT filter chips would have returned an error rather
    # than results.
    vehicle_type: str = Query(
        None, pattern="^(bus|minibus|microbus|metro|monorail|lrt|tram|apm)$"),
    # 200 used to be the ceiling here, which silently truncated an
    # unfiltered "All" browse (and even single-mode filters like microbus,
    # 590 routes) to a small head-of-list slice -- the browser can't show
    # a route it never received. The Cairo feed has ~1,090 browsable
    # routes as of Sept 2026; 2000 gives headroom for the feed to grow
    # without needing this raised again. search_routes() already builds
    # and sorts the full match list before slicing to `limit`, so raising
    # this doesn't add real server-side cost -- only the JSON payload size
    # for a genuinely unfiltered request grows.
    limit: int = Query(40, ge=1, le=2000),
    lang: str = Query("en", pattern="^(en|ar)$"),
):
    """
    Search routes by number OR by where they go.

    Searching the description is not a nicety here: route numbers in this
    feed are things like "CTA 65" and "MM M5", and several hundred microbus
    routes carry no number at all -- only a corridor ("Maadi Metro - Saqr
    Quraish"). A number-only lookup would simply not find them.
    """
    try:
        return {"success": True, "routes": lookups.search_routes(q, lang=lang, limit=limit,
                                                                 vehicle_type=vehicle_type)}
    except Exception:
        logger.exception("Route search failed for %r", q)
        raise HTTPException(status_code=500, detail="Could not search routes right now.")


@app.get("/api/routes/{route_id}")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
def route_detail(
    request: Request,
    route_id: str,
    lang: str = Query("en", pattern="^(en|ar)$"),
):
    """Every stop this route serves, in order, per direction."""
    try:
        detail = lookups.route_detail(route_id, lang=lang)
    except Exception:
        logger.exception("Route detail failed for %r", route_id)
        raise HTTPException(status_code=500, detail="Could not load that route right now.")
    if detail is None:
        raise HTTPException(status_code=404, detail="We don't have a route with that id.")
    return detail


@app.get("/api/metro/stations")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
def metro_stations(request: Request, lang: str = Query("en", pattern="^(en|ar)$")):
    """All metro stations, each with the line(s) it serves."""
    try:
        return {"success": True, "stations": lookups.metro_stations(lang=lang)}
    except Exception:
        logger.exception("Metro station list failed")
        raise HTTPException(status_code=500, detail="Could not load metro stations right now.")


@app.get("/api/metro/plan")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
def metro_plan(
    request: Request,
    from_stop: str = Query(..., min_length=1, max_length=128),
    to_stop: str = Query(..., min_length=1, max_length=128),
    lang: str = Query("en", pattern="^(en|ar)$"),
):
    """
    Metro-only journey between two stations: which line, which direction,
    where to change, how many stops, and the fare.

    Deliberately separate from /api/route. That plans the best trip using
    everything available and may well tell you to take a minibus. This
    answers a different question -- "I only want the metro" -- which is a
    real thing riders want when the surface network is gridlocked.
    """
    if from_stop == to_stop:
        raise HTTPException(status_code=400, detail="Choose two different stations.")
    try:
        result = lookups.plan_metro(from_stop, to_stop, lang=lang)
    except Exception:
        logger.exception("Metro plan failed %s -> %s", from_stop, to_stop)
        raise HTTPException(status_code=500, detail="Could not plan that metro trip right now.")
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="Both places need to be metro stations on the network.",
        )
    return result


# The metro endpoints above are kept exactly as they were -- the app's
# Metro tab calls them and a rider asking for the metro means the metro.
# These two answer the broader question the network can now support:
# "keep me on rails", across metro, monorail and LRT together.
RAIL_MODES = {"metro", "monorail", "lrt", "apm", "tram"}


def _parse_modes(modes: str):
    """`modes` is a comma-separated whitelist; None means every rail mode."""
    if not modes:
        return None
    wanted = {m.strip().lower() for m in modes.split(",") if m.strip()}
    unknown = wanted - RAIL_MODES
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown mode(s): {', '.join(sorted(unknown))}.",
        )
    return wanted


@app.get("/api/rail/stations")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
def rail_stations(
    request: Request,
    lang: str = Query("en", pattern="^(en|ar)$"),
    modes: str = Query(None, max_length=64),
):
    """Every rail station, with the line(s) and mode(s) it serves."""
    try:
        return {
            "success": True,
            "stations": lookups.rail_stations(lang=lang, modes=_parse_modes(modes)),
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Rail station list failed")
        raise HTTPException(status_code=500, detail="Could not load rail stations right now.")


@app.get("/api/rail/plan")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
def rail_plan(
    request: Request,
    from_stop: str = Query(..., min_length=1, max_length=128),
    to_stop: str = Query(..., min_length=1, max_length=128),
    lang: str = Query("en", pattern="^(en|ar)$"),
    modes: str = Query(None, max_length=64),
):
    """
    Rail-only journey across metro, monorail and LRT: which line, which
    direction, where to change, whether that change is on foot, how many
    stops, and the fare ITEMIZED PER MODE -- because a journey using two
    of them means buying two tickets, and one pooled number would leave a
    rider unable to check it against what they actually pay.
    """
    if from_stop == to_stop:
        raise HTTPException(status_code=400, detail="Choose two different stations.")
    try:
        result = lookups.plan_rail(from_stop, to_stop, lang=lang,
                                   modes=_parse_modes(modes))
    except HTTPException:
        raise
    except Exception:
        logger.exception("Rail plan failed %s -> %s", from_stop, to_stop)
        raise HTTPException(status_code=500, detail="Could not plan that rail trip right now.")
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="Both places need to be rail stations, and connected by rail.",
        )
    return result


@app.get("/api/stations")
def get_stations():
    """
    Returns every GTFS stop the engine knows about. Used by the app's
    backend-reachability check and for any future "browse stations" UI.
    """
    try:
        stations = [
            {"id": stop_id, "name": info["name"], "lat": info["lat"], "lon": info["lon"]}
            for stop_id, info in transit_engine.stops.items()
        ]
        return {"success": True, "count": len(stations), "stations": stations}
    except Exception:
        logger.exception("Unexpected error listing stations")
        raise HTTPException(status_code=500, detail="Could not load stations right now. Please try again.")


@app.get("/api/health")
def health_check():
    """Simple endpoint to verify the server (and that the GTFS graph loaded) is running."""
    return {
        "status": "ok",
        "stops_loaded": len(transit_engine.stops),
        "live_vehicles_tracked": transit_engine.live_store.active_vehicle_count(),
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    # Last-resort safety net: FastAPI would otherwise return a raw 500
    # with no body consistency for anything not already caught above.
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "An unexpected server error occurred."},
    )


if __name__ == "__main__":
    # Allows you to run the server directly by executing `python main.py`
    #
    # reload=True runs uvicorn's file-watcher, which restarts the whole
    # worker process on ANY change it sees under this directory -- and
    # kills whatever request happens to be in-flight at that moment. The
    # client sees a bare "Connection aborted" / RemoteDisconnected, not a
    # normal error response, because the server process is gone, not
    # because anything about that specific request was wrong. This is
    # the leading suspect behind a cluster of unexplained connection
    # resets seen during the route-comparison audit (see
    # claude/route-comparison-audit-2026-08-22.md): route_comparison_audit.py
    # checkpoints its cache file into this same working directory every
    # 10 pairs, which is exactly the kind of write a directory-wide
    # reload watcher can pick up as "code changed, restart" -- and the
    # crashes were not tied to any particular route/stop, consistent
    # with "whichever request was in-flight when a restart fired" rather
    # than a routing bug. Hot-reload is a dev convenience, not something
    # this service needs while under any kind of test/audit/production
    # traffic -- opt in explicitly with DEV_RELOAD=true only while
    # actively editing code and not simultaneously hitting the API.
    dev_reload = os.environ.get("DEV_RELOAD", "false").lower() == "true"

    # Pass the app OBJECT, not the "main:app" import string, whenever
    # reload is off. With the string form, `python main.py` loads the
    # engine TWICE: once as __main__ when this file runs, and again when
    # uvicorn imports "main" as a separate module object and re-executes
    # the module body. Confirmed in a real boot log 2026-08-23 -- two
    # complete "Booting up / Engine Ready in ~30s" cycles back to back
    # before uvicorn even started serving.
    #
    # That cost ~30 seconds of extra startup and, worse, kept two full
    # engines resident: two copies of the graph, the 532k-point shape
    # table and every index built on top of them.
    #
    # reload=True still needs the import string, because uvicorn has to
    # be able to re-import the module when a file changes -- so that path
    # keeps the double load. It is opt-in and dev-only anyway.
    if dev_reload:
        uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
    else:
        uvicorn.run(app, host="0.0.0.0", port=8000)
