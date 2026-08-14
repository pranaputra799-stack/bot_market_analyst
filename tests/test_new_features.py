"""Unit tests untuk fitur baru tanpa AI: kuota harian, journal stats, heatmap."""

import unittest
from unittest import mock

from bot import handlers as handlers_mod
from bot.handlers import MarketBot


class TestDailyQuota(unittest.TestCase):
    def _bot(self):
        bot = MarketBot.__new__(MarketBot)
        bot._daily_usage = {}
        bot._MAX_USER_ACTIVITY_ENTRIES = 5000
        return bot

    def test_quota_off_when_zero(self):
        bot = self._bot()
        with mock.patch.object(handlers_mod, "USER_DAILY_QUOTA", 0):
            self.assertFalse(bot._daily_quota_exceeded(111))
            bot._consume_daily_quota(111)
            self.assertFalse(bot._daily_quota_exceeded(111))

    def test_exceeds_after_limit(self):
        bot = self._bot()
        with mock.patch.object(handlers_mod, "USER_DAILY_QUOTA", 3):
            self.assertFalse(bot._daily_quota_exceeded(111))
            for _ in range(3):
                bot._consume_daily_quota(111)
            self.assertTrue(bot._daily_quota_exceeded(111))

    def test_new_day_resets(self):
        bot = self._bot()
        with mock.patch.object(handlers_mod, "USER_DAILY_QUOTA", 2):
            bot._consume_daily_quota(111)
            bot._consume_daily_quota(111)
            self.assertTrue(bot._daily_quota_exceeded(111))
            # Pindahkan tanggal → reset
            bot._daily_usage[111][0] = "2099-01-01"
            self.assertFalse(bot._daily_quota_exceeded(111))

    def test_quota_is_per_user(self):
        bot = self._bot()
        with mock.patch.object(handlers_mod, "USER_DAILY_QUOTA", 1):
            bot._consume_daily_quota(111)
            self.assertTrue(bot._daily_quota_exceeded(111))
            self.assertFalse(bot._daily_quota_exceeded(222))


class TestJournalStats(unittest.TestCase):
    def test_empty(self):
        s = MarketBot._journal_stats([])
        self.assertEqual(s["total"], 0)
        self.assertIsNone(s["win_rate"])

    def test_win_rate(self):
        entries = [
            {"status": "closed", "result": "win", "pnl_pct": 1.5, "symbol": "XAU/USD"},
            {"status": "closed", "result": "loss", "pnl_pct": -2.0, "symbol": "XAU/USD"},
            {"status": "closed", "result": "win", "pnl_pct": 0.5, "symbol": "EUR/USD"},
            {"status": "open", "symbol": "XAU/USD"},
        ]
        s = MarketBot._journal_stats(entries)
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["open"], 1)
        self.assertEqual(s["closed"], 3)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertAlmostEqual(s["win_rate"], 66.6666, places=2)
        self.assertAlmostEqual(s["total_pnl_pct"], 0.0)

    def test_by_pair(self):
        entries = [
            {"status": "closed", "result": "win", "pnl_pct": 1.0, "symbol": "xau/usd"},
            {"status": "closed", "result": "loss", "pnl_pct": -1.0, "symbol": "xau/usd"},
            {"status": "closed", "result": "win", "pnl_pct": 2.0, "symbol": "EUR/USD"},
        ]
        s = MarketBot._journal_stats(entries)
        self.assertEqual(s["by_pair"]["XAU/USD"], {"wins": 1, "losses": 1, "pnl_pct": 0.0})
        self.assertEqual(s["by_pair"]["EUR/USD"], {"wins": 1, "losses": 0, "pnl_pct": 2.0})


class TestMapRowFormat(unittest.TestCase):
    def test_healthy_row(self):
        ind = {
            "current_price": 2400.5,
            "price_5d_change": 1.23,
            "rsi": 62.4,
            "ema_20": 2390.0,
            "ema_50": 2380.0,
        }
        row = MarketBot._format_map_row("XAU/USD", ind)
        self.assertIn("XAU/USD", row)
        self.assertIn("2,400.5", row)  # format dengan separator ribuan
        self.assertIn("+1.23%", row)
        self.assertIn("62", row)
        self.assertIn("📈", row)

    def test_bearish_row(self):
        ind = {
            "current_price": 100.5,
            "price_5d_change": -0.5,
            "rsi": 35.0,
            "ema_20": 100.0,
            "ema_50": 101.0,
        }
        row = MarketBot._format_map_row("EUR/USD", ind)
        self.assertIn("-0.50%", row)
        self.assertIn("📉", row)

    def test_missing_data_row(self):
        row = MarketBot._format_map_row("BTC/USD", {})
        self.assertIn("BTC/USD", row)
        self.assertIn("tidak tersedia", row)


if __name__ == "__main__":
    unittest.main()
