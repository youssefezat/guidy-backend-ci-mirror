"""
Unit tests for osrm_client.OSRMClient.

Tests the 4-tier failover chain (cache → local OSRM → public OSRM → haversine)
using mocks — no real OSRM instance or network access needed.
Also tests the detour detection and cache persistence logic.
"""

import json
import math
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from osrm_client import OSRMClient, _haversine, WALK_SPEED_MPS


def _mock_osrm_response(distance=500.0, duration=400.0, coords=None):
    """Build a mock OSRM response."""
    if coords is None:
        coords = [[31.0, 30.0], [31.01, 30.01]]
    return MagicMock(
        status_code=200,
        json=lambda: {
            "code": "Ok",
            "routes": [{
                "distance": distance,
                "duration": duration,
                "geometry": {"coordinates": coords},
            }],
        },
    )


def _mock_failed_response():
    return MagicMock(status_code=500)


class TestDisabledClient(unittest.TestCase):
    """When enabled=False, everything falls back to haversine immediately."""

    def setUp(self):
        self.client = OSRMClient(enabled=False)

    def test_walking_route_returns_haversine(self):
        result = self.client.walking_route(30.0, 31.0, 30.01, 31.01)
        self.assertEqual(result["source"], "haversine")
        self.assertGreater(result["distance_m"], 0)
        self.assertGreater(result["duration_sec"], 0)

    def test_haversine_duration_uses_walk_speed(self):
        result = self.client.walking_route(30.0, 31.0, 30.01, 31.01)
        expected_dur = result["distance_m"] / WALK_SPEED_MPS
        self.assertAlmostEqual(result["duration_sec"], expected_dur, places=1)

    def test_geometry_is_straight_line(self):
        result = self.client.walking_route(30.0, 31.0, 30.01, 31.01)
        self.assertEqual(len(result["geometry"]), 2)
        self.assertAlmostEqual(result["geometry"][0]["lat"], 30.0)
        self.assertAlmostEqual(result["geometry"][1]["lat"], 30.01)


class TestCacheTier(unittest.TestCase):
    """Tier 0: SQLite cache returns results without hitting any server."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "test_cache.db")
        self.client = OSRMClient(
            base_url="http://unreachable:9999",
            fallback_url="",
            cache_db_path=self.cache_path,
            enabled=True,
        )

    def test_cache_round_trip(self):
        """Write a cache entry, then read it back."""
        key = self.client._get_cache_key(30.0, 31.0, 30.01, 31.01)
        self.client._store_cache(key, 500.0, 400.0, [{"lat": 30, "lon": 31}], "osrm_local")
        cached = self.client._lookup_cache(key)
        self.assertIsNotNone(cached)
        self.assertAlmostEqual(cached["distance_m"], 500.0)
        self.assertEqual(cached["source"], "osrm_local_cached")

    def test_cache_miss_returns_none(self):
        cached = self.client._lookup_cache("nonexistent_key")
        self.assertIsNone(cached)

    def test_cached_result_skips_network(self):
        """If cache has a hit, walking_route should return it without OSRM call."""
        key = self.client._get_cache_key(30.0, 31.0, 30.01, 31.01)
        self.client._store_cache(key, 500.0, 400.0, [{"lat": 30, "lon": 31}], "osrm_local")
        with patch.object(self.client.session, "get") as mock_get:
            result = self.client.walking_route(30.0, 31.0, 30.01, 31.01)
            mock_get.assert_not_called()
        self.assertEqual(result["source"], "osrm_local_cached")


class TestLocalOSRMTier(unittest.TestCase):
    """Tier 1: Local OSRM instance."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "test_cache.db")
        self.client = OSRMClient(
            base_url="http://localhost:5000",
            fallback_url="",
            cache_db_path=self.cache_path,
            enabled=True,
        )
        # Pretend local OSRM is available
        self.client._local_available = True

    def test_successful_local_response(self):
        with patch.object(self.client.session, "get",
                          return_value=_mock_osrm_response(distance=200.0, duration=160.0)):
            result = self.client.walking_route(30.0, 31.0, 30.001, 31.001)
        self.assertEqual(result["source"], "osrm_local")
        self.assertEqual(result["distance_m"], 200.0)

    def test_local_failure_falls_to_haversine(self):
        """When local OSRM fails and no fallback URL, fall back to haversine."""
        with patch.object(self.client.session, "get", return_value=_mock_failed_response()):
            result = self.client.walking_route(30.0, 31.0, 30.01, 31.01)
        self.assertEqual(result["source"], "haversine")


