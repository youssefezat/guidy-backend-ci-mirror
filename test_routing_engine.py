"""
Automated test suite for the Guidy routing engine.

Run with:
    cd guidy-backend-main
    python3 -m unittest test_routing_engine.py -v

Uses only the Python standard library's `unittest` -- no pytest or other
extra install required, since this needs to run in any environment
(including CI, if one gets set up) without extra setup steps.

Loads the engine ONCE for the whole suite (setUpClass), not once per test
-- loading takes a few seconds, and there's no reason to pay that cost
repeatedly when nothing in these tests mutates engine state.

These tests exercise the same failure modes found and fixed during
development (see git history / commit messages for raptor_engine.py):
snap-distance-capping, identical-point routing, official fare tiers,
day-of-week service filtering. They're meant to catch a *regression* --
if one of these starts failing after a future change, that's a real
signal, not a flaky test.
"""
import unittest
import datetime
from raptor_engine import GTFSRaptorEngine


class GuidyEngineTestCase(unittest.TestCase):
    # A fixed instant, so the suite means the same thing whenever it runs.
    #
    # The engine filters routes against their declared service window, so a
    # 04:00 query correctly returns "nothing is running at this hour" and no
    # options at all. That is right in production and fatal in a test suite:
    # run overnight, or in CI on any schedule, eight routing tests failed
    # for a reason that had nothing to do with the code under test.
    # Wednesday 09:00 is inside service hours for every route in this feed.
    FIXED_NOW = datetime.datetime(2026, 9, 2, 9, 0, 0)

    @classmethod
    def setUpClass(cls):
        cls.eng = GTFSRaptorEngine('gtfs_data', use_osrm=False)
        cls.eng.load_data()

    def route(self, slat, slon, elat, elon, lang='en', now=None):
        return self.eng.run_raptor_by_coords(
            slat, slon, elat, elon, lang=lang, now=now or self.FIXED_NOW)


class TestAdversarialInput(GuidyEngineTestCase):
    """Edge cases that previously produced nonsensical results -- these
    should now degrade gracefully instead of returning absurd numbers."""

    def test_identical_start_and_end(self):
        r = self.route(30.0444, 31.2357, 30.0444, 31.2357)
        for opt in r.get('options', []):
            self.assertLess(opt['distance_m'], 200,
                             f"identical points should not produce a padded 'there and back' route: {opt}")

    def test_points_30m_apart(self):
        r = self.route(30.0444, 31.2357, 30.0447, 31.2357)
        for opt in r.get('options', []):
            self.assertLess(opt['distance_m'], 200,
                             f"trivially close points should not route through transit: {opt}")

    def test_out_of_coverage_area_does_not_explode(self):
        """A point genuinely outside GTFS coverage (Western Desert) must
        never produce an absurd multi-hundred-km 'transit' answer -- this
        was the actual root-cause bug behind an earlier user report of a
        '10,000m walk' on a completely unrelated real Cairo trip."""
        r = self.route(27.0, 28.0, 27.05, 28.05)
        for opt in r.get('options', []):
            self.assertLess(opt['distance_m'], 20000,
                             f"out-of-coverage routing produced an absurd distance: {opt}")

    def test_cairo_to_middle_of_nowhere_does_not_explode(self):
        r = self.route(30.0444, 31.2357, 27.0, 28.0)
        for opt in r.get('options', []):
            self.assertLess(opt['distance_m'], 600000,
                             f"should reject or cap rather than snap to an absurdly distant stop: {opt}")

    def test_garbage_language_code_falls_back_gracefully(self):
        r = self.route(30.0444, 31.2357, 30.0524, 31.2468, lang='xx')
        self.assertTrue(r.get('options'), "should still return options even with an invalid lang code")


class TestMetroNetwork(GuidyEngineTestCase):
    """Metro-specific regression tests -- station data, line connectivity,
    interchanges, and fare tiers."""

    def test_all_84_metro_stations_loaded(self):
        metro_stops = [s for s in self.eng.stops if s.startswith('NAT_')]
        self.assertEqual(len(metro_stops), 84)

    def test_three_metro_routes_loaded(self):
        self.assertEqual(len(self.eng.metro_route_ids), 3)

    def test_full_line1_trip_is_direct_metro(self):
        """Helwan to New El-Marg is the full length of Line 1 -- should
        be a single uninterrupted metro ride, not require any transfer."""
        a, b = self.eng.stops['NAT_HELWAN'], self.eng.stops['NAT_NEW_EL_MARG']
        r = self.route(a['lat'], a['lon'], b['lat'], b['lon'])
        opt = r['options'][0]
        vehicle_types = {i.get('vehicle_type') for i in opt['instructions'] if i.get('vehicle_type') not in (None, 'walk')}
        self.assertEqual(vehicle_types, {'metro'})

    def test_line1_to_line3_interchange_at_nasser(self):
        """Ain Shams (Line 1) to Cairo University (Line 3) forces a
        transfer at Nasser -- confirms cross-line interchange routing
        works, not just single-line trips."""
        a, b = self.eng.stops['NAT_AIN_SHAMS'], self.eng.stops['NAT_CAIRO_UNIVERSITY']
        r = self.route(a['lat'], a['lon'], b['lat'], b['lon'])
        self.assertTrue(r['options'], "should find a route across the L1/L3 interchange")

    def test_shared_interchange_stations_use_one_stop_id(self):
        """Sadat, Nasser, Al-Shohadaa, Attaba, and Cairo University are
        each served by two lines but are the same physical station --
        confirms they weren't accidentally duplicated as separate stops
        during data entry, which would silently break free transfers."""
        for name in ['Sadat', 'Nasser', 'Al-Shohadaa', 'Attaba', 'Cairo University']:
            matching_ids = [sid for sid, data in self.eng.stops.items()
                             if data['name'] == name and sid.startswith('NAT_')]
            self.assertEqual(len(matching_ids), 1, f"{name} should be exactly one stop_id, found {matching_ids}")


