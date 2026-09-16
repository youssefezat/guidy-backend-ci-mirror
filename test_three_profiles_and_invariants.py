"""
Test suite verifying the three base routing profiles:
  - Recommended: balanced best bang for buck
  - Fastest: absolute fastest route (minimal elapsed duration)
  - Cheapest: absolute lowest fare in EGP
and asserting that fake traffic data has been completely removed.
"""
import unittest
import datetime
from raptor_engine import GTFSRaptorEngine

class TestThreeProfilesAndInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = GTFSRaptorEngine('gtfs_data', use_osrm=False)
        cls.eng.load_data()
        cls.noon = datetime.datetime(2026, 9, 2, 12, 0, 0)  # Wednesday noon

    def test_no_traffic_field_in_options(self):
        """Options must never return any 'traffic' field."""
        pairs = [
            (30.0561, 31.3301, 30.0609, 31.2197),  # Nasr City to Zamalek
            (29.9598, 31.2612, 30.0444, 31.2357),  # Maadi to Tahrir
            (30.0444, 31.2357, 30.0450, 31.2360),  # Short walk
        ]
        for slat, slon, elat, elon in pairs:
            res = self.eng.run_raptor_by_coords(slat, slon, elat, elon, now=self.noon)
            self.assertTrue(res.get('success', False))
            for opt in res.get('options', []):
                self.assertNotIn('traffic', opt, f"Option {opt['type']} must not have 'traffic' field")

    def test_three_profiles_and_invariants(self):
        """Verifies that for transit trips, Cheapest is <= Fastest in price and Fastest is <= Cheapest in time."""
        pairs = [
            ("Tagamoa to Ramses", 30.0210, 31.4320, 30.0626, 31.2497),
            ("Nasr City to Zamalek", 30.0561, 31.3301, 30.0609, 31.2197),
            ("Giza to Heliopolis", 30.0131, 31.2089, 30.0888, 31.3285),
            ("Maadi to Tahrir", 29.9598, 31.2612, 30.0444, 31.2357),
            ("Shubra to Dokki", 30.1250, 31.2450, 30.0385, 31.2114),
        ]

        for name, slat, slon, elat, elon in pairs:
            with self.subTest(name=name):
                res = self.eng.run_raptor_by_coords(slat, slon, elat, elon, now=self.noon)
                self.assertTrue(res.get('success', False), f"Failed to find route for {name}")
                options = res.get('options', [])
                self.assertGreater(len(options), 0)

                by_type = {o['type']: o for o in options}
                
                # Check tier ordering
                types_in_order = [o['type'] for o in options]
                expected_priority = {"Recommended": 0, "Fastest": 1, "Cheapest": 2, "Alternative": 3, "Walk": 4, "Partial": 5}
                ranks = [expected_priority.get(t, 9) for t in types_in_order]
                self.assertEqual(ranks, sorted(ranks), f"Options not sorted according to tier_order: {types_in_order}")

                if "Cheapest" in by_type and "Fastest" in by_type:
                    cheap = by_type["Cheapest"]
                    fast = by_type["Fastest"]
                    self.assertLessEqual(
                        cheap['fare_egp'], fast['fare_egp'],
                        f"[{name}] Cheapest fare ({cheap['fare_egp']} EGP) must be <= Fastest fare ({fast['fare_egp']} EGP)"
                    )
                    self.assertLessEqual(
                        int(fast['time']), int(cheap['time']),
                        f"[{name}] Fastest duration ({fast['time']} min) must be <= Cheapest duration ({cheap['time']} min)"
                    )

                if "Recommended" in by_type and "Cheapest" in by_type:
                    rec = by_type["Recommended"]
                    cheap = by_type["Cheapest"]
                    self.assertLessEqual(
                        cheap['fare_egp'], rec['fare_egp'],
                        f"[{name}] Cheapest fare ({cheap['fare_egp']} EGP) must be <= Recommended fare ({rec['fare_egp']} EGP)"
                    )

if __name__ == '__main__':
    unittest.main()
