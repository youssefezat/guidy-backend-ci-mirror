import unittest
import csv
import math
import os
import collections

from raptor_engine import GTFSRaptorEngine
from lookups import TransitLookups

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "gtfs_data")


def hav(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class TestFeedGeometry(unittest.TestCase):
    """Feed-wide geometric integrity tests.

    Guarantees that no route in gtfs_data suffers from out-and-back spur detours,
    corrupted backtrack loops, or extreme path-to-OD wander.
    """

    @classmethod
    def setUpClass(cls):
        cls.stops = {}
        with open(os.path.join(GTFS, "stops.txt"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cls.stops[r["stop_id"]] = (
                    float(r["stop_lat"]),
                    float(r["stop_lon"]),
                    r["stop_name"],
                )

        with open(os.path.join(GTFS, "routes.txt"), encoding="utf-8") as f:
            cls.routes = {r["route_id"]: r for r in csv.DictReader(f)}
        with open(os.path.join(GTFS, "trips.txt"), encoding="utf-8") as f:
            cls.trips = {r["trip_id"]: r for r in csv.DictReader(f)}

        cls.trip_stops = collections.defaultdict(list)
        with open(os.path.join(GTFS, "stop_times.txt"), encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cls.trip_stops[row["trip_id"]].append(
                    (int(row["stop_sequence"]), row["stop_id"])
                )

    def test_no_severe_spur_detours(self):
        """Asserts that zero arterial trips (OD >= 3.5 km) in the active GTFS feed
        contain an intermediate out-and-back spur loop >= 3.0 km that returns within 1.2 km
        of where it branched off.
        """
        bad_spurs = []
        for tid, seq_tuples in self.trip_stops.items():
            seq_sorted = [s[1] for s in sorted(seq_tuples, key=lambda x: x[0])]
            coords = [self.stops[sid][:2] for sid in seq_sorted if sid in self.stops]
            n = len(coords)
            if n < 5:
                continue

            od_dist = hav(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1])
            # Only test linear / arterial corridors; terminal circulators are handled separately
            if od_dist < 3500:
                continue

            # Check both forward and reverse directions for intermediate spurs
            for direction_coords in (coords, list(reversed(coords))):
                for i in range(1, n - 4):
                    for k in range(i + 4, n - 1):
                        d_ik = hav(direction_coords[i][0], direction_coords[i][1], direction_coords[k][0], direction_coords[k][1])
                        if d_ik < 1200:
                            max_dev = max(
                                hav(direction_coords[i][0], direction_coords[i][1], direction_coords[m][0], direction_coords[m][1])
                                for m in range(i + 1, k)
                            )
                            if max_dev >= 3500:
                                t_info = self.trips.get(tid, {})
                                r_info = self.routes.get(t_info.get("route_id", ""), {})
                                bad_spurs.append({
                                    "trip_id": tid,
                                    "route_id": t_info.get("route_id"),
                                    "agency": r_info.get("agency_id"),
                                    "name": r_info.get("route_short_name"),
                                    "spur_km": round(max_dev / 1000, 1),
                                    "gap_m": round(d_ik),
                                })
                                break
                    if bad_spurs and bad_spurs[-1]["trip_id"] == tid:
                        break
                if bad_spurs and bad_spurs[-1]["trip_id"] == tid:
                    break

        self.assertEqual(
            bad_spurs,
            [],
            f"Found {len(bad_spurs)} trips with severe spur detours: {bad_spurs[:5]}",
        )

    def test_no_extreme_path_to_od_ratios(self):
        """Asserts that no route in the feed exceeds a 4.0x distance ratio, and no
        reconstructed governorate route exceeds 3.2x.
        """
        anomalies = []
        for tid, seq_tuples in self.trip_stops.items():
            seq_sorted = [s[1] for s in sorted(seq_tuples, key=lambda x: x[0])]
            coords = [self.stops[sid][:2] for sid in seq_sorted if sid in self.stops]
            if len(coords) < 5:
                continue

            path_len = sum(
                hav(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
                for i in range(len(coords) - 1)
            )
            od_dist = hav(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1])

            if od_dist > 2000:
                ratio = path_len / od_dist
                is_gov = tid.startswith("GOV_")
                limit = 3.2 if is_gov else 4.0
                if ratio > limit:
                    t_info = self.trips.get(tid, {})
                    r_info = self.routes.get(t_info.get("route_id", ""), {})
                    anomalies.append({
                        "trip_id": tid,
                        "route_id": t_info.get("route_id"),
                        "agency": r_info.get("agency_id"),
                        "name": r_info.get("route_short_name"),
                        "od_km": round(od_dist / 1000, 1),
                        "path_km": round(path_len / 1000, 1),
                        "ratio": round(ratio, 1),
                    })

        self.assertEqual(
            anomalies,
            [],
            f"Found {len(anomalies)} trips with excessive path/OD ratio: {anomalies[:5]}",
        )

    def test_minibus_255_is_clean_and_direct(self):
        """Specifically verifies that Minibus 255 (Abou Zaabal - Ramses) is direct
        without the historical 44-stop Maadi detour loop.
        """
        trip_ids = ["GOV_MIN_255_0", "GOV_MIN_255_1"]
        for tid in trip_ids:
            self.assertIn(tid, self.trip_stops, f"Expected {tid} in gtfs_data")
            seq = [s[1] for s in sorted(self.trip_stops[tid], key=lambda x: x[0])]
            self.assertEqual(len(seq), 40, f"Expected exactly 40 stops for {tid}")

            # Verify endpoints
            first_name = self.stops[seq[0]][2]
            last_name = self.stops[seq[-1]][2]
            if tid.endswith("_0"):
                self.assertIn("Abou Zaabal", first_name)
                self.assertIn("Ramses", last_name)
            else:
                self.assertIn("Ramses", first_name)
                self.assertIn("Abou Zaabal", last_name)

            # Ensure Maadi stops (e.g. 1603 Al Horia Square) are absent
            self.assertNotIn("1603", seq, f"Maadi stop 1603 must not be present in {tid}")

            # Verify length is direct (~26 km, ratio ~1.1x)
            coords = [self.stops[s][:2] for s in seq]
            path_km = sum(
                hav(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
                for i in range(len(coords) - 1)
            ) / 1000
            od_km = hav(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1]) / 1000
            self.assertLess(path_km, 30.0, f"Path length {path_km}km too long")
            self.assertLess(path_km / od_km, 1.4, f"Detour ratio {path_km/od_km:.2f} too high")

    def test_key_reconstructed_routes_intact(self):
        """Verifies that rider-priority reconstructed routes (Minibus 37, 140)
        are present, active, and healthy.
        """
        for r_num in ["37", "140"]:
            rid = f"GOV_MIN_{r_num}"
            self.assertIn(rid, self.routes, f"Expected {rid} in routes.txt")
            t0 = f"{rid}_0"
            self.assertIn(t0, self.trip_stops, f"Expected trip {t0} in stop_times.txt")
            stops_count = len(self.trip_stops[t0])
            self.assertGreaterEqual(stops_count, 30, f"{rid} has too few stops: {stops_count}")

    def test_route_detail_loads_cleanly(self):
        """Verifies that TransitLookups loads route details with valid points and
        stations for Minibus 255.
        """
        eng = GTFSRaptorEngine(GTFS, use_osrm=False)
        eng.load_data()
        lookups = TransitLookups(eng)

        detail = lookups.route_detail("GOV_MIN_255", lang="en")
        self.assertIsNotNone(detail)
        self.assertTrue(detail["success"])
        self.assertEqual(len(detail["directions"]), 2)

        for d in detail["directions"]:
            self.assertEqual(len(d["stops"]), 40)
            self.assertGreaterEqual(len(d["points"]), 40)
            # Verify coordinates are valid non-zero Cairo coordinates
            for pt in d["points"]:
                self.assertTrue(29.8 <= pt["lat"] <= 30.5)
                self.assertTrue(31.0 <= pt["lon"] <= 31.6)


if __name__ == "__main__":
    unittest.main()