class TestFares(GuidyEngineTestCase):
    """Fare tiers, verified against the official March 2026 Ministry of
    Transport pricing announcement. If these start failing, check whether
    real fares changed again before assuming the code is wrong."""

    def test_metro_tier_boundaries(self):
        # boundaries are 8 / 15 / 22 stops (not 9/16/23 -- see code comment
        # in raptor_engine.py for how these were derived from the real
        # TfC fare_rules.txt matrix). Use real station pairs at a known
        # stop-count along Line 1's fixed station order.
        line1_order = [
            'NAT_HELWAN','NAT_AIN_HELWAN','NAT_HELWAN_UNIVERSIT','NAT_WADI_HOF','NAT_HADAYEK_HELWAN',
            'NAT_EL_MAASARA','NAT_TORA_EL_ASMANT','NAT_KOZZIKA','NAT_TORA_EL_BALAD',
        ]
        # Helwan -> Tora El-Balad is 8 hops -> should land in the cheapest tier (10 EGP)
        a, b = self.eng.stops[line1_order[0]], self.eng.stops[line1_order[-1]]
        r = self.route(a['lat'], a['lon'], b['lat'], b['lon'])
        opt = r['options'][0]
        self.assertEqual(opt['fare_total_egp'], 10, f"8-stop metro trip should be 10 EGP, got {opt}")

    def test_full_line1_is_top_fare_tier(self):
        a, b = self.eng.stops['NAT_HELWAN'], self.eng.stops['NAT_NEW_EL_MARG']
        r = self.route(a['lat'], a['lon'], b['lat'], b['lon'])
        self.assertEqual(r['options'][0]['fare_total_egp'], 20)

    def test_surface_fares_are_not_stale(self):
        """Regression guard for a bug that has now recurred twice: the
        minibus fare sat at 7 EGP long after real prices moved to 14, was
        corrected, then sat at 14 straight through the March 2026
        revision that took it to 19. Bus likewise sat at 10 against a
        real 13.

        Current values verified 2026-08-22 against two independent
        Egyptian outlets reporting the same March 2026 Cairo Governorate
        adjustment (elwatannews.com/news/details/8242721 and
        elbalad.news/6897307), which agree on both pre- and post-increase
        figures. Non-A/C variants, since this feed has no A/C flag.

        Asserts the constants directly rather than routing, so it fails
        loudly on a revert instead of depending on some route happening
        to include a minibus leg. If this fails, check whether real fares
        moved AGAIN before assuming the code is wrong.

        NOTE these are RIDER-REPORTED, not the published tariff. The
        March 2026 Cairo Governorate announcement said 13 EGP bus / 19
        EGP minibus; the project owner, who rides this network, reports
        the cheapest real bus ticket as 20 EGP. Do not "correct" these
        back toward a news figure without asking someone who rides it."""
        from raptor_engine import FLAT_FARE_BY_VEHICLE
        self.assertEqual(FLAT_FARE_BY_VEHICLE['bus'], 20)
        self.assertEqual(FLAT_FARE_BY_VEHICLE['minibus'], 20)

    def test_no_surface_fare_undercuts_the_cheapest_metro_ticket(self):
        """The cheapest metro ticket is 10 EGP (confirmed by the project
        owner, and matching METRO_FARE_TIERS' first tier). No surface
        vehicle should ever be priced below that -- a microbus floor of
        5 EGP made the router believe a microbus always beat any metro
        ride on cost, which biased the "cheapest" profile toward
        microbus legs on trips where the metro is genuinely cheaper."""
        from raptor_engine import (
            FLAT_FARE_BY_VEHICLE, MICROBUS_MIN_FARE, METRO_FARE_TIERS,
        )
        cheapest_metro = METRO_FARE_TIERS[0][1]
        self.assertEqual(cheapest_metro, 10)
        self.assertGreaterEqual(MICROBUS_MIN_FARE, cheapest_metro)
        for vtype in ('bus', 'minibus'):
            self.assertGreaterEqual(FLAT_FARE_BY_VEHICLE[vtype], cheapest_metro)

    def test_step_fares_sum_to_displayed_total(self):
        """Regression guard for a real user-visible bug: _build_option_data
        used to add 5 EGP to every "Fastest" option and subtract 5 from
        every "Cheapest" one, purely from the tier LABEL. A confirmed real
        case showed a 29 EGP Fastest card whose own steps summed to 24.
        The metro tier fare was also folded into the total without being
        attached to any step, so metro legs displayed "0 EGP" while
        silently contributing 10-20 EGP.

        Every fare a rider can see must reconcile: per-step fares must add
        up to the number on the card, for every tier."""
        r = self.route(30.0663104, 31.3806677, 30.0717052, 31.220952)
        transit = [o for o in r.get('options', []) if o['type'] != 'Walk']
        self.assertTrue(transit, "expected at least one transit option")
        for opt in transit:
            steps_sum = sum(i.get('fare_egp') or 0 for i in opt['instructions'])
            self.assertEqual(
                steps_sum, opt['fare_total_egp'],
                f"{opt['type']}: steps sum to {steps_sum} but card shows "
                f"{opt['fare_total_egp']} -- displayed fares must reconcile"
            )

    def test_cheapest_tier_is_actually_cheapest(self):
        """Regression guard for a real bug: the "cheapest" profile only
        penalized the metro and never looked at fare at all, so on the
        confirmed Nasr City -> Zamalek case it returned a 32 EGP
        two-vehicle route while "Regular" returned a 13 EGP single-bus
        one. A tier labelled Cheapest costing 2.5x the middle tier is
        indefensible to a rider, so assert the invariant directly.

        Uses Nasr City -> Faisal rather than the Zamalek trip, because on
        the Zamalek trip both tiers converge on the same route and get
        collapsed to one card (see test_identical_tiers_collapse) -- so it
        would no longer exercise the comparison at all."""
        r = self.route(30.0731, 31.3467, 30.0175, 31.2037)
        by_tier = {o['type']: o for o in r.get('options', []) if o['type'] != 'Walk'}
        cheapest = by_tier.get('Cheapest')
        if cheapest is None:
            self.skipTest("no Cheapest option produced for this pair")
        for other_tier in ('Fastest', 'Regular'):
            other = by_tier.get(other_tier)
            if other is None:
                continue
            self.assertLessEqual(
                cheapest['fare_total_egp'], other['fare_total_egp'],
                f"Cheapest ({cheapest['fare_total_egp']} EGP) must not cost more "
                f"than {other_tier} ({other['fare_total_egp']} EGP)"
            )


