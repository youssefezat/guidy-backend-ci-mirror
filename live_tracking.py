"""
Crowdsourced live-vehicle tracking: riders' own phones, while actively
riding a leg of a computed trip, periodically report their GPS position.
Those reports are the only "vehicle position" data this system has --
there is no driver-side hardware or dedicated fleet-tracking feed to
plug into (see the 2026-08-22 design doc in the Guidy project for the
product reasoning: rather than trying to determine the one "correct"
GTFS-timed route among several that cover the same corridor, show the
rider every interchangeable route and let real-time position break the
tie for them).

This module is intentionally standalone (no import of raptor_engine)
so it can be unit tested and reasoned about on its own; GTFSRaptorEngine
owns one instance of LivePositionStore (self.live_store) and calls into
it from _find_equivalent_routes().

WHAT A "SESSION" IS
--------------------
A session_id is a client-generated identifier (the app mints one when a
rider taps "start navigation" on a transit leg and clears it on
transfer/arrival) standing in for "one continuous ride" -- a proxy for
one physical vehicle. Two different riders on the same physical bus
show up as two different sessions; that's fine, both are independently
valid position samples for that route+direction and only make the ETA
estimate more robust, not double a vehicle. There is no way for
crowdsourced phone data to know real vehicle IDs, so this system never
claims to track "the 7:14 AM bus" specifically -- only "someone is
riding route X, direction Y, right now, here."

NO PERSISTENCE
----------------
Positions live in memory only and expire after POSITION_TTL_SEC of no
new report. This is deliberately not a historical record or an
analytics store -- just enough state to answer "is anyone riding this
route right now, and if so, how far are they from a given stop."
"""

import math
import time

# A reported position stops counting as "the vehicle is still there"
# this long after its last update. Riders are expected to report every
# ~5-15s while actively riding (see live_tracking_service.dart on the
# frontend); 90s covers a couple of missed reports (a dead zone, an
# app backgrounded briefly) without holding on to a genuinely stale
# position and quoting an ETA for a vehicle that's long gone.
POSITION_TTL_SEC = 90

# A rolling window of this many recent positions per session is kept --
# enough to smooth over one noisy GPS fix without holding unbounded
# history for a long-running ride.
HISTORY_WINDOW = 5

# Speed sanity bounds for a rider-derived speed estimate. Below
# MIN_TRUSTED_SPEED_MPS the vehicle is plausibly stopped at a light/stuck
# in traffic/picking up passengers -- extrapolating a near-zero speed
# into a distance-based ETA would wildly overestimate wait time, so a
# typical-for-vehicle-type default is used instead. Above
# MAX_TRUSTED_SPEED_MPS the delta is almost certainly a GPS jump rather
# than the vehicle's true speed (mirrors MAX_PLAUSIBLE_SPEED_MPS's role
# elsewhere in raptor_engine.py's data-quality guards).
MIN_TRUSTED_SPEED_MPS = 1.0
MAX_TRUSTED_SPEED_MPS = 25.0

# Fallback speed used when a session exists (someone is riding) but
# there isn't yet a trustworthy derived speed for it (first report, or
# the vehicle is currently stationary) -- rough real-world Cairo
# surface-street/metro averages, not meant to be precise.
DEFAULT_SPEED_MPS = {
    "bus": 6.0,
    "minibus": 7.0,
    "microbus": 7.0,
    "metro": 11.0,
}


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(a)))


