"""
Exhaustive geometric and integrity verification for all transit routes.
Audits routes for:
- API response in EN and AR
- Directions, stops, and polyline points
- Egypt bounding box validity
- Maximum inter-stop hop distances
- Path length vs OD distance ratios
- Spur detour detection
"""

import csv
import math
import os
import unittest

from raptor_engine import GTFSRaptorEngine
from lookups import TransitLookups

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "gtfs_data")

EGYPT_LAT = (22.0, 32.0)
EGYPT_LON = (24.0, 37.0)


def hav(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class TestAllRoutesExhaustive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = GTFSRaptorEngine(GTFS, use_osrm=False)
        cls.eng.load_data()
        cls.lookups = TransitLookups(cls.eng)

        with open(os.path.join(GTFS, "routes.txt"), encoding="utf-8") as f:
            cls.all_routes = list(csv.DictReader(f))

    def test_all_routes_integrity_and_geometry(self):
        total_routes = len(self.all_routes)
        self.assertGreater(total_routes, 0, "No routes found in routes.txt")

        failed_routes = []
        severe_spurs = []
        extreme_ratios = []
        invalid_coords = []
        empty_directions = []
        excessive_hops = []
        rail_agency_prefixes = ("NAT", "CAI_APM")

        for r_row in self.all_routes:
            rid = r_row["route_id"]
            agency = r_row.get("agency_id", "UNKNOWN")
            short_name = r_row.get("route_short_name", "")
            is_reconstructed = str(rid).startswith("GOV_")

            # 1. Test in English
            try:
                detail_en = self.lookups.route_detail(rid, lang="en")
            except Exception as e:
                failed_routes.append((rid, agency, short_name, f"Exception in lang=en: {e}"))
                continue

            if not detail_en or not detail_en.get("success"):
                # Degenerate 1-stop routes in GTFS are deliberately omitted from detail
                pats = [p for k, p_list in self.lookups._patterns.items() if k[0] == rid for p in p_list]
                if not pats or max(len(p) for p in pats) < 2:
                    continue
                failed_routes.append((rid, agency, short_name, "detail_en is None or success=False"))
                continue

            # 2. Test in Arabic
            try:
                detail_ar = self.lookups.route_detail(rid, lang="ar")
            except Exception as e:
                failed_routes.append((rid, agency, short_name, f"Exception in lang=ar: {e}"))
                continue

            if not detail_ar or not detail_ar.get("success"):
                failed_routes.append((rid, agency, short_name, "detail_ar is None or success=False"))
                continue

            # 3. Validate directions
            directions = detail_en.get("directions", [])
            if not directions:
                empty_directions.append((rid, agency, short_name, "No directions returned"))
                continue

            for d_idx, d in enumerate(directions):
                dir_id = d.get("direction_id", str(d_idx))
                stops = d.get("stops", [])
                points = d.get("points", [])

                if not stops:
                    empty_directions.append((rid, agency, short_name, f"Direction {dir_id} has 0 stops"))
                    continue

                if not points:
                    empty_directions.append((rid, agency, short_name, f"Direction {dir_id} has 0 points"))
                    continue

                # Check coordinates bounds
                for s in stops:
                    lat = s.get("lat")
                    lon = s.get("lon")
                    if lat is None or lon is None or not (EGYPT_LAT[0] <= lat <= EGYPT_LAT[1] and EGYPT_LON[0] <= lon <= EGYPT_LON[1]):
                        invalid_coords.append((rid, agency, s.get("name"), lat, lon))

                for pt in points:
                    lat = pt.get("lat")
                    lon = pt.get("lon")
                    if lat is None or lon is None or not (EGYPT_LAT[0] <= lat <= EGYPT_LAT[1] and EGYPT_LON[0] <= lon <= EGYPT_LON[1]):
                        invalid_coords.append((rid, agency, "point", lat, lon))

                # Check path distance & OD ratio
                if len(points) >= 2:
                    path_len = sum(hav(points[i]["lat"], points[i]["lon"], points[i+1]["lat"], points[i+1]["lon"]) for i in range(len(points)-1))
                    od_dist = hav(points[0]["lat"], points[0]["lon"], points[-1]["lat"], points[-1]["lon"])

                    # Check maximum hop between consecutive stops (rail has intercity hops; regional express road routes on Ring Road/Suez Rd reach up to 34.5km)
                    for i in range(len(stops) - 1):
                        hop_dist = hav(stops[i]["lat"], stops[i]["lon"], stops[i+1]["lat"], stops[i+1]["lon"])
                        if hop_dist > 36000 and agency not in rail_agency_prefixes and not rid.startswith("RL_"):
                            excessive_hops.append((rid, agency, short_name, i, stops[i]["name"], stops[i+1]["name"], round(hop_dist/1000, 1)))

                    # Check OD ratio (for routes with OD > 2.5km; shorter routes include neighborhood loop/horseshoe lines)
                    if od_dist > 2500:
                        ratio = path_len / od_dist
                        limit = 3.2 if is_reconstructed else 4.0
                        if ratio > limit:
                            extreme_ratios.append((rid, agency, short_name, round(od_dist/1000, 1), round(path_len/1000, 1), round(ratio, 1)))

                    # Check spur detours (>=3.5km out and back to within 1.2km)
                    coords = [(pt["lat"], pt["lon"]) for pt in points]
                    n_pts = len(coords)
                    if od_dist >= 3500 and n_pts >= 5:
                        step = max(1, n_pts // 80)
                        sample_idx = list(range(0, n_pts, step))
                        if sample_idx[-1] != n_pts - 1:
                            sample_idx.append(n_pts - 1)
                        spur_found = False
                        for si_a, i in enumerate(sample_idx[:-4]):
                            for k in sample_idx[si_a + 4:]:
                                if hav(coords[i][0], coords[i][1], coords[k][0], coords[k][1]) < 1200:
                                    max_dev = max(hav(coords[i][0], coords[i][1], coords[m][0], coords[m][1]) for m in range(i + 1, k, step))
                                    if max_dev >= 3500:
                                        severe_spurs.append((rid, agency, short_name, round(max_dev/1000, 1), dir_id))
                                        spur_found = True
                                        break
                            if spur_found:
                                break

        self.assertEqual(len(failed_routes), 0, f"Failed routes: {failed_routes[:5]}")
        self.assertEqual(len(empty_directions), 0, f"Empty directions: {empty_directions[:5]}")
        self.assertEqual(len(invalid_coords), 0, f"Invalid coords: {invalid_coords[:5]}")
        self.assertEqual(len(severe_spurs), 0, f"Severe spurs: {severe_spurs[:5]}")
        self.assertEqual(len(extreme_ratios), 0, f"Extreme ratios: {extreme_ratios[:5]}")
        self.assertEqual(len(excessive_hops), 0, f"Excessive hops: {excessive_hops[:5]}")


if __name__ == "__main__":
    unittest.main()
