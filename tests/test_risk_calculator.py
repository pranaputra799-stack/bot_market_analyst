"""Unit tests untuk position size calculator (utils/risk_calculator.py)."""

import unittest

from utils.risk_calculator import calculate_position_size, format_risk_result, _parse_symbol


class TestParseSymbol(unittest.TestCase):
    def test_slash_format(self):
        self.assertEqual(_parse_symbol("XAU/USD"), ("XAU", "USD"))

    def test_concatenated_format(self):
        self.assertEqual(_parse_symbol("eurusd"), ("EUR", "USD"))

    def test_whitespace_case(self):
        self.assertEqual(_parse_symbol(" eur / usd "), ("EUR", "USD"))


class TestCalculatePositionSize(unittest.TestCase):
    def test_xau_basic(self):
        r = calculate_position_size(1000, 2, 20, "XAU/USD")
        self.assertNotIn("error", r)
        # risk 20 USD, pip value 10 → 0.10 lot standar
        self.assertAlmostEqual(r["risk_amount"], 20.0)
        self.assertAlmostEqual(r["lots"], 0.10, places=4)

    def test_eurusd_same_as_xau(self):
        r = calculate_position_size(1000, 2, 20, "EUR/USD")
        self.assertAlmostEqual(r["lots"], 0.10, places=4)

    def test_usd_jpy_needs_quote(self):
        r = calculate_position_size(1000, 2, 20, "USD/JPY")
        self.assertIn("error", r)

    def test_usd_jpy_with_quote(self):
        r = calculate_position_size(1000, 2, 20, "USD/JPY", price_quote=155.0)
        self.assertNotIn("error", r)
        # pip value = 0.01 * 100000 / 155 ≈ 6.4516 → lots = 20 / (20*6.4516)
        expected = 20.0 / (20.0 * (0.01 * 100000 / 155.0))
        self.assertAlmostEqual(r["lots"], expected, places=4)
        self.assertAlmostEqual(r["pip"], 0.01)

    def test_xag_pip_value(self):
        r = calculate_position_size(1000, 1, 10, "XAG/USD")
        self.assertAlmostEqual(r["pip_value_per_lot"], 50.0)
        self.assertAlmostEqual(r["lots"], 10.0 / (10 * 50.0), places=4)

    def test_invalid_inputs(self):
        self.assertIn("error", calculate_position_size(0, 2, 20))
        self.assertIn("error", calculate_position_size(1000, 0, 20))
        self.assertIn("error", calculate_position_size(1000, 2, 0))
        self.assertIn("error", calculate_position_size("abc", 2, 20))

    def test_risk_pct_capped(self):
        self.assertIn("error", calculate_position_size(1000, 150, 20))

    def test_lot_breakdown(self):
        r = calculate_position_size(5000, 1, 10, "XAU/USD")
        # risk 50 USD → 0.5 lot standar = 5 mini = 50 mikro
        self.assertAlmostEqual(r["lots"], 0.5, places=4)
        self.assertAlmostEqual(r["lots_mini"], 5.0, places=4)
        self.assertAlmostEqual(r["lots_micro"], 50.0, places=4)


class TestFormatRiskResult(unittest.TestCase):
    def test_success_format(self):
        r = calculate_position_size(1000, 2, 20)
        text = format_risk_result(r)
        self.assertIn("POSITION SIZE", text.upper())
        self.assertIn("0.10", text)

    def test_error_format(self):
        r = calculate_position_size(1000, 2, 20, "USD/JPY")
        text = format_risk_result(r)
        self.assertIn("❌", text)


if __name__ == "__main__":
    unittest.main()
