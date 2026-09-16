"""
Tests for Lines screen browsing, mode filtering, search query handling,
and route map rendering across all transit modes.
"""

import os
import unittest

from raptor_engine import GTFSRaptorEngine
from lookups import TransitLookups

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "gtfs_data")

EGYPT_LAT = (22.0, 32.0)
EGYPT_LON = (24.0, 37.0)


class TestLinesScreenAndMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = GTFSRaptorEngine(GTFS, use_osrm=False)
        cls.eng.load_data()
        cls.lookups = TransitLookups(cls.eng)

    def test_all_modes_populated_and_render_on_map(self):
        """Every supported transit mode must have browsable routes with valid map geometry."""
        modes = ["bus", "minibus", "microbus", "metro", "monorail", "lrt"]
        for mode in modes:
            routes = self.lookups.search_routes(query="", vehicle_type=mode, limit=10, lang="en")
            self.assertGreater(len(routes), 0, f"Vehicle mode {mode} must populate routes")

            for r in routes[:5]:  # Spot check sample per mode for speed
                rid = r["route_id"]
                for lang in ("en", "ar"):
                    det = self.lookups.route_detail(rid, lang=lang)
                    self.assertIsNotNone(det, f"Route {rid} returned None in lang={lang}")
                    self.assertTrue(det.get("success"), f"Route {rid} success != True")
                    self.assertEqual(det.get("vehicle_type"), mode)

                    dirs = det.get("directions", [])
                    self.assertGreater(len(dirs), 0, f"Route {rid} must have directions")
                    for d in dirs:
                        stops = d.get("stops", [])
                        pts = d.get("points", [])
                        self.assertGreater(len(stops), 0, f"Route {rid} direction has 0 stops")
                        self.assertGreater(len(pts), 0, f"Route {rid} direction has 0 points")

                        for s in stops:
                            lat, lon = s.get("lat"), s.get("lon")
                            self.assertTrue(EGYPT_LAT[0] <= lat <= EGYPT_LAT[1] and EGYPT_LON[0] <= lon <= EGYPT_LON[1])

                        for p in pts:
                            lat, lon = p.get("lat"), p.get("lon")
                            self.assertTrue(EGYPT_LAT[0] <= lat <= EGYPT_LAT[1] and EGYPT_LON[0] <= lon <= EGYPT_LON[1])

    def test_search_query_variations(self):
        """User search variations (Arabic, English, colloquial, line numbers) must resolve."""
        user_query_checks = [
            ("310", "NsRadhTyf0r3kiWFf6VpA", "minibus"),
            ("minibus 310", "NsRadhTyf0r3kiWFf6VpA", "minibus"),
            ("mini bus 310", "NsRadhTyf0r3kiWFf6VpA", "minibus"),
            ("ميني باص 310", "NsRadhTyf0r3kiWFf6VpA", "minibus"),
            ("702", "GOV_MIN_702", "minibus"),
            ("bus 1023", "Gqw2d9u5JudgWrjkgqYyp", "bus"),
            ("اتوبيس 1023", "Gqw2d9u5JudgWrjkgqYyp", "bus"),
            ("مترو", "NAT_L1", "metro"),
            ("monorail", "RL_MONO_EN", "monorail"),
            ("مونوريل", "RL_MONO_EN", "monorail"),
            ("lrt", "RL_LRT_CAP", "lrt"),
            ("القطار الخفيف", "RL_LRT_CAP", "lrt"),
        ]

        for query_str, expected_id, expected_mode in user_query_checks:
            for lang in ("en", "ar"):
                hits = self.lookups.search_routes(query=query_str, lang=lang, limit=10)
                self.assertGreater(len(hits), 0, f"Query '{query_str}' ({lang}) returned 0 hits")
                found_ids = [h["route_id"] for h in hits]
                self.assertIn(expected_id, found_ids, f"Query '{query_str}' did not find {expected_id}")


if __name__ == "__main__":
    unittest.main()
