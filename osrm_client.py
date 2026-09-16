"""
Client for a self-hosted OSRM (Open Source Routing Machine) instance,
used for real street-network walking directions instead of the
straight-line haversine estimates the engine used before.

Why self-hosted instead of Google Directions API: Directions API is
billed per request, which doesn't scale for a free-to-use transit app
making potentially thousands of walking-leg lookups a day. OSRM is
open-source (MIT licensed) and, once you've built the Cairo extract
locally, answering routing queries is unlimited and free -- the only
cost is the server you run it on.

SETUP (do this once, on whatever machine will run the backend):
  1. Download the Egypt OSM extract from Geofabrik:
     https://download.geofabrik.de/africa/egypt-latest.osm.pbf
  2. Build the foot-profile routing graph (uses OSRM's official Docker
     image, no local OSRM install needed):
       docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \\
         osrm-extract -p /opt/foot.lua /data/egypt-latest.osm.pbf
       docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \\
         osrm-partition /data/egypt-latest.osrm
       docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \\
         osrm-customize /data/egypt-latest.osrm
  3. Run the routing server (keep this running alongside the FastAPI
     backend -- e.g. as a second Docker service / systemd unit):
       docker run -t -i -p 5000:5000 -v "${PWD}:/data" \\
         ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld \\
         /data/egypt-latest.osrm
  4. Point this client at it via the OSRM_BASE_URL environment variable
     (defaults to http://localhost:5000, i.e. same machine).

If OSRM is unreachable (not set up yet, container down, etc.) every
method below falls back to the old haversine straight-line estimate
rather than failing the whole route request -- self-hosted infra can
go down, and a slightly-less-accurate walking leg beats a broken app.
"""