class TestFallbackTier(unittest.TestCase):
    """Tier 2: Public OSRM fallback."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "test_cache.db")
        self.client = OSRMClient(
            base_url="http://localhost:5000",
            fallback_url="https://router.project-osrm.org",
            cache_db_path=self.cache_path,
            enabled=True,
        )
        # Pretend local is NOT available, so it skips to fallback
        self.client._local_available = False

    def test_fallback_used_when_local_unavailable(self):
        with patch.object(self.client.session, "get",
                          return_value=_mock_osrm_response(distance=200.0, duration=160.0)):
            result = self.client.walking_route(30.0, 31.0, 30.001, 31.001)
        self.assertEqual(result["source"], "osrm_public")

    def test_fallback_failure_falls_to_haversine(self):
        with patch.object(self.client.session, "get", return_value=_mock_failed_response()):
            result = self.client.walking_route(30.0, 31.0, 30.01, 31.01)
        self.assertEqual(result["source"], "haversine")


class TestDetourDetection(unittest.TestCase):
    """Excessive detour / circular route detection."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "test_cache.db")
        self.client = OSRMClient(
            base_url="http://localhost:5000",
            fallback_url="",
            cache_db_path=self.cache_path,
            enabled=True,
        )
        self.client._local_available = True

    def test_excessive_detour_falls_back_to_haversine(self):
        """If OSRM returns a route > 2.1x direct distance, use haversine instead."""
        # direct distance between (30.0, 31.0) and (30.001, 31.001) is ~157m
        # Return a route claiming 50 km — clearly a circular path in the graph
        with patch.object(self.client.session, "get",
                          return_value=_mock_osrm_response(distance=50000.0)):
            result = self.client.walking_route(30.0, 31.0, 30.001, 31.001)
        self.assertEqual(result["source"], "haversine")


class TestCacheKeyResolution(unittest.TestCase):
    """Cache key rounding to ~1m resolution."""

    def setUp(self):
        self.client = OSRMClient(enabled=False)

    def test_same_key_for_nearby_points(self):
        key1 = self.client._get_cache_key(30.000001, 31.000001, 30.01, 31.01)
        key2 = self.client._get_cache_key(30.000002, 31.000002, 30.01, 31.01)
        self.assertEqual(key1, key2, "Points <1m apart should share a cache key")

    def test_different_key_for_distant_points(self):
        key1 = self.client._get_cache_key(30.0, 31.0, 30.01, 31.01)
        key2 = self.client._get_cache_key(30.1, 31.1, 30.01, 31.01)
        self.assertNotEqual(key1, key2)


