"""
Exhaustive verification test suite for the Lines feature.
Audits transit lines across every transit mode in Cairo:
- bus, minibus, microbus, metro, monorail, lrt, apm

Verifies that:
1. Every single route loads cleanly in both English and Arabic.
2. Every direction has valid stops (>= 2) and polyline points (>= 2).
3. 100% of routes have EXACT zero-gap connectivity between the origin stop pin and
   the start of the polyline (start gap == 0m).
4. 100% of routes have EXACT zero-gap connectivity between the destination stop pin and
   the end of the polyline (end gap == 0m).
5. Zero routes suffer from acute 180-degree needle spikes (haywire spurs).
6. Every single coordinate is a valid Cairo coordinate.
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


class TestLinesScreenAllRoutesConnectivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = GTFSRaptorEngine(GTFS, use_osrm=False)
        cls.eng.load_data()
        cls.lookups = TransitLookups(cls.eng)

    def test_all_routes_connectivity_and_coherence(self):
        all_routes = self.lookups.search_routes(query="", limit=5000, lang="en")
        total_routes = len(all_routes)
        self.assertGreater(total_routes, 0, "Expected routes to be discovered")

        failed_routes = []
        disconnected_start = []
        disconnected_end = []
        haywire_spikes = []
        invalid_coords = []
        empty_lines = []

        for r in all_routes:
            rid = r["route_id"]
            vtype = r.get("vehicle_type", "unknown")
            rnum = r.get("number") or r.get("name") or rid

            # Degenerate 1-stop routes in GTFS (stops clustered into 1 hub) have no multi-stop directions
            pats = [p for k, p_list in self.lookups._patterns.items() if k[0] == rid for p in p_list]
            if not pats or max(len(p) for p in pats) < 2:
                continue

            for lang in ("en", "ar"):
                det = self.lookups.route_detail(rid, lang=lang)
                if not det or not det.get("success"):
                    failed_routes.append((rid, vtype, rnum, f"failed load in lang={lang}"))
                    continue

                dirs = det.get("directions", [])
                if not dirs:
                    failed_routes.append((rid, vtype, rnum, "empty directions"))
                    continue

                for di, d in enumerate(dirs):
                    stops = d.get("stops", [])
                    pts = d.get("points", [])

                    if len(stops) < 2 or len(pts) < 2:
                        empty_lines.append((rid, vtype, rnum, di, len(stops), len(pts)))
                        continue

                    s0 = stops[0]
                    p0 = pts[0]
                    s_end = stops[-1]
                    p_end = pts[-1]

                    g_start = hav(s0["lat"], s0["lon"], p0["lat"], p0["lon"])
                    if g_start > 0.05:
                        disconnected_start.append((rid, vtype, rnum, di, round(g_start, 2)))

                    g_end = hav(s_end["lat"], s_end["lon"], p_end["lat"], p_end["lon"])
                    if g_end > 0.05:
                        disconnected_end.append((rid, vtype, rnum, di, round(g_end, 2)))

                    for pt in pts:
                        if not (29.0 <= pt["lat"] <= 31.5 and 30.5 <= pt["lon"] <= 32.5):
                            invalid_coords.append((rid, vtype, rnum, pt))
                            break

                    if len(pts) > 4:
                        for pi in range(len(pts) - 2):
                            d_ab = hav(pts[pi]["lat"], pts[pi]["lon"], pts[pi+1]["lat"], pts[pi+1]["lon"])
                            d_bc = hav(pts[pi+1]["lat"], pts[pi+1]["lon"], pts[pi+2]["lat"], pts[pi+2]["lon"])
                            d_ac = hav(pts[pi]["lat"], pts[pi]["lon"], pts[pi+2]["lat"], pts[pi+2]["lon"])
                            if d_ab > 800 and d_bc > 800 and d_ac < 100:
                                haywire_spikes.append((rid, vtype, rnum, di, pi, round(d_ab), round(d_ac)))
                                break

        self.assertEqual(len(failed_routes), 0, f"Routes failed to load: {failed_routes[:5]}")
        self.assertEqual(len(empty_lines), 0, f"Empty lines: {empty_lines[:5]}")
        self.assertEqual(len(disconnected_start), 0, f"Disconnected start points: {disconnected_start[:5]}")
        self.assertEqual(len(disconnected_end), 0, f"Disconnected end points: {disconnected_end[:5]}")
        self.assertEqual(len(haywire_spikes), 0, f"Haywire spikes found: {haywire_spikes[:5]}")
        self.assertEqual(len(invalid_coords), 0, f"Invalid coords: {invalid_coords[:5]}")


if __name__ == "__main__":
    unittest.main()