import json
import math
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import requests

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "http://localhost:5000")
OSRM_FALLBACK_URL = os.environ.get("OSRM_FALLBACK_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT_SEC = 1.0
OSRM_FALLBACK_TIMEOUT_SEC = 4.0
WALK_SPEED_MPS = 1.25  # must match GTFSRaptorEngine.WALK_SPEED_MPS


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


class OSRMClient:
    def __init__(self, base_url=OSRM_BASE_URL, fallback_url=OSRM_FALLBACK_URL, cache_db_path=None, enabled=True):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.fallback_url = fallback_url.rstrip("/") if fallback_url else ""
        self.enabled = enabled
        self.session = requests.Session()
        self._warned_local = False
        self._warned_fallback = False
        self._local_available = None

        if cache_db_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            gtfs_dir = os.path.join(base_dir, "gtfs_data")
            if os.path.isdir(gtfs_dir):
                self.cache_db_path = os.path.join(gtfs_dir, "osrm_walking_cache.db")
            else:
                self.cache_db_path = os.path.join(base_dir, "osrm_walking_cache.db")
        else:
            self.cache_db_path = cache_db_path

        if self.enabled:
            self._init_cache()

    def _is_local_available(self):
        if not self.base_url:
            return False
        if self._local_available is not None:
            return self._local_available
        try:
            test_url = f"{self.base_url}/route/v1/foot/31.0,30.0;31.01,30.01"
            resp = self.session.get(test_url, timeout=0.3)
            self._local_available = (resp.status_code == 200)
        except Exception:
            self._local_available = False
            if not self._warned_local:
                print(f"[OSRM] Local instance at {self.base_url} unreachable. Using fallback and cache.")
                self._warned_local = True
        return self._local_available

    def _init_cache(self):
        """Initializes persistent SQLite database for cached pedestrian routes."""
        if not self.enabled:
            return
        conn = None
        try:
            conn = sqlite3.connect(self.cache_db_path, timeout=5.0)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS walking_cache (
                    cache_key TEXT PRIMARY KEY,
                    distance_m REAL NOT NULL,
                    duration_sec REAL NOT NULL,
                    geometry_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        except Exception as e:
            print(f"[OSRM] Warning: Failed to initialize walking cache at {self.cache_db_path}: {e}")
        finally:
            if conn:
                conn.close()

    def _get_cache_key(self, lat1, lon1, lat2, lon2):
        # 5 decimal places ~= 1.1 meter resolution
        return f"{round(lat1, 5)},{round(lon1, 5)};{round(lat2, 5)},{round(lon2, 5)}"

    def _lookup_cache(self, key):
        if not self.enabled:
            return None
        conn = None
        try:
            conn = sqlite3.connect(self.cache_db_path, timeout=2.0)
            cur = conn.cursor()
            cur.execute("SELECT distance_m, duration_sec, geometry_json, source FROM walking_cache WHERE cache_key = ?", (key,))
            row = cur.fetchone()
            if row:
                return {
                    "distance_m": row[0],
                    "duration_sec": row[1],
                    "geometry": json.loads(row[2]),
                    "source": f"{row[3]}_cached",
                }
        except Exception:
            pass
        finally:
            if conn:
                conn.close()
        return None

    def _store_cache(self, key, dist, dur, geometry, source):
        if not self.enabled:
            return
        conn = None
        try:
            conn = sqlite3.connect(self.cache_db_path, timeout=5.0)
            conn.execute(
                "INSERT OR REPLACE INTO walking_cache (cache_key, distance_m, duration_sec, geometry_json, source) VALUES (?, ?, ?, ?, ?)",
                (key, dist, dur, json.dumps(geometry), source),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            if conn:
                conn.close()

    def _fallback(self, lat1, lon1, lat2, lon2):
        dist_m = _haversine(lat1, lon1, lat2, lon2)
        duration_sec = dist_m / WALK_SPEED_MPS
        geometry = [{"lat": lat1, "lon": lon1}, {"lat": lat2, "lon": lon2}]
        return {"distance_m": dist_m, "duration_sec": duration_sec, "geometry": geometry, "source": "haversine"}

    def walking_route(self, lat1, lon1, lat2, lon2):
        """Real walking distance/duration/path between two points, via
        OSRM's foot profile. Checks local persistent cache, then local OSRM,
        then online OSM fallback router before falling back to haversine."""
        cache_key = self._get_cache_key(lat1, lon1, lat2, lon2)
        cached = self._lookup_cache(cache_key)
        if cached:
            return cached

        if not self.enabled:
            return self._fallback(lat1, lon1, lat2, lon2)

        # Tier 1: Try local OSRM instance (fastest, private)
        if self._is_local_available():
            url = f"{self.base_url}/route/v1/foot/{lon1},{lat1};{lon2},{lat2}"
            try:
                resp = self.session.get(
                    url,
                    params={"overview": "full", "geometries": "geojson"},
                    timeout=OSRM_TIMEOUT_SEC,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        route = data["routes"][0]
                        coords = route["geometry"]["coordinates"]
                        geometry = [{"lat": c[1], "lon": c[0]} for c in coords]
                        dist = float(route["distance"])
                        dur = float(route["duration"])
                        direct_m = _haversine(lat1, lon1, lat2, lon2)
                        # Detect excessive detour / circular routes in pedestrian graph
                        if dist > max(direct_m * 2.1, 350):
                            return self._fallback(lat1, lon1, lat2, lon2)
                        self._store_cache(cache_key, dist, dur, geometry, "osrm_local")
                        return {
                            "distance_m": dist,
                            "duration_sec": dur,
                            "geometry": geometry,
                            "source": "osrm_local",
                        }
            except (requests.RequestException, ValueError, KeyError, IndexError):
                pass

        # Tier 2: Try online OSM foot router fallback
        if self.fallback_url:
            fallback_url = f"{self.fallback_url}/route/v1/foot/{lon1},{lat1};{lon2},{lat2}"
            try:
                resp = self.session.get(
                    fallback_url,
                    params={"overview": "full", "geometries": "geojson"},
                    timeout=OSRM_FALLBACK_TIMEOUT_SEC,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        route = data["routes"][0]
                        coords = route["geometry"]["coordinates"]
                        geometry = [{"lat": c[1], "lon": c[0]} for c in coords]
                        dist = float(route["distance"])
                        dur = float(route["duration"])
                        direct_m = _haversine(lat1, lon1, lat2, lon2)
                        # Detect excessive detour / circular routes in pedestrian graph
                        if dist > max(direct_m * 2.1, 350):
                            return self._fallback(lat1, lon1, lat2, lon2)
                        self._store_cache(cache_key, dist, dur, geometry, "osrm_public")
                        return {
                            "distance_m": dist,
                            "duration_sec": dur,
                            "geometry": geometry,
                            "source": "osrm_public",
                        }
            except (requests.RequestException, ValueError, KeyError, IndexError) as e:
                if not self._warned_fallback:
                    print(f"[OSRM] Fallback router at {self.fallback_url} failed ({e}), using haversine.")
                    self._warned_fallback = True

        return self._fallback(lat1, lon1, lat2, lon2)

    def walking_routes_batch(self, pairs):
        """
        Resolve several (lat1, lon1, lat2, lon2) walking legs concurrently
        instead of one at a time.

        A single /api/route request needs several of these -- one pair per
        boarding/alighting point, times up to four candidate itineraries
        (Recommended/Fastest/Cheapest/Alternative) -- and they were being
        looked up strictly sequentially. Each one is an independent network
        round trip (up to OSRM_TIMEOUT_SEC against a local instance, then up
        to OSRM_FALLBACK_TIMEOUT_SEC against the public fallback if that
        instance isn't running, which -- see the module docstring -- is a
        completely normal, expected state for this to be in on a dev/staging
        box). Six to eight of those back to back is exactly why a rider
        planning a trip could see the option cards take several seconds to
        populate: pure serial network latency with nothing computed in
        between. threading (not multiprocessing) is enough here because each
        lookup is I/O-bound -- Python releases the GIL while `requests`
        blocks on the socket -- so this turns N sequential round trips into
        roughly the duration of the single slowest one.

        `requests.Session` is safe to share across threads for plain
        concurrent reads like these (no per-call session mutation happens
        here), so every thread reuses the same connection pool rather than
        opening a fresh one per lookup.

        Returns a list of walking_route() results, one per input pair, in
        the same order as `pairs`. Empty input returns an empty list without
        spinning up a thread pool; a single pair is resolved directly rather
        than paying thread-pool overhead for something with nothing to
        parallelize against.
        """
        if not pairs:
            return []
        if len(pairs) == 1:
            return [self.walking_route(*pairs[0])]
        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as pool:
            return list(pool.map(lambda p: self.walking_route(*p), pairs))

    def driving_route(self, lat1, lon1, lat2, lon2):
        """Road-following driving route for snapping transit vehicle segments to actual roads."""
        cache_key = f"drive_{self._get_cache_key(lat1, lon1, lat2, lon2)}"
        cached = self._lookup_cache(cache_key)
        if cached:
            return cached

        direct_m = _haversine(lat1, lon1, lat2, lon2)
        fallback = {
            "distance_m": direct_m,
            "duration_sec": direct_m / 11.0,
            "geometry": [{"lat": lat1, "lon": lon1}, {"lat": lat2, "lon": lon2}],
            "source": "haversine"
        }

        # Try local or public driving router
        routers = []
        if self._is_local_available():
            routers.append((f"{self.base_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}", OSRM_TIMEOUT_SEC, "osrm_local_drive"))
        if self.fallback_url:
            routers.append((f"{self.fallback_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}", OSRM_FALLBACK_TIMEOUT_SEC, "osrm_public_drive"))

        for url, timeout, source in routers:
            try:
                resp = self.session.get(url, params={"overview": "full", "geometries": "geojson"}, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        route = data["routes"][0]
                        coords = route["geometry"]["coordinates"]
                        geometry = [{"lat": c[1], "lon": c[0]} for c in coords]
                        dist = float(route["distance"])
                        dur = float(route["duration"])
                        self._store_cache(cache_key, dist, dur, geometry, source)
                        return {
                            "distance_m": dist,
                            "duration_sec": dur,
                            "geometry": geometry,
                            "source": source,
                        }
            except Exception:
                continue

        return fallback

    def walking_table(self, coordinates):
        """
        Many-to-many walking durations for a list of (lat, lon) tuples,
        via local OSRM's Table service.
        Note: We intentionally DO NOT send large table matrices to public
        fallback routers to prevent overloading or being rate-limited.
        Returns a duration matrix (seconds), or None on failure (caller
        should fall back to candidate checking in that case).
        """
        if not self.enabled or not self._is_local_available() or len(coordinates) < 2:
            return None


        coord_str = ";".join(f"{lon},{lat}" for lat, lon in coordinates)
        url = f"{self.base_url}/table/v1/foot/{coord_str}"
        try:
            resp = self.session.get(url, params={"annotations": "duration,distance"}, timeout=2.0)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("code") != "Ok":
                return None
            return data
        except (requests.RequestException, ValueError, KeyError) as e:
            if not self._warned_local:
                print(f"[OSRM] Table request to local instance failed ({e}).")
                self._warned_local = True
            return None