class TestWaterCrossing(GuidyEngineTestCase):
    """The engine must never route a rider across the Nile where there is
    no bridge. Reported twice: the original "walk over the Nile" case that
    started the 2026-08-21 work, and again on 2026-08-22 when a rider was
    told to walk 538m from Imbaba Police Station to a Zamalek destination.
    The real walk is 5.2km (Google Maps). OSRM genuinely returned 538m --
    the OSM extract has a way across the water there (almost certainly the
    Imbaba railway bridge) that the foot profile will use but no
    pedestrian can.

    No distance heuristic can catch this: 538m against a 376m straight
    line is an ordinary 1.43x ratio. Hence the island polygon."""

    ZAMALEK_DEST = (30.0717052, 31.220952)      # Faculty of Commerce, Zamalek
    IMBABA_POLICE = (30.074943, 31.222087)      # opposite bank, no bridge between

    def test_island_polygon_classifies_known_points(self):
        """Six points whose district Google's geocoder confirms."""
        cases = [
            (30.0717052, 31.220952, True,  'Faculty of Commerce -> Zamalek'),
            (30.074943,  31.222087, False, 'Imbaba Police Station -> Imbaba, Giza'),
            (30.06212,   31.21715,  True,  'Al Sawy Culture Wheel -> Zamalek'),
            (30.05912,   31.21524,  False, 'Al Baloun Theatre -> Agouza, Giza'),
            (30.06221,   31.22735,  False, 'Al Ahli Bank -> Bulaq, Cairo'),
            (30.06228,   31.22329,  True,  'Safaa Hegazy -> Gezira Island'),
        ]
        for lat, lon, expected, label in cases:
            self.assertEqual(
                self.eng._point_in_island(lat, lon), expected,
                f"{label}: expected inside={expected}"
            )

    def test_bridges_derived_from_route_shapes(self):
        """Crossings come from shapes.txt, not a hand-written list -- which
        is what guarantees a crossing no transit vehicle uses (the Imbaba
        railway bridge) can never be treated as walkable."""
        self.assertTrue(
            self.eng.island_bridges,
            "no island bridges derived -- shapes.txt missing or polygon wrong"
        )

    def test_no_derived_bridge_near_the_imbaba_railway_crossing(self):
        """The specific crossing OSRM tried to use must NOT appear as a
        legal bridge."""
        for blat, blon, _ in self.eng.island_bridges:
            d = self.eng._haversine(blat, blon, *self.IMBABA_POLICE)
            self.assertGreater(
                d, self.eng.ISLAND_BRIDGE_TOLERANCE_M,
                f"a derived bridge sits {d:.0f}m from the Imbaba railway crossing"
            )

    def test_the_reported_impossible_walk_is_rejected(self):
        self.assertTrue(
            self.eng._crosses_water_illegally(*self.IMBABA_POLICE, *self.ZAMALEK_DEST),
            "Imbaba Police Station -> Zamalek must be flagged as crossing water"
        )

    def test_a_walk_within_the_island_is_allowed(self):
        self.assertFalse(
            self.eng._crosses_water_illegally(30.062279, 31.223286, *self.ZAMALEK_DEST),
            "Safaa Hegazy -> Zamalek destination is entirely on the island"
        )

    def test_destination_candidates_stay_on_the_right_bank(self):
        """The end-to-end guarantee: every stop offered for a Zamalek
        destination must actually be reachable on foot from it."""
        cands = self.eng._find_nearest_stop_candidates(*self.ZAMALEK_DEST, k=4)
        self.assertTrue(cands, "expected at least one destination candidate")
        for sid, _dist in cands:
            s = self.eng.stops[sid]
            self.assertTrue(
                self.eng._point_in_island(s['lat'], s['lon']),
                f"{sid} ({s['name']}) is across water from a Zamalek destination"
            )

    def test_full_route_to_zamalek_lands_on_the_island(self):
        r = self.route(30.0663104, 31.3806677, *self.ZAMALEK_DEST)
        transit = [o for o in r.get('options', []) if o['type'] != 'Walk']
        self.assertTrue(transit, "expected a transit option")
        for opt in transit:
            arrive = [i for i in opt['instructions'] if i['action'] == 'arrive']
            self.assertTrue(arrive, f"{opt['type']} has no arrive step")
            self.assertTrue(
                self.eng._point_in_island(arrive[-1]['lat'], arrive[-1]['lon']),
                f"{opt['type']} drops the rider at {arrive[-1]['station']}, "
                f"across water from the destination"
            )


class TestOptionDeduplication(GuidyEngineTestCase):
    """Every card shown to a rider must be a distinct real route.

    The engine used to pad a missing tier with a literal copy of another
    option and then nudge its time and price apart so they didn't look
    identical -- fabricated numbers presented as fact, the same class of
    bug as the +/-5 EGP tier fudge. Now exact duplicates collapse and the
    app simply shows fewer cards when fewer real routes exist."""

    def test_identical_tiers_collapse(self):
        """Maadi -> Zamalek: the metro is honestly both the fastest and
        the cheapest way to go, so it should be ONE card, not two."""
        r = self.route(29.9603, 31.2577, 30.0616, 31.2194)
        transit = [o for o in r.get('options', []) if o['type'] != 'Walk']
        sigs = {
            tuple((i['action'], i.get('route_id'), i.get('station'))
                  for i in o['instructions'])
            for o in transit
        }
        self.assertEqual(
            len(sigs), len(transit),
            "two option cards describe the identical route"
        )

    def test_distinct_tiers_are_both_kept(self):
        """Nasr City -> Faisal has a genuine speed/cost trade-off, so both
        cards must survive the dedup."""
        r = self.route(30.0731, 31.3467, 30.0175, 31.2037)
        types = {o['type'] for o in r.get('options', []) if o['type'] != 'Walk'}
        self.assertTrue({'Fastest', 'Recommended'} & types)
        self.assertIn('Cheapest', types)

    def test_no_option_is_a_fabricated_copy(self):
        """Whatever cards come back, no two transit options may share an
        identical route yet advertise different numbers -- that is exactly
        what the old padding step produced."""
        for coords in [(30.0663104, 31.3806677, 30.0717052, 31.220952),
                       (30.0731, 31.3467, 30.0175, 31.2037),
                       (29.8488, 31.3343, 30.1225, 31.2447)]:
            r = self.route(*coords)
            transit = [o for o in r.get('options', []) if o['type'] != 'Walk']
            by_sig = {}
            for o in transit:
                sig = tuple((i['action'], i.get('route_id'), i.get('station'))
                            for i in o['instructions'])
                if sig in by_sig:
                    self.fail(
                        f"{coords}: '{o['type']}' duplicates '{by_sig[sig]['type']}' "
                        f"({o['price']}/{o['time']}min vs "
                        f"{by_sig[sig]['price']}/{by_sig[sig]['time']}min)"
                    )
                by_sig[sig] = o


