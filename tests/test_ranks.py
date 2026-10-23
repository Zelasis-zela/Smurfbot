import unittest

from core import ranks


class TierGroupTests(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(ranks.tier_group(3), 0)
        self.assertEqual(ranks.tier_group(5), 0)
        self.assertEqual(ranks.tier_group(6), 1)
        self.assertEqual(ranks.tier_group(14), 3)
        self.assertEqual(ranks.tier_group(24), 7)
        self.assertEqual(ranks.tier_group(26), 7)
        self.assertEqual(ranks.tier_group(27), 8)

    def test_invalid(self):
        for tier in (None, 0, 1, 2, 28, -1, "14", True, 3.5):
            self.assertIsNone(ranks.tier_group(tier), tier)


class TierNameTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ranks.tier_name(3), "Fer 1")
        self.assertEqual(ranks.tier_name(11), "Argent 3")
        self.assertEqual(ranks.tier_name(13), "Or 2")
        self.assertEqual(ranks.tier_name(14), "Or 3")
        self.assertEqual(ranks.tier_name(15), "Platine 1")
        self.assertEqual(ranks.tier_name(20), "Diamant 3")
        self.assertEqual(ranks.tier_name(21), "Ascendant 1")
        self.assertEqual(ranks.tier_name(24), "Immortel 1")
        self.assertEqual(ranks.tier_name(27), "Radiant")
        self.assertEqual(ranks.tier_name(14.0), "Or 3")

    def test_unranked(self):
        for tier in (None, 0, 1, 2, 28, "Or"):
            self.assertEqual(ranks.tier_name(tier), "Non classé")

    def test_group_name(self):
        self.assertEqual(ranks.group_name(0), "Fer")
        self.assertEqual(ranks.group_name(3), "Or")
        self.assertEqual(ranks.group_name(8), "Radiant")
        self.assertEqual(ranks.group_name(9), "Non classé")
        self.assertEqual(ranks.group_name(None), "Non classé")

    def test_icon(self):
        self.assertEqual(
            ranks.tier_icon(15),
            "https://media.valorant-api.com/competitivetiers/03621f52-342b-cf4e-4f86-9350a49c6d04/15/smallicon.png")
        self.assertIn("/0/smallicon.png", ranks.tier_icon(None))
        self.assertIn("/0/smallicon.png", ranks.tier_icon(0))
        self.assertIn("/0/smallicon.png", ranks.tier_icon(40))


class TierFromNameTests(unittest.TestCase):
    def test_english_and_french(self):
        cases = {
            "Gold 2": 13, "Or 2": 13, "Radiant": 27, "Unrated": 0, "Non classé": 0, "non classe": 0,
            "Immortel 1": 24, "Immortal 1": 24, "Iron 1": 3, "Fer 1": 3, "Platinum 1": 15, "Platine 3": 17,
            "Diamant 3": 20, "silver 1": 9, "Argent 1": 9, "ASCENDANT 2": 22, "  Bronze   3 ": 8, "Gold": 12,
        }
        for name, tier in cases.items():
            self.assertEqual(ranks.tier_from_name(name), tier, name)

    def test_unknown(self):
        for name in ("Inconnu", "", None, "Gold 4", "Gold 0", "Mithril 2", 13):
            self.assertIsNone(ranks.tier_from_name(name), name)

    def test_round_trip(self):
        for tier in range(3, 28):
            self.assertEqual(ranks.tier_from_name(ranks.tier_name(tier)), tier)


class HsThresholdTests(unittest.TestCase):
    def test_rows(self):
        self.assertEqual(ranks.hs_threshold(3), (30.0, "Fer–Argent"))
        self.assertEqual(ranks.hs_threshold(11), (30.0, "Fer–Argent"))
        self.assertEqual(ranks.hs_threshold(12), (33.0, "Or–Platine"))
        self.assertEqual(ranks.hs_threshold(17), (33.0, "Or–Platine"))
        self.assertEqual(ranks.hs_threshold(18), (36.0, "Diamant–Ascendant"))
        self.assertEqual(ranks.hs_threshold(23), (36.0, "Diamant–Ascendant"))
        self.assertEqual(ranks.hs_threshold(24), (40.0, "Immortel–Radiant"))
        self.assertEqual(ranks.hs_threshold(27), (40.0, "Immortel–Radiant"))

    def test_unranked_default(self):
        for tier in (None, 0, 1, 2, 99):
            self.assertEqual(ranks.hs_threshold(tier), (33.0, "Or–Platine"))


if __name__ == "__main__":
    unittest.main()
