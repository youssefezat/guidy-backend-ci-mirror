"""Deterministic Greater Cairo routing-coverage sweep.

This is a service-area test, not a claim about every square metre of Egypt.
It uses named, geographically distributed urban centres and tests every
ordered pair at a fixed weekday service time.  A point is endpoint-covered
when the engine can snap it to the network; a pair is route-covered only
when the engine returns a successful itinerary with a transit leg.

Run: python coverage_sweep.py
"""
import datetime
import json

from raptor_engine import GTFSRaptorEngine


# Named urban centres spanning Cairo, Giza, the satellite cities and the
# New Administrative Capital. Coordinates are deliberately public landmarks
# or district centres rather than GTFS stops, so the test includes access.
POINTS = {
    "6th October": (29.9285, 30.9188),
    "Sheikh Zayed": (30.0131, 30.9762),
    "Smart Village": (30.0747, 31.0184),
    "Giza / Haram": (29.9870, 31.1400),
    "Faisal": (30.0175, 31.2037),
    "Dokki": (30.0385, 31.2101),
    "Mohandessin": (30.0612, 31.2045),
    "Zamalek": (30.0616, 31.2194),
    "Downtown": (30.0444, 31.2357),
    "Shubra El Kheima": (30.1225, 31.2447),
    "Rod El Farag": (30.1010, 31.2463),
    "Heliopolis": (30.0910, 31.3260),
    "Nasr City": (30.0731, 31.3467),
    "Abbas El Akkad": (30.0596, 31.3357),
    "Mokattam": (30.0224, 31.3030),
    "Maadi": (29.9603, 31.2577),
    "Helwan": (29.8488, 31.3343),
    "New Cairo": (30.0300, 31.4820),
    "Rehab": (30.0619, 31.4934),
    "Madinaty": (30.1036, 31.6372),
    # Carrefour / Downtown Shorouk is a verifiable public city-centre
    # landmark (OSM node 7255002820).  The former point at 30.2333, 31.6200
    # was ~6 km north of Shorouk's documented LRT/feeder corridor and made a
    # city-centre coverage test look like a network gap.  Source: the 2026
    # Ministry feeder-service announcement and https://mapcarta.com/N7255002820
    "El Shorouk": (30.173334, 31.592825),
    "Obour": (30.2280, 31.4690),
    "Badr": (30.1350, 31.7350),
    "New Administrative Capital": (30.0059, 31.7701),
}

# Wednesday 09:00 is within the declared service window for the feed.
TEST_TIME = datetime.datetime(2026, 9, 2, 9, 0, 0)


def has_transit(result):
    return result.get("success") and any(
        step.get("vehicle_type") not in (None, "walk")
        for option in result.get("options", [])
        for step in option.get("instructions", [])
    )


def has_complete_transit(result):
    """A partial itinerary is useful diagnostic coverage, not full coverage."""
    return result.get("success") and any(
        option.get("type") != "Partial" and any(
            step.get("vehicle_type") not in (None, "walk")
            for step in option.get("instructions", [])
        )
        for option in result.get("options", [])
    )


def main():
    engine = GTFSRaptorEngine("gtfs_data", use_osrm=False)
    engine.load_data()
    names = list(POINTS)
    endpoints = {}
    for name, (lat, lon) in POINTS.items():
        stop, distance_m = engine.find_nearest_stop(lat, lon)
        endpoints[name] = {"covered": stop is not None, "snap_distance_m": distance_m}

    pairs, failures, partials = 0, [], []
    by_origin, by_destination = {n: [0, 0] for n in names}, {n: [0, 0] for n in names}
    for origin in names:
        for destination in names:
            if origin == destination:
                continue
            pairs += 1
            result = engine.run_raptor_by_coords(*POINTS[origin], *POINTS[destination], now=TEST_TIME)
            ok = has_transit(result)
            complete = has_complete_transit(result)
            by_origin[origin][0] += int(ok)
            by_origin[origin][1] += 1
            by_destination[destination][0] += int(ok)
            by_destination[destination][1] += 1
            if not ok:
                failures.append({"origin": origin, "destination": destination,
                                 "reason": result.get("reason")})
            elif not complete:
                partials.append({"origin": origin, "destination": destination})

    endpoint_covered = sum(v["covered"] for v in endpoints.values())
    report = {
        "method": "24 named Greater Cairo urban centres; all ordered OD pairs; Wednesday 09:00; direct engine; OSRM disabled",
        "endpoint_coverage_percent": round(100 * endpoint_covered / len(POINTS), 1),
        "transit_assisted_od_percent": round(100 * (pairs - len(failures)) / pairs, 1),
        "complete_transit_od_percent": round(100 * (pairs - len(failures) - len(partials)) / pairs, 1),
        "points": len(POINTS), "ordered_pairs": pairs,
        "endpoint_coverage": endpoints,
        "origin_success_percent": {n: round(100 * a / b, 1) for n, (a, b) in by_origin.items()},
        "destination_success_percent": {n: round(100 * a / b, 1) for n, (a, b) in by_destination.items()},
        "failures": failures,
        "partial_itineraries": partials,
    }
    with open("coverage_sweep_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