class TestServiceHours(GuidyEngineTestCase):
    """Routes must only be offered inside their declared frequencies.txt
    hours. Before 2026-08-22 the engine read that file purely to average
    headways and threw the service WINDOW away, so a 02:00 query got the
    same confident ~2-hour itinerary as a midday one -- for buses whose
    first vehicle was four hours off. Surfaced by the route-comparison
    audit: run at 21:35 Cairo, Google priced the same trips at 7-12 hours
    (it counts the overnight wait) while Guidy's numbers didn't move."""

    def test_feed_declares_service_windows(self):
        self.assertTrue(
            self.eng.route_service_windows,
            "no service windows parsed -- frequencies.txt missing or unparsed"
        )

    def test_nothing_runs_in_the_dead_of_night(self):
        """
        Every route whose declared window excludes 02:00 must be filtered
        out at 02:00 -- and, just as importantly, a route that really does
        run through the night must NOT be.

        This used to assert `len(closed) == len(windows)`: every route with
        declared hours is closed at 02:00. That held only because the feed
        happened to contain nothing but daytime services, so it was testing
        a property of the DATA rather than of the filter. Adding the Cairo
        Airport people mover, which is assumed to run round the clock,
        broke it -- correctly: the engine was right and the test was
        over-claiming.

        Checking each route against its own declared window tests the thing
        that actually matters, and keeps working whatever the feed holds.
        """
        at = 2 * 3600
        grace = self.eng.SERVICE_WINDOW_GRACE_SEC
        closed = self.eng._routes_not_running_at(at)

        overnight = []
        for route_id, windows in self.eng.route_service_windows.items():
            covers = any(
                start - grace <= t <= end
                for start, end in windows
                for t in (at, at + 86400)
            )
            if covers:
                overnight.append(route_id)
                self.assertNotIn(
                    route_id, closed,
                    f"{route_id} declares hours covering 02:00 but was filtered out"
                )
            else:
                self.assertIn(
                    route_id, closed,
                    f"{route_id} does not run at 02:00 but was still offered"
                )

        # The point of the night filter is that it removes almost
        # everything. Without this, the per-route assertions above would
        # still pass in a world where the filter had stopped doing its job.
        self.assertLess(
            len(overnight), len(self.eng.route_service_windows) * 0.05,
            "almost nothing in this network should be running at 02:00"
        )

    def test_daytime_has_service(self):
        closed = self.eng._routes_not_running_at(12 * 3600)
        self.assertLess(
            len(closed), len(self.eng.route_service_windows) * 0.1,
            "the overwhelming majority of routes should be running at midday"
        )

    def test_grace_period_before_first_service(self):
        """A rider searching at 05:30 for an 06:00 first bus is making a
        reasonable request; refusing it is worse than being slightly
        early. See SERVICE_WINDOW_GRACE_SEC."""
        closed = self.eng._routes_not_running_at(5 * 3600 + 30 * 60)
        self.assertLess(
            len(closed), len(self.eng.route_service_windows) * 0.1,
            "routes opening at 06:00 should be offered at 05:30 via the grace window"
        )

    def test_night_query_finds_no_route(self):
        start, _ = self.eng.find_nearest_stop(30.0663104, 31.3806677)
        cands = self.eng._find_nearest_stop_candidates(30.0717052, 31.220952, k=4)
        exits = {sid: max(d / self.eng.WALK_SPEED_MPS, 10) for sid, d in cands}
        path, _ = self.eng._find_shortest_path(
            start, exits, profile='fastest', today_day='saturday', now_sec=2 * 3600
        )
        self.assertIsNone(path, "a 02:00 departure should find no running service")

    def test_same_query_works_during_the_day(self):
        start, _ = self.eng.find_nearest_stop(30.0663104, 31.3806677)
        cands = self.eng._find_nearest_stop_candidates(30.0717052, 31.220952, k=4)
        exits = {sid: max(d / self.eng.WALK_SPEED_MPS, 10) for sid, d in cands}
        path, _ = self.eng._find_shortest_path(
            start, exits, profile='fastest', today_day='saturday', now_sec=9 * 3600
        )
        self.assertTrue(path, "the same trip must still route at 09:00")

    def test_routes_without_declared_hours_are_treated_as_running(self):
        """~14 MM_ routes carry no frequencies.txt entry. Defaulting those
        to 'closed' would silently delete part of the network, so they are
        always offered -- same degrade-toward-useful philosophy as the
        OSRM haversine fallback."""
        no_hours = [r for r in self.eng.routes if r not in self.eng.route_service_windows]
        if not no_hours:
            self.skipTest("every route in this feed declares hours")
        closed_at_night = self.eng._routes_not_running_at(3 * 3600)
        for route_id in no_hours:
            self.assertNotIn(route_id, closed_at_night)


