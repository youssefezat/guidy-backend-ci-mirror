"""
Unit tests for live_tracking.LivePositionStore.

Covers session lifecycle, TTL expiry, speed derivation, speed clamping,
history window limits, ETA calculation, and edge cases.
"""

import math
import time
import unittest

from live_tracking import (
    LivePositionStore,
    POSITION_TTL_SEC,
    HISTORY_WINDOW,
    MIN_TRUSTED_SPEED_MPS,
    MAX_TRUSTED_SPEED_MPS,
    DEFAULT_SPEED_MPS,
    _haversine_m,
)


class TestReportAndRetrieve(unittest.TestCase):
    """Basic session creation and retrieval."""

    def setUp(self):
        self.store = LivePositionStore()
        self.now = 1000000.0

    def test_report_creates_retrievable_session(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        vehicles = self.store.active_vehicles("route_A", "0", now=self.now)
        self.assertEqual(len(vehicles), 1)
        self.assertAlmostEqual(vehicles[0]["lat"], 30.0)
        self.assertAlmostEqual(vehicles[0]["lon"], 31.0)

    def test_no_vehicles_on_untracked_route(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        vehicles = self.store.active_vehicles("route_B", "0", now=self.now)
        self.assertEqual(len(vehicles), 0)

    def test_multiple_sessions_same_route(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s2", "route_A", "0", 30.1, 31.1, ts=self.now)
        vehicles = self.store.active_vehicles("route_A", "0", now=self.now)
        self.assertEqual(len(vehicles), 2)

    def test_direction_id_filtering(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s2", "route_A", "1", 30.1, 31.1, ts=self.now)
        self.assertEqual(len(self.store.active_vehicles("route_A", "0", now=self.now)), 1)
        self.assertEqual(len(self.store.active_vehicles("route_A", "1", now=self.now)), 1)

    def test_empty_direction_normalised(self):
        """None and '' should be treated as the same direction."""
        self.store.report_position("s1", "route_A", None, 30.0, 31.0, ts=self.now)
        vehicles = self.store.active_vehicles("route_A", "", now=self.now)
        self.assertEqual(len(vehicles), 1)


class TestEndSession(unittest.TestCase):
    """Explicit session termination."""

    def setUp(self):
        self.store = LivePositionStore()
        self.now = 1000000.0

    def test_end_session_removes_positions(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.end_session("s1")
        self.assertEqual(len(self.store.active_vehicles("route_A", "0", now=self.now)), 0)

    def test_end_nonexistent_session_is_safe(self):
        """Ending a session that doesn't exist should not raise."""
        self.store.end_session("no_such_session")


class TestTTLExpiry(unittest.TestCase):
    """Positions older than POSITION_TTL_SEC are purged."""

    def setUp(self):
        self.store = LivePositionStore()
        self.now = 1000000.0

    def test_fresh_positions_survive_cleanup(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        vehicles = self.store.active_vehicles("route_A", "0", now=self.now + 10)
        self.assertEqual(len(vehicles), 1)

    def test_stale_positions_are_purged(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        stale_time = self.now + POSITION_TTL_SEC + 1
        vehicles = self.store.active_vehicles("route_A", "0", now=stale_time)
        self.assertEqual(len(vehicles), 0)

    def test_active_vehicle_count_reflects_cleanup(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s2", "route_B", "0", 30.1, 31.1, ts=self.now)
        self.assertEqual(self.store.active_vehicle_count(now=self.now), 2)
        self.assertEqual(self.store.active_vehicle_count(now=self.now + POSITION_TTL_SEC + 1), 0)


class TestSpeedDerivation(unittest.TestCase):
    """Speed computed from consecutive GPS fixes."""

    def setUp(self):
        self.store = LivePositionStore()
        self.now = 1000000.0

    def test_single_fix_has_no_speed(self):
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        vehicles = self.store.active_vehicles("route_A", "0", now=self.now)
        self.assertIsNone(vehicles[0]["speed_mps"])

    def test_two_fixes_derive_speed(self):
        """Two fixes 10 seconds apart at known positions should give a speed."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s1", "route_A", "0", 30.001, 31.0, ts=self.now + 10)
        vehicles = self.store.active_vehicles("route_A", "0", now=self.now + 10)
        self.assertIsNotNone(vehicles[0]["speed_mps"])
        self.assertGreater(vehicles[0]["speed_mps"], 0)

    def test_too_close_in_time_gives_no_speed(self):
        """Fixes < 2s apart are ignored to avoid GPS jitter amplification."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s1", "route_A", "0", 30.001, 31.0, ts=self.now + 1)
        vehicles = self.store.active_vehicles("route_A", "0", now=self.now + 1)
        self.assertIsNone(vehicles[0]["speed_mps"])

    def test_route_change_resets_history(self):
        """Switching routes clears history, so speed is None after transfer."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s1", "route_A", "0", 30.001, 31.0, ts=self.now + 10)
        # Transfer to a different route
        self.store.report_position("s1", "route_B", "0", 30.001, 31.0, ts=self.now + 20)
        vehicles = self.store.active_vehicles("route_B", "0", now=self.now + 20)
        self.assertEqual(len(vehicles), 1)
        self.assertIsNone(vehicles[0]["speed_mps"])


class TestHistoryWindow(unittest.TestCase):
    """Only the last HISTORY_WINDOW fixes are kept."""

    def setUp(self):
        self.store = LivePositionStore()
        self.now = 1000000.0

    def test_history_is_bounded(self):
        for i in range(HISTORY_WINDOW + 5):
            self.store.report_position(
                "s1", "route_A", "0", 30.0 + i * 0.001, 31.0, ts=self.now + i * 10
            )
        sess = self.store._sessions["s1"]
        self.assertEqual(len(sess["history"]), HISTORY_WINDOW)

    def test_latest_position_is_most_recent(self):
        for i in range(HISTORY_WINDOW + 3):
            self.store.report_position(
                "s1", "route_A", "0", 30.0 + i * 0.001, 31.0, ts=self.now + i * 10
            )
        vehicles = self.store.active_vehicles("route_A", "0", now=self.now + (HISTORY_WINDOW + 2) * 10)
        expected_lat = 30.0 + (HISTORY_WINDOW + 2) * 0.001
        self.assertAlmostEqual(vehicles[0]["lat"], expected_lat, places=5)


class TestGetEta(unittest.TestCase):
    """ETA estimation for waiting riders."""

    def setUp(self):
        self.store = LivePositionStore()
        self.now = 1000000.0

    def test_eta_with_trusted_speed(self):
        """When derived speed is within trusted bounds, use it for ETA."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s1", "route_A", "0", 30.001, 31.0, ts=self.now + 10)
        etas = self.store.get_eta("route_A", "0", 30.01, 31.0, vehicle_type="bus", now=self.now + 10)
        self.assertEqual(len(etas), 1)
        self.assertGreater(etas[0]["eta_sec"], 0)
        self.assertGreater(etas[0]["distance_m"], 0)

    def test_eta_uses_default_speed_for_single_fix(self):
        """Single fix has no derived speed -> fallback to mode default."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        etas = self.store.get_eta("route_A", "0", 30.01, 31.0, vehicle_type="bus", now=self.now)
        self.assertEqual(len(etas), 1)
        dist = _haversine_m(30.0, 31.0, 30.01, 31.0)
        expected_eta = round(dist / DEFAULT_SPEED_MPS["bus"])
        self.assertEqual(etas[0]["eta_sec"], expected_eta)

    def test_eta_with_custom_distance_fn(self):
        """Custom distance function is used instead of haversine."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        custom_distance = 5000.0
        etas = self.store.get_eta(
            "route_A", "0", 30.01, 31.0,
            vehicle_type="bus", now=self.now,
            distance_fn=lambda lat, lon: custom_distance,
        )
        expected_eta = round(custom_distance / DEFAULT_SPEED_MPS["bus"])
        self.assertEqual(etas[0]["eta_sec"], expected_eta)

    def test_eta_sorted_soonest_first(self):
        """Multiple vehicles: closest (soonest ETA) should be first."""
        self.store.report_position("s1", "route_A", "0", 30.009, 31.0, ts=self.now)
        self.store.report_position("s2", "route_A", "0", 30.0, 31.0, ts=self.now)
        etas = self.store.get_eta("route_A", "0", 30.01, 31.0, vehicle_type="bus", now=self.now)
        self.assertEqual(len(etas), 2)
        self.assertLessEqual(etas[0]["eta_sec"], etas[1]["eta_sec"])

    def test_eta_empty_for_no_vehicles(self):
        etas = self.store.get_eta("route_A", "0", 30.01, 31.0, vehicle_type="bus", now=self.now)
        self.assertEqual(etas, [])

    def test_speed_clamping_below_min(self):
        """Very slow speed (near-stationary) should use mode default."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now + 10)
        etas = self.store.get_eta("route_A", "0", 30.01, 31.0, vehicle_type="metro", now=self.now + 10)
        dist = _haversine_m(30.0, 31.0, 30.01, 31.0)
        expected_eta = round(dist / DEFAULT_SPEED_MPS["metro"])
        self.assertEqual(etas[0]["eta_sec"], expected_eta)

    def test_speed_clamping_above_max(self):
        """GPS teleport (huge jump in short time) should use mode default."""
        self.store.report_position("s1", "route_A", "0", 30.0, 31.0, ts=self.now)
        self.store.report_position("s1", "route_A", "0", 30.1, 31.0, ts=self.now + 10)
        etas = self.store.get_eta("route_A", "0", 30.2, 31.0, vehicle_type="bus", now=self.now + 10)
        dist = _haversine_m(30.1, 31.0, 30.2, 31.0)
        expected_eta = round(dist / DEFAULT_SPEED_MPS["bus"])
        self.assertEqual(etas[0]["eta_sec"], expected_eta)


class TestHaversine(unittest.TestCase):
    """Sanity checks for the haversine helper."""

    def test_same_point_is_zero(self):
        self.assertAlmostEqual(_haversine_m(30.0, 31.0, 30.0, 31.0), 0.0)

    def test_known_distance(self):
        dist = _haversine_m(30.0, 31.0, 31.0, 31.0)
        self.assertAlmostEqual(dist, 111_195, delta=500)


if __name__ == "__main__":
    unittest.main()
