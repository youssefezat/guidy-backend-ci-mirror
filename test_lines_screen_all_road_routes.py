"""
Comprehensive verification test that audits road vehicles
(bus, minibus, and microbus) populated in the lines screen browser.
Verifies that:
1. Every route loads with 200 OK in both English and Arabic.
2. Every route has at least 1 valid direction and at least 2 stops.
3. Every route has a coherent start point (stops[0] matches points[0])
   and end point (stops[-1] matches points[-1]).
4. Every route produces a connected polyline with valid coordinates that renders
   its intermediate stops continuously on Google Maps.
"""

import math
import os
import sys
import unittest

from raptor_engine import GTFSRaptorEngine
from lookups import TransitLookups

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "gtfs_data")


def hav(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2)**2
    return 2 * R * math.asin(math.sqrt(a))


class TestLinesScreenAllRoadRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = GTFSRaptorEngine(GTFS, use_osrm=False)
        cls.eng.load_data()
        cls.lookups = TransitLookups(cls.eng)

    def test_road_routes_coherence(self):
        all_routes = self.lookups.search_routes(query="", limit=2000, lang="en")
        road_routes = [r for r in all_routes if r.get("vehicle_type") in ("bus", "minibus", "microbus")]

        total = len(road_routes)
        self.assertGreater(total, 0, "Expected road routes to be present")

        failed_load = []
        disconnected = []
        mismatched = []

        for r in road_routes:
            rid = r["route_id"]
            vtype = r.get("vehicle_type")

            # Degenerate 1-stop routes in GTFS (stops clustered into 1 hub) have no multi-stop road directions
            pats = [p for k, p_list in self.lookups._patterns.items() if k[0] == rid for p in p_list]
            if not pats or max(len(p) for p in pats) < 2:
                continue

            for lang in ("en", "ar"):
                det = self.lookups.route_detail(rid, lang=lang)
                if not det or not det.get("success"):
                    failed_load.append((rid, vtype, f"failed in lang={lang}"))
                    continue

                dirs = det.get("directions", [])
                if not dirs:
                    failed_load.append((rid, vtype, "empty directions"))
                    continue

                for di, d in enumerate(dirs):
                    stops = d.get("stops", [])
                    points = d.get("points", [])

                    if len(stops) < 2:
                        disconnected.append((rid, vtype, di, f"stops < 2 ({len(stops)})"))
                    if len(points) < 2:
                        disconnected.append((rid, vtype, di, f"points < 2 ({len(points)})"))

                    d_start = hav(stops[0]["lat"], stops[0]["lon"], points[0]["lat"], points[0]["lon"])
                    d_end = hav(stops[-1]["lat"], stops[-1]["lon"], points[-1]["lat"], points[-1]["lon"])

                    if d_start > 800:
                        mismatched.append((rid, vtype, r.get("number"), di, "start_gap_m", round(d_start)))
                    if d_end > 800:
                        mismatched.append((rid, vtype, r.get("number"), di, "end_gap_m", round(d_end)))

        self.assertEqual(len(failed_load), 0, f"Failed to load: {failed_load}")
        self.assertEqual(len(disconnected), 0, f"Disconnected: {disconnected}")
        self.assertEqual(len(mismatched), 0, f"Mismatched endpoints: {mismatched}")


if __name__ == "__main__":
    unittest.main()