class TestDrivingRoute(unittest.TestCase):
    """driving_route follows the same tiered pattern."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "test_cache.db")
        self.client = OSRMClient(
            base_url="http://localhost:5000",
            fallback_url="",
            cache_db_path=self.cache_path,
            enabled=True,
        )
        self.client._local_available = False

    def test_driving_falls_back_to_haversine(self):
        result = self.client.driving_route(30.0, 31.0, 30.01, 31.01)
        self.assertEqual(result["source"], "haversine")
        # driving fallback uses 11.0 m/s, not WALK_SPEED_MPS
        expected_dur = result["distance_m"] / 11.0
        self.assertAlmostEqual(result["duration_sec"], expected_dur, places=1)


class TestWalkingTable(unittest.TestCase):
    """walking_table returns None gracefully when OSRM is unavailable."""

    def setUp(self):
        self.client = OSRMClient(enabled=True, base_url="http://unreachable:9999", fallback_url="")
        self.client._local_available = False

    def test_returns_none_when_local_unavailable(self):
        result = self.client.walking_table([(30.0, 31.0), (30.01, 31.01)])
        self.assertIsNone(result)

    def test_returns_none_for_too_few_coords(self):
        self.client._local_available = True
        result = self.client.walking_table([(30.0, 31.0)])
        self.assertIsNone(result)

    def test_returns_none_when_disabled(self):
        client = OSRMClient(enabled=False)
        result = client.walking_table([(30.0, 31.0), (30.01, 31.01)])
        self.assertIsNone(result)


class TestWalkingRoutesBatch(unittest.TestCase):
    """walking_routes_batch resolves multiple legs concurrently but must
    still return results in the same order as the input pairs, regardless
    of which one finishes first -- callers zip the results back onto their
    original leg list positionally (see raptor_engine.py), so an
    out-of-order batch would silently attach the wrong walking leg to the
    wrong boarding/alighting point."""

    def setUp(self):
        self.client = OSRMClient(enabled=False)

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(self.client.walking_routes_batch([]), [])

    def test_empty_input_does_not_spin_up_a_thread_pool(self):
        with patch("osrm_client.ThreadPoolExecutor") as mock_pool:
            self.client.walking_routes_batch([])
        mock_pool.assert_not_called()

    def test_single_pair_bypasses_the_thread_pool(self):
        # A lone pair has nothing to parallelize against -- it should be
        # resolved directly rather than paying thread-pool setup cost.
        with patch("osrm_client.ThreadPoolExecutor") as mock_pool:
            with patch.object(
                self.client, "walking_route", return_value={"source": "stub"}
            ) as mock_route:
                result = self.client.walking_routes_batch([(30.0, 31.0, 30.01, 31.01)])
        mock_pool.assert_not_called()
        mock_route.assert_called_once_with(30.0, 31.0, 30.01, 31.01)
        self.assertEqual(result, [{"source": "stub"}])

    def test_batch_calls_walking_route_once_per_pair(self):
        pairs = [
            (30.0, 31.0, 30.01, 31.01),
            (30.1, 31.1, 30.11, 31.11),
            (30.2, 31.2, 30.21, 31.21),
        ]
        with patch.object(
            self.client, "walking_route", side_effect=lambda *p: {"pair": p}
        ) as mock_route:
            result = self.client.walking_routes_batch(pairs)
        self.assertEqual(mock_route.call_count, 3)
        self.assertEqual([r["pair"] for r in result], pairs)

    def test_batch_preserves_input_order_even_when_completion_order_differs(self):
        # Give each pair a different artificial delay so the threads finish
        # in a different order than they were submitted, then confirm the
        # returned list still lines up positionally with the input.
        # ThreadPoolExecutor.map guarantees this; a naive as_completed()
        # based implementation would not, so this is the actual regression
        # this test protects against.
        delays = {1: 0.06, 2: 0.01, 3: 0.03}
        pairs = [(0.0, 0.0, 0.0, float(i)) for i in (1, 2, 3)]

        def fake_walking_route(lat1, lon1, lat2, lon2):
            time.sleep(delays[int(lon2)])
            return {"pair_id": int(lon2)}

        with patch.object(self.client, "walking_route", side_effect=fake_walking_route):
            result = self.client.walking_routes_batch(pairs)

        self.assertEqual([r["pair_id"] for r in result], [1, 2, 3])


class TestHaversineFunction(unittest.TestCase):
    """Module-level _haversine helper."""

    def test_same_point_is_zero(self):
        self.assertAlmostEqual(_haversine(30.0, 31.0, 30.0, 31.0), 0.0)

    def test_known_distance(self):
        dist = _haversine(30.0, 31.0, 31.0, 31.0)
        self.assertAlmostEqual(dist, 111_195, delta=500)


if __name__ == "__main__":
    unittest.main()
