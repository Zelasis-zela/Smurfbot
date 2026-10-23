import unittest

from core import fmt

MINUS = "−"


class NumTests(unittest.TestCase):
    def test_decimals(self):
        self.assertEqual(fmt.num(1.854, 2), "1,85")
        self.assertEqual(fmt.num(231), "231")
        self.assertEqual(fmt.num(230.6), "231")
        self.assertEqual(fmt.num(0.5, 1), "0,5")
        self.assertEqual(fmt.num(3, 2), "3,00")

    def test_thousands(self):
        self.assertEqual(fmt.num(2359), "2 359")
        self.assertEqual(fmt.num(999), "999")
        self.assertEqual(fmt.num(999.6), "1 000")
        self.assertEqual(fmt.num(1234567.25, 1), "1 234 567,2")

    def test_negative(self):
        self.assertEqual(fmt.num(-3.5, 1), MINUS + "3,5")
        self.assertEqual(fmt.num(-2500), MINUS + "2 500")
        self.assertEqual(fmt.num(-0.001, 1), "0,0")   # pas de "−0,0"

    def test_missing(self):
        for value in (None, float("nan"), float("inf"), "abc", True):
            self.assertEqual(fmt.num(value), "—")


class OtherFormatsTests(unittest.TestCase):
    def test_pct(self):
        self.assertEqual(fmt.pct(34.12), "34,1 %")
        self.assertEqual(fmt.pct(30, 0), "30 %")
        self.assertEqual(fmt.pct(None), "—")

    def test_ratio(self):
        self.assertEqual(fmt.ratio(1.72), "1,7×")
        self.assertEqual(fmt.ratio(2), "2,0×")
        self.assertEqual(fmt.ratio(None), "—")

    def test_signed(self):
        self.assertEqual(fmt.signed(17), "+17")
        self.assertEqual(fmt.signed(-12), MINUS + "12")
        self.assertEqual(fmt.signed(0), "0")
        self.assertEqual(fmt.signed(2.36, 1), "+2,4")
        self.assertEqual(fmt.signed(1500), "+1 500")
        self.assertEqual(fmt.signed(None), "—")


if __name__ == "__main__":
    unittest.main()