class TestWalkOnlyPlausibility(GuidyEngineTestCase):
    """Guards on the walk-only option.

    Origin case (2026-08-23): a rider asked for Koshary Tawagen Salsa ->
    AASTMT Sheraton, picked the walking option, and was told to walk
    9285 m. Straight line is 3367 m and Google walks it in 4.1 km; the
    number came from our own OSRM, whose foot graph detours ~1.8 km north
    past the destination and back because no pedestrian crossing of
    Tareeq El-Nasr is mapped nearby. Two separate defects met there: the
    option was offered at all (the detour_ratio branch sets
    should_offer_walk with no distance bound), and once offered its
    distance was taken on faith.
    """

    def test_rejects_the_aastmt_case(self):
        """The exact numbers OSRM returned for the reported trip."""
        self.assertFalse(
            self.eng._walk_only_is_plausible(9285.4, 3367.0),
            "A 9.3 km walk for a 3.4 km straight line (2.76x) must not be recommended",
        )

    def test_accepts_googles_answer_for_the_same_trip(self):
        """Google's real 4.1 km / 1.22x path is a perfectly good walk --
        the guard must reject the bad number, not the trip."""
        self.assertTrue(self.eng._walk_only_is_plausible(4100.0, 3367.0))

    def test_absolute_ceiling_applies_even_to_a_straight_walk(self):
        """Circuity 1.0 still isn't a walk anyone wants offered to them."""
        self.assertFalse(self.eng._walk_only_is_plausible(9000.0, 9000.0))
        self.assertTrue(self.eng._walk_only_is_plausible(4400.0, 4300.0))

    def test_short_trips_get_slack(self):
        """A 120 m straight line needing a 300 m walk around one block is
        2.5x but entirely normal -- the ratio is noise at that scale."""
        self.assertTrue(self.eng._walk_only_is_plausible(300.0, 120.0))
        self.assertTrue(self.eng._walk_only_is_plausible(480.0, 100.0))
        # ...but the slack is bounded; this is a real detour, not a block.
        self.assertFalse(self.eng._walk_only_is_plausible(900.0, 150.0))

    def test_no_offered_walk_exceeds_the_ceiling(self):
        """End-to-end: across a spread of real trips, no option we hand
        back is a walk longer than we'd be willing to defend."""
        pairs = [
            (30.0663125, 31.3806875, 30.0959491, 31.3734994),  # the reported trip
            (30.0663104, 31.3806677, 30.0717052, 31.220952),   # Nasr City -> Zamalek
            (29.9603, 31.2577, 30.0616, 31.2194),              # Maadi -> Zamalek
            (30.0444, 31.2357, 30.0524, 31.2468),              # downtown short hop
            (29.9285, 30.9188, 30.0444, 31.2357),              # 6th October -> downtown
        ]
        for slat, slon, elat, elon in pairs:
            with self.subTest(origin=(slat, slon), destination=(elat, elon)):
                for opt in self.route(slat, slon, elat, elon).get('options', []):
                    if opt.get('type') == 'Walk':
                        self.assertLessEqual(
                            opt['distance_m'], self.eng.WALK_ONLY_MAX_REAL_M,
                            f"offered a {opt['distance_m']} m walk",
                        )


class TestWaitingTime(GuidyEngineTestCase):
    """Expected waiting time, added 2026-08-23.

    frequencies.txt headways were loaded and surfaced to the app as
    `typical_headway_min` but never entered the routing cost or the quoted
    time. Guidy said 41 min for Koshary Tawagen Salsa -> AASTMT; Google's
    five options ran 69-91 min and its own itinerary left at 2:04 to board
    at 2:24. We were quoting pure riding time.
    """

    def _a_route_with_headway(self):
        for (rid, did), mins in self.eng.route_typical_headway_min.items():
            if mins:
                return rid, did, mins
        self.skipTest("no headway data in this feed")

    def test_expected_wait_is_half_the_headway(self):
        rid, did, mins = self._a_route_with_headway()
        expected = min(mins * 30.0, self.eng.EXPECTED_WAIT_CAP_SEC)
        self.assertAlmostEqual(self.eng._expected_wait_sec(rid, did), expected, places=3)

    def test_unknown_route_falls_back_rather_than_being_free(self):
        """A route with no frequencies.txt entry must not be treated as
        turning up instantly -- that would make undocumented routes look
        strictly better than documented ones."""
        self.assertAlmostEqual(
            self.eng._expected_wait_sec("no-such-route", "0"),
            self.eng.FALLBACK_HEADWAY_MIN * 30.0,
            places=3,
        )

    def test_the_cap_bounds_the_tail(self):
        """At a 3-hour headway nobody waits 90 minutes; they find another
        way. Uncapped, one bad route would poison a sane itinerary."""
        self.eng.route_typical_headway_min[("__test_cap__", "0")] = 180.0
        try:
            self.assertEqual(
                self.eng._expected_wait_sec("__test_cap__", "0"),
                self.eng.EXPECTED_WAIT_CAP_SEC,
            )
        finally:
            del self.eng.route_typical_headway_min[("__test_cap__", "0")]

    def test_tuned_penalties_survive_as_a_floor(self):
        """Real headway data may say a wait is LONGER than the tuned
        transfer penalties assumed. It must never argue one is shorter --
        those constants each fixed a specific reported routing bug."""
        rid, did, _ = self._a_route_with_headway()
        huge_floor = 99999.0
        self.assertEqual(self.eng._expected_wait_sec(rid, did, floor_sec=huge_floor), huge_floor)

    def test_quoted_time_is_riding_plus_waiting(self):
        """raw_time must be a breakdown, not a number the app has to add
        `wait_min` onto itself."""
        r = self.route(30.0663125, 31.3806875, 30.0959491, 31.3734994)
        transit = [o for o in r.get('options', []) if o.get('type') != 'Walk']
        self.assertTrue(transit, "expected at least one transit option")
        for o in transit:
            with self.subTest(option=o['type']):
                self.assertEqual(int(o['time']), o['riding_min'] + o['wait_min'])
                self.assertGreater(o['wait_min'], 0, "a transit trip involves waiting")

    def test_every_boarding_carries_its_own_wait(self):
        """Including the first one -- the search deliberately doesn't
        charge it (it can't discriminate between options from the same
        origin), but the rider still stands there."""
        r = self.route(30.0663125, 31.3806875, 30.0959491, 31.3734994)
        for o in r.get('options', []):
            if o.get('type') == 'Walk':
                continue
            boardings = [i for i in o['instructions'] if i.get('action') in ('board', 'transfer')]
            self.assertTrue(boardings)
            for b in boardings:
                with self.subTest(option=o['type'], route=b.get('route_number')):
                    self.assertIn('wait_min', b)

    def test_waiting_moved_us_toward_googles_answer(self):
        """Regression on the case that prompted this. Google's five
        options for this pair ran 69-91 min against our old 41. We should
        now be in the same neighbourhood -- not identical (Google models a
        specific departure, we model an average rider), but no longer
        quoting half."""
        r = self.route(30.0663125, 31.3806875, 30.0959491, 31.3734994)
        transit = [o for o in r.get('options', []) if o.get('type') != 'Walk']
        best = min(int(o['time']) for o in transit)
        self.assertGreater(best, 45, "still quoting riding time only")
        self.assertLess(best, 120, "over-corrected past anything defensible")


