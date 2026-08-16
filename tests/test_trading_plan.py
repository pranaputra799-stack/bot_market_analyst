"""Unit tests untuk generator trading plan (analysis/trading_plan.py) — tanpa network."""

import unittest

from analysis.trading_plan import (
    compute_position_sizes,
    format_profile_line,
    format_trading_plan,
    parse_plan_json,
    resolve_yahoo_symbol,
    validate_profile_input,
)

PROFILE = {
    "balance": 1000.0,
    "risk_per_trade": 2.0,
    "trading_style": "swing",
    "favorite_pairs": "XAU/USD,EUR/USD",
    "trading_hours": "09:00-16:00",
}


class TestValidateProfile(unittest.TestCase):
    def test_valid_input(self):
        p = validate_profile_input(["1000", "2", "swing", "XAU/USD,EUR/USD", "09:00-16:00"])
        self.assertNotIn("error", p)
        self.assertEqual(p["balance"], 1000.0)
        self.assertEqual(p["risk_per_trade"], 2.0)
        self.assertEqual(p["trading_style"], "swing")
        self.assertIn("XAU/USD", p["favorite_pairs"])

    def test_style_aliases(self):
        self.assertEqual(validate_profile_input(["100", "1", "scalp", "EUR/USD"])["trading_style"], "scalping")
        self.assertEqual(validate_profile_input(["100", "1", "day_trade", "EUR/USD"])["trading_style"], "day_trade")

    def test_bad_inputs(self):
        self.assertIn("error", validate_profile_input(["abc", "2", "swing", "EUR/USD"]))
        self.assertIn("error", validate_profile_input(["100", "0", "swing", "EUR/USD"]))
        self.assertIn("error", validate_profile_input(["100", "150", "swing", "EUR/USD"]))
        self.assertIn("error", validate_profile_input(["100", "2", "hoki", "EUR/USD"]))
        self.assertIn("error", validate_profile_input(["100", "2", "swing"]))
        self.assertIn("error", validate_profile_input([]))

    def test_symbol_normalization(self):
        p = validate_profile_input(["100", "1", "swing", "XAUUSD,EUR/USD"])
        self.assertIn("XAU/USD", p["favorite_pairs"])


class TestResolveYahoo(unittest.TestCase):
    def test_forex_label(self):
        self.assertEqual(resolve_yahoo_symbol("EUR/USD"), "EURUSD=X")
        self.assertEqual(resolve_yahoo_symbol("gbp/usd"), "GBPUSD=X")

    def test_special_labels(self):
        self.assertEqual(resolve_yahoo_symbol("XAU/USD (Gold)"), "GC=F")
        self.assertEqual(resolve_yahoo_symbol("BTC/USD (Bitcoin)"), "BTC-USD")

    def test_unknown(self):
        self.assertIsNone(resolve_yahoo_symbol(""))
        self.assertIsNone(resolve_yahoo_symbol(None))


class TestParsePlanJson(unittest.TestCase):
    def test_plain_json(self):
        text = '{"market_outlook": "ok", "pairs": [{"symbol": "XAU/USD"}]}'
        plan = parse_plan_json(text)
        self.assertEqual(plan["pairs"][0]["symbol"], "XAU/USD")

    def test_fenced_json(self):
        text = "```json\n{\"market_outlook\": \"ok\"}\n```"
        plan = parse_plan_json(text)
        self.assertEqual(plan["market_outlook"], "ok")

    def test_invalid_returns_empty(self):
        self.assertEqual(parse_plan_json("tidak ada json"), {})
        self.assertEqual(parse_plan_json(""), {})
        self.assertEqual(parse_plan_json(None), {})


class TestPositionSizing(unittest.TestCase):
    def test_gold_lot_calculation(self):
        pairs = [
            {"symbol": "XAU/USD", "direction": "long", "entry": 2400.0, "stop_loss": 2390.0, "take_profit": 2420.0}
        ]
        sized = compute_position_sizes(PROFILE, pairs)
        pos = sized[0]["position"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["symbol"], "XAU/USD")
        # XAU pip = 0.1 → jarak 10 poin = 100 pips; risk $20 → 20/(100*10) = 0.02 lot
        self.assertAlmostEqual(sized[0]["sl_pips"], 100.0)
        self.assertAlmostEqual(pos["lots"], 0.02, places=3)

    def test_forex_lot_calculation(self):
        pairs = [{"symbol": "EUR/USD", "entry": 1.0850, "stop_loss": 1.0800, "take_profit": 1.0950}]
        sized = compute_position_sizes(PROFILE, pairs)
        pos = sized[0]["position"]
        # EUR/USD pip = 0.0001 → SL 50 pips → 20/(50*10) = 0.04 lot
        self.assertAlmostEqual(pos["lots"], 0.04, places=4)

    def test_missing_fields_skip_sizing(self):
        pairs = [{"symbol": "XAU/USD", "entry": 2400.0}]
        sized = compute_position_sizes(PROFILE, pairs)
        self.assertIsNone(sized[0]["position"])


class TestFormat(unittest.TestCase):
    def test_format_profile_line(self):
        self.assertIn("$1,000", format_profile_line(PROFILE))
        self.assertIn("2%/trade", format_profile_line(PROFILE))

    def test_format_plan_message(self):
        plan = {
            "market_outlook": "Pasar tenang.",
            "pairs": [
                {
                    "symbol": "XAU/USD",
                    "direction": "long",
                    "bias_summary": "Uptrend lanjut.",
                    "entry": 2400.0,
                    "stop_loss": 2390.0,
                    "take_profit": 2420.0,
                    "fundamental_reason": "Dollar melemah.",
                    "technical_reason": "RSI sehat.",
                }
            ],
            "risk_notes": "NFP Jumat.",
        }
        msg = format_trading_plan(PROFILE, plan, "Senin, 17 Agustus 2026")
        self.assertIn("RENCANA TRADING", msg)
        self.assertIn("XAU/USD", msg)
        self.assertIn("LONG", msg)
        self.assertIn("1:2.0", msg)
        self.assertIn("NFP", msg)

    def test_format_empty_plan(self):
        msg = format_trading_plan(PROFILE, {}, "Senin, 17 Agustus 2026")
        self.assertIn("tidak menghasilkan", msg)


if __name__ == "__main__":
    unittest.main()
