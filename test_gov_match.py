"""
Unit tests for gov_match's landmark-normalisation and containment-matching
helpers -- the pure functions that decide whether a Cairo Governorate
corridor landmark is the same place as a GTFS stop.
"""

import unittest

from gov_match import CONTAINS_MIN_CHARS, contains_match, is_road, normalise


class TestNormalisePrefixStripping(unittest.TestCase):
    def test_al_prefixed_street_word_normalises_same_as_bare_prefix(self):
        # "شارع العاشر" (bare) and "الشارع العاشر" (definite article glued
        # onto the prefix word itself, not just onto the street name) are
        # the same phrase in real usage -- the governorate documents are
        # inconsistent about which form they write. normalise() already
        # handles this for "حي"/"الحي" (see the next test), so the other
        # prefix words should not behave differently.
        self.assertEqual(
            normalise("الشارع العاشر"),
            normalise("شارع العاشر"),
        )

    def test_al_prefixed_hai_already_normalises_same_as_bare(self):
        # Locks in the behaviour the "الحي"/"حي" special case already
        # provides, so a future refactor of the prefix list can't
        # regress it silently.
        self.assertEqual(
            normalise("الحي الثامن"),
            normalise("حي الثامن"),
        )

    def test_strips_diacritics_and_unifies_hamza_and_ta_marbuta(self):
        self.assertEqual(normalise("مِصر الجَديدة"), normalise("مصر الجديده"))


class TestIsRoad(unittest.TestCase):
    def test_true_for_a_named_corridor(self):
        self.assertTrue(is_road("كورنيش النيل"))

    def test_false_for_an_ordinary_street_landmark(self):
        # "شارع النيل" names a specific street landmark, not one of the
        # named corridors in ROAD_WORDS -- it should be treated as a
        # candidate stop, not dropped as a road.
        self.assertFalse(is_road("شارع النيل"))


class TestContainsMatch(unittest.TestCase):
    def test_finds_landmark_contained_in_a_longer_stop_key(self):
        normed = {"mainstreetjunction": ["1750"]}
        self.assertEqual(
            contains_match("mainstreet", normed, list(normed)),
            ("1750", "mainstreetjunction"),
        )

    def test_rejects_landmark_below_min_chars(self):
        # Too short to be distinctive on its own -- matching it by
        # containment would hit unrelated stops that merely share a
        # short substring.
        short = "a" * (CONTAINS_MIN_CHARS - 1)
        normed = {short + "somewhereelse": ["1"]}
        self.assertIsNone(contains_match(short, normed, list(normed)))

    def test_rejects_landmark_that_is_a_small_fraction_of_the_stop_key(self):
        # Real case: the feed names this stop by its full junction
        # ("طريق العروبة (شارع الثورة)"); "العروبة" alone is a real
        # landmark a governorate document would write, but it's under a
        # third of the combined key's length -- below CONTAINS_MIN_RATIO,
        # so it should not be accepted as a match (it would also match
        # plenty of unrelated stops that happen to share the fragment).
        stop_key = normalise("طريق العروبة (شارع الثورة)")
        landmark_key = normalise("العروبة")
        self.assertIsNone(
            contains_match(landmark_key, {stop_key: ["1750"]}, [stop_key])
        )


if __name__ == "__main__":
    unittest.main()