class TestCalendarFiltering(GuidyEngineTestCase):
    def test_weekday_only_routes_are_excluded_on_friday(self):
        inactive_friday = self.eng._inactive_routes_for_day('friday')
        import csv
        weekday_only = set()
        with open('gtfs_data/trips.txt') as f:
            for row in csv.DictReader(f):
                if row['service_id'] == 'Ground_Weekdays':
                    weekday_only.add(row['route_id'])
        self.assertTrue(weekday_only, "expected at least one Ground_Weekdays-only route in the feed")
        sample = next(iter(weekday_only))
        self.assertIn(sample, inactive_friday)

    def test_weekday_only_routes_are_active_sunday(self):
        inactive_sunday = self.eng._inactive_routes_for_day('sunday')
        import csv
        weekday_only = set()
        with open('gtfs_data/trips.txt') as f:
            for row in csv.DictReader(f):
                if row['service_id'] == 'Ground_Weekdays':
                    weekday_only.add(row['route_id'])
        sample = next(iter(weekday_only))
        self.assertNotIn(sample, inactive_sunday)


class TestDataIntegrity(GuidyEngineTestCase):
    """Sanity checks on the GTFS feed itself, independent of any specific
    route query -- catches bad data before it ever reaches the router."""

    def test_all_stops_within_egypt_bounds(self):
        EGYPT_LAT_RANGE = (22.0, 32.0)
        EGYPT_LON_RANGE = (24.0, 37.0)
        bad = [(sid, d) for sid, d in self.eng.stops.items()
               if not (EGYPT_LAT_RANGE[0] <= d['lat'] <= EGYPT_LAT_RANGE[1]
                       and EGYPT_LON_RANGE[0] <= d['lon'] <= EGYPT_LON_RANGE[1])]
        self.assertEqual(bad, [], f"stops with out-of-bounds coordinates: {bad}")

    def test_no_zero_or_null_island_coordinates(self):
        bad = [(sid, d) for sid, d in self.eng.stops.items() if d['lat'] == 0 and d['lon'] == 0]
        self.assertEqual(bad, [])

    def test_metro_routes_have_metro_vehicle_type(self):
        from raptor_engine import AGENCY_VEHICLE_TYPES
        self.assertEqual(AGENCY_VEHICLE_TYPES.get('NAT'), 'metro')


class TestRealWorldRegressionSweep(GuidyEngineTestCase):
    """Known-good real trips with expected distance ranges (not exact
    values, since the router may reasonably pick different paths over
    time -- these bound-check for 'still sane', not 'still identical').
    If a future change makes one of these wildly different, investigate
    before assuming it's fine."""

    def _check_route_is_sane(self, slat, slon, elat, elon, max_reasonable_km):
        r = self.route(slat, slon, elat, elon)
        self.assertTrue(r.get('options'), "expected at least one route option")
        best = r['options'][0]
        self.assertLess(best['distance_m'], max_reasonable_km * 1000,
                         f"route distance {best['distance_m']}m exceeds sane bound for this trip")
        self.assertGreater(best['distance_m'], 0)

    def test_nasr_city_to_faisal(self):
        self._check_route_is_sane(30.0731, 31.3467, 30.0175, 31.2037, max_reasonable_km=25)

    def test_maadi_to_zamalek(self):
        self._check_route_is_sane(29.9603, 31.2577, 30.0616, 31.2194, max_reasonable_km=20)

    def test_helwan_to_shubra_el_kheima(self):
        self._check_route_is_sane(29.8488, 31.3343, 30.1225, 31.2447, max_reasonable_km=45)

    def test_downtown_short_hop(self):
        self._check_route_is_sane(30.0444, 31.2357, 30.0524, 31.2468, max_reasonable_km=3)

    def test_sixth_of_october_to_downtown(self):
        """Known weak-coverage satellite city -- should still find *some*
        route via informal transit, not fail outright."""
        r = self.route(29.9285, 30.9188, 30.0444, 31.2357)
        self.assertTrue(r.get('options'), "6th October City should still have some route, even if a long one")