class LivePositionStore:
    def __init__(self):
        self._sessions = {}  # session_id -> {route_id, direction_id, history: [(lat,lon,ts)], last_seen}

    def report_position(self, session_id, route_id, direction_id, lat, lon, ts=None):
        """Record one GPS fix for a rider actively riding route_id/direction_id.
        Starts a fresh history if this session is new, or if it switched
        to a different route/direction since its last report (a transfer,
        or the rider corrected a mis-tagged board) -- otherwise speed
        would be computed across a seam that doesn't represent one
        continuous ride."""
        ts = ts if ts is not None else time.time()
        direction_id = direction_id or ""
        sess = self._sessions.get(session_id)
        if sess is None or sess["route_id"] != route_id or sess["direction_id"] != direction_id:
            sess = {"route_id": route_id, "direction_id": direction_id, "history": []}
            self._sessions[session_id] = sess
        sess["history"].append((lat, lon, ts))
        sess["history"] = sess["history"][-HISTORY_WINDOW:]
        sess["last_seen"] = ts

    def end_session(self, session_id):
        """Explicit stop (rider tapped 'I've arrived'/'stop sharing', or
        transferred to a different leg) -- removes the session immediately
        rather than waiting out the TTL, so a just-alighted rider's stale
        position doesn't linger as a false "vehicle is here" signal for
        up to POSITION_TTL_SEC."""
        self._sessions.pop(session_id, None)

    def _cleanup(self, now):
        stale = [sid for sid, s in self._sessions.items() if now - s["last_seen"] > POSITION_TTL_SEC]
        for sid in stale:
            del self._sessions[sid]

    def active_vehicle_count(self, now=None):
        """Total number of currently-fresh rider sessions, across all
        routes -- cheap health/coverage signal for /api/health or admin
        use, not used in ETA math itself."""
        now = now if now is not None else time.time()
        self._cleanup(now)
        return len(self._sessions)

    def active_vehicles(self, route_id, direction_id, now=None):
        """Every currently-fresh rider-reported position on this exact
        route+direction, each with a derived speed (None if not yet
        computable -- fewer than 2 fixes, or the fixes are too close in
        time to give a stable estimate)."""
        now = now if now is not None else time.time()
        direction_id = direction_id or ""
        self._cleanup(now)
        out = []
        for sess in self._sessions.values():
            if sess["route_id"] != route_id or sess["direction_id"] != direction_id:
                continue
            hist = sess["history"]
            lat, lon, ts = hist[-1]
            speed_mps = None
            if len(hist) >= 2:
                lat0, lon0, ts0 = hist[-2]
                dt = ts - ts0
                if dt >= 2:  # avoid dividing by a near-zero interval and amplifying GPS jitter
                    speed_mps = _haversine_m(lat0, lon0, lat, lon) / dt
            out.append({"lat": lat, "lon": lon, "speed_mps": speed_mps, "last_seen": sess["last_seen"]})
        return out

    def get_eta(self, route_id, direction_id, target_lat, target_lon, vehicle_type="bus", now=None, distance_fn=None):
        """ETA (seconds) for every currently-tracked vehicle on this
        route+direction to reach (target_lat, target_lon) -- normally the
        stop the rider is waiting at.

        By default the distance used is straight-line (haversine), same
        as before. Pass `distance_fn(vehicle_lat, vehicle_lon) -> meters`
        to use something more accurate instead -- raptor_engine.py passes
        one that snaps both points to the route's real shape and measures
        along it, since a straight line can badly underestimate distance
        wherever the real road needs a detour a straight line doesn't see
        (a bridge, a ring-road loop; see the 2026-08-22 sanity-check
        doc's circuity findings, up to 5.8x for river crossings in this
        feed). This module stays GTFS/shape-agnostic on purpose -- it
        just calls whatever distance function it's handed.

        Returns a list sorted soonest-first; empty if nobody is
        currently reporting a live position on this route+direction."""
        now = now if now is not None else time.time()
        etas = []
        for v in self.active_vehicles(route_id, direction_id, now=now):
            speed = v["speed_mps"]
            trusted = speed is not None and MIN_TRUSTED_SPEED_MPS <= speed <= MAX_TRUSTED_SPEED_MPS
            eta_speed = speed if trusted else DEFAULT_SPEED_MPS.get(vehicle_type, 6.0)
            if distance_fn is not None:
                dist_m = distance_fn(v["lat"], v["lon"])
            else:
                dist_m = _haversine_m(v["lat"], v["lon"], target_lat, target_lon)
            etas.append({
                "eta_sec": round(dist_m / eta_speed),
                "distance_m": round(dist_m),
                "reported_speed_mps": round(speed, 1) if speed is not None else None,
                "age_sec": round(now - v["last_seen"]),
            })
        etas.sort(key=lambda e: e["eta_sec"])
        return etas