class TestRailModes(GuidyEngineTestCase):
    """
    The East Nile Monorail, the Cairo LRT and the airport people mover,
    added to the feed by build_rail_gtfs.py from OSM + published operator
    figures.

    These lines are the first thing in this feed that is neither a road
    vehicle nor the metro, and almost every bug found while adding them
    came from code that had only ever seen those two: agency_id alone
    could not tell a NAT monorail from a NAT metro, the road-tuned speed
    guard rejected the LRT's real timings as impossible, and the fare
    logic assumed one banded ticket per journey rather than one per mode.
    """

    EXPECTED = {
        "RL_MONO_EN": ("monorail", 12),
        "RL_LRT_CAP": ("lrt", 2),
        "RL_LRT_RAM": ("lrt", 2),
        "RL_APM": ("apm", 12),
    }

    def test_all_rail_routes_loaded_with_the_right_mode(self):
        for route_id, (vtype, route_type) in self.EXPECTED.items():
            self.assertIn(route_id, self.eng.routes, f"{route_id} missing from feed")
            info = self.eng.routes[route_id]
            self.assertEqual(info["vehicle_type"], vtype, route_id)
            self.assertEqual(str(info["type"]), str(route_type), route_id)

    def test_nat_monorail_is_not_labelled_metro(self):
        """The regression that motivated resolve_vehicle_type. The monorail
        and the LRT are operated by NAT, same as the Cairo Metro, so
        agency-based detection called all three 'metro' -- which also meant
        charging a monorail ride the metro's fare tiers."""
        self.assertEqual(self.eng.routes["RL_MONO_EN"]["vehicle_type"], "monorail")
        self.assertNotIn("RL_MONO_EN", self.eng.metro_route_ids)
        self.assertIn("RL_MONO_EN", self.eng.rail_route_ids)

    def test_each_banded_mode_is_a_separate_ticket(self):
        from raptor_engine import banded_journey_fare
        # Metro and monorail bands are genuinely different tables; if the
        # two ever collapse into one, this catches it.
        self.assertEqual(banded_journey_fare("metro", 5), 10)
        self.assertEqual(banded_journey_fare("monorail", 5), 20)
        self.assertEqual(banded_journey_fare("monorail", 21), 80)
        self.assertEqual(banded_journey_fare("lrt", 3), 10)
        self.assertEqual(banded_journey_fare("apm", 3), 0)
        self.assertEqual(banded_journey_fare("monorail", 0), 0)

    def test_banded_modes_are_not_also_charged_per_leg(self):
        """_estimate_leg_fare must return 0 for them, or the journey-level
        band gets added on top of a per-boarding fare."""
        for route_id in self.EXPECTED:
            self.assertEqual(
                self.eng._estimate_leg_fare(route_id, 5000), 0,
                f"{route_id} is charging a per-leg fare on top of its band"
            )

    def test_rail_timings_match_published_end_to_end_figures(self):
        """The road-tuned implausible-timing guard rejects anything faster
        than 15.3 m/s straight-line, which is a speed derived from road
        circuity. Applied to grade-separated rail it threw away the LRT's
        real hop times and fell back to a 20 km/h urban-surface estimate,
        quoting a 60-minute line at 172 minutes."""
        from lookups import TransitLookups
        lookups = TransitLookups(self.eng)
        by_name = {s["name"]: s["stop_id"] for s in lookups.rail_stations()}
        cases = [
            ("Adly Mansour", "Arts and Culture City", 60),
            ("Adly Mansour", "Knowledge City", 40),
            ("Cairo Stadium", "Justice City", 60),
        ]
        for origin, dest, published in cases:
            with self.subTest(line=f"{origin} -> {dest}"):
                self.assertIn(origin, by_name)
                self.assertIn(dest, by_name)
                result = lookups.plan_rail(by_name[origin], by_name[dest])
                self.assertIsNotNone(result, f"no rail path {origin} -> {dest}")
                self.assertAlmostEqual(
                    result["time_min"], published, delta=published * 0.15,
                    msg=f"{origin} -> {dest}: {result['time_min']} min "
                        f"against a published {published}"
                )

    def test_lrt_and_monorail_are_connected(self):
        """Their interchange at Arts and Culture City is 628m apart, past
        the general 500m WALK_LINK_RADIUS_M. Without the cross-mode rail
        interchange pass the monorail's whole New Capital half is
        unreachable by rail and this returns None."""
        from lookups import TransitLookups
        lookups = TransitLookups(self.eng)
        by_name = {s["name"]: s["stop_id"] for s in lookups.rail_stations()}
        result = lookups.plan_rail(by_name["Adly Mansour"], by_name["Justice City"])
        self.assertIsNotNone(result, "LRT and monorail are not connected")
        modes = {leg["line"]["vehicle_type"] for leg in result["legs"]}
        self.assertEqual(modes, {"lrt", "monorail"})
        # Two modes ridden, so two tickets -- not one pooled band.
        self.assertEqual(len(result["fare_breakdown"]), 2)
        self.assertEqual(
            result["fare_egp"],
            sum(f["fare_egp"] for f in result["fare_breakdown"])
        )

    def test_monorail_is_not_offered_after_it_closes(self):
        """Published hours are 06:00-18:00 daily. Offering it at 20:00
        sends a rider to a shut station in the New Administrative
        Capital, which is not somewhere to be stranded."""
        closed_evening = self.eng._routes_not_running_at(20 * 3600)
        self.assertIn("RL_MONO_EN", closed_evening)
        closed_midday = self.eng._routes_not_running_at(12 * 3600)
        self.assertNotIn("RL_MONO_EN", closed_midday)

    def test_metro_only_planner_still_refuses_non_metro(self):
        """Generalising the planner to rail must not quietly turn the
        Metro tab into a rail tab -- a rider asking for the metro is
        asking for the certainty of a train they already know."""
        from lookups import TransitLookups
        lookups = TransitLookups(self.eng)
        by_name = {s["name"]: s["stop_id"] for s in lookups.rail_stations()}
        self.assertIsNone(
            lookups.plan_metro(by_name["Adly Mansour"], by_name["Justice City"]),
            "the metro-only planner routed over the monorail"
        )
        self.assertTrue(
            all("metro" in s["modes"] for s in lookups.metro_stations()),
            "metro_stations() is returning non-metro stations"
        )

    def test_new_capital_is_now_reachable(self):
        """The route-comparison audit recorded the New Administrative
        Capital's government district as a genuine no-route: the nearest
        stop with any departure was ~10km away. The monorail serves it
        directly, so this should now answer."""
        result = self.eng.run_raptor_by_coords(
            30.0900, 31.3300, 30.0059, 31.7701, now=self.FIXED_NOW
        )
        self.assertTrue(result.get("success"), result.get("reason"))
        used = {
            step.get("vehicle_type")
            for option in result["options"]
            for step in option["instructions"]
        }
        self.assertIn("monorail", used, "the monorail is not being used to reach the New Capital")




class TestLookups(GuidyEngineTestCase):
    """Route browser and metro-only planner (lookups.py), added 2026-08-24.

    Neither is routing in the RAPTOR sense -- the browser is a table of
    contents over route_stop_sequence, the metro planner is a shortest path
    over 84 nodes -- but both are rider-facing and both must agree with the
    trip planner about fares, so they are tested against the same engine.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from lookups import TransitLookups
        cls.lookups = TransitLookups(cls.eng)

    # --- metro ---------------------------------------------------------
    def test_metro_network_shape(self):
        """Three lines, 84 stations, five interchanges -- the real Cairo
        metro. A change here means the feed changed, not the code."""
        stations = self.lookups.metro_stations('en')
        self.assertEqual(len(stations), 84)
        interchanges = {s['name'] for s in stations if s['is_interchange']}
        self.assertEqual(
            interchanges,
            {'Sadat', 'Al-Shohadaa', 'Attaba', 'Nasser', 'Cairo University'},
        )

    def _station_id(self, name):
        return next(s['stop_id'] for s in self.lookups.metro_stations('en') if s['name'] == name)

    def test_same_line_trip_has_no_interchange(self):
        r = self.lookups.plan_metro(self._station_id('Sadat'), self._station_id('Dokki'), 'en')
        self.assertIsNotNone(r)
        self.assertEqual(r['interchange_count'], 0)
        self.assertEqual(len(r['legs']), 1)
        self.assertEqual(r['total_stops'], 2)

    def test_cross_line_trip_changes_at_a_real_interchange(self):
        """Helwan to Adly Mansour is Line 1 then Line 3; the only station
        where those two meet is Nasser."""
        r = self.lookups.plan_metro(self._station_id('Helwan'), self._station_id('Adly Mansour'), 'en')
        self.assertIsNotNone(r)
        self.assertEqual(r['interchange_count'], 1)
        self.assertEqual(r['interchanges'][0]['station']['name'], 'Nasser')

    def test_metro_fare_matches_the_trip_planner(self):
        """The browser and the planner must never quote different fares for
        the same ride -- both go through metro_journey_fare()."""
        from raptor_engine import metro_journey_fare
        r = self.lookups.plan_metro(self._station_id('Helwan'), self._station_id('Adly Mansour'), 'en')
        self.assertEqual(r['fare_egp'], metro_journey_fare(r['total_stops']))

    def test_every_leg_names_the_direction(self):
        """'Line 1' is not actionable; 'Line 1 towards New El-Marg' is --
        it's what's on the platform sign."""
        r = self.lookups.plan_metro(self._station_id('Helwan'), self._station_id('Adly Mansour'), 'en')
        for leg in r['legs']:
            self.assertIsNotNone(leg['towards'], f"leg on {leg['line']['name']} has no direction")

    def test_non_metro_endpoints_are_rejected(self):
        """A bus stop is not a metro station; say so rather than inventing
        a journey."""
        self.assertIsNone(self.lookups.plan_metro('definitely-not-a-station', 'nor-this', 'en'))

    # --- route browser -------------------------------------------------
    def test_every_route_is_browsable(self):
        all_routes = self.lookups.search_routes('', 'en', limit=99999)
        self.assertEqual(len(all_routes), len(self.eng.routes))

    def test_search_matches_corridor_not_just_number(self):
        """Several hundred microbus routes have no number at all, so
        searching only numbers would hide them entirely."""
        hits = self.lookups.search_routes('maadi', 'en', limit=50)
        self.assertTrue(hits)
        self.assertTrue(any('Maadi' in f"{h['from']} {h['to']} {h['description']}" for h in hits))

    def test_search_works_in_arabic(self):
        hits = self.lookups.search_routes('المعادي', 'ar', limit=20)
        self.assertTrue(hits, "Arabic search returned nothing")

    def test_route_detail_lists_stops_in_order(self):
        rid = next(r['route_id'] for r in self.lookups.search_routes('CTA 65', 'en', limit=1))
        d = self.lookups.route_detail(rid, 'en')
        self.assertTrue(d['directions'])
        for direction in d['directions']:
            self.assertGreater(len(direction['stops']), 1)
            self.assertIsNotNone(direction['towards'])

    def test_metro_route_detail_quotes_no_flat_fare(self):
        """Metro fare is banded by hops across the journey, so a single
        number for the whole line would be wrong in both directions."""
        d = self.lookups.route_detail('NAT_L2', 'en')
        self.assertIsNone(d['fare_egp'])

    def test_surface_route_detail_quotes_its_flat_fare(self):
        rid = next(r['route_id'] for r in self.lookups.search_routes('CTA 65', 'en', limit=1))
        self.assertEqual(self.lookups.route_detail(rid, 'en')['fare_egp'], 20)

    def test_unknown_route_returns_none(self):
        self.assertIsNone(self.lookups.route_detail('no-such-route', 'en'))

    def test_exact_number_outranks_a_longer_number_containing_it(self):
        """Searching '65' used to return CTA 1065 first -- it also contains
        those digits and happened to have one more stop. A rider typing a
        number means that number."""
        results = self.lookups.search_routes('65', 'en', limit=5)
        self.assertTrue(results)
        self.assertEqual(results[0]['number'], 'CTA 65')

    def test_microbus_detail_quotes_no_flat_fare(self):
        """Microbus fares are distance-based, so one number for a whole
        corridor would be a guess presented as a fact."""
        microbus = next(r for r in self.lookups.search_routes('', 'en', limit=400)
                        if r['vehicle_type'] == 'microbus')
        self.assertIsNone(self.lookups.route_detail(microbus['route_id'], 'en')['fare_egp'])

    def test_branch_variants_are_not_hidden(self):
        """Metro Line 3 branches at Kit Kat: Rod El-Farag Corridor (29 stops)
        and Cairo University (28). The browser used to show only the longest
        per direction, so the Cairo University branch was invisible in the
        app while the trip planner routed people along it."""
        detail = self.lookups.route_detail('NAT_L3', 'en')
        towards = {d['towards'] for d in detail['directions']}
        self.assertIn('Cairo University', towards)
        self.assertIn('Rod El-Farag Corridor', towards)
        # Two directions, two variants each.
        self.assertEqual(len(detail['directions']), 4)
        self.assertEqual(sum(1 for d in detail['directions'] if d['is_main_variant']), 2)

    def test_every_declared_route_still_appears(self):
        """Nothing may silently vanish from the browser -- including three
        Mwasalat Misr routes whose stops all cluster into one hub."""
        import csv, io as _io, os
        path = os.path.join(self.eng.folder, 'routes.txt')
        with _io.open(path, encoding='utf-8-sig') as f:
            declared = {r['route_id'] for r in csv.DictReader(f)}
        browsable = {r['route_id'] for r in
                     self.lookups.search_routes('', 'en', limit=100000)}
        self.assertEqual(declared - browsable, set())

    def test_search_result_carries_no_internal_ranking_field(self):
        """_rank is a sort key, not part of the API contract -- it must not
        leak into the JSON the app parses."""
        for row in self.lookups.search_routes('65', 'en', limit=5):
            self.assertNotIn('_rank', row)


if __name__ == '__main__':
    unittest.main()
