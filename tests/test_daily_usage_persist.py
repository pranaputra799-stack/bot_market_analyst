"""Unit tests untuk kuota harian PERSISTEN (Supabase) & laporan AI usage.

- flush_daily_usage: batch ke Supabase, clear setelah sukses, restore saat gagal
- load_daily_usage: muat kuota hari ini dari Supabase saat boot
- format_ai_usage_report: format laporan token & request per provider
"""

import unittest
from unittest import mock

from bot import handlers as handlers_mod
from bot.handlers import MarketBot
from bot.messages import format_ai_usage_report


def _bot():
    bot = MarketBot.__new__(MarketBot)
    bot._daily_usage = {}
    bot._MAX_USER_ACTIVITY_ENTRIES = 5000
    return bot


class TestFlushDailyUsage(unittest.TestCase):
    def test_flush_clears_on_success(self):
        bot = _bot()
        bot._daily_usage = {111: ["2026-08-14", 3]}
        with mock.patch.object(
            handlers_mod.db, "update_daily_usage_async", new=mock.AsyncMock(return_value=True)
        ):
            import asyncio
            asyncio.run(bot.flush_daily_usage())
        self.assertEqual(bot._daily_usage, {}, "Buffer harus dikosongkan setelah flush sukses")

    def test_flush_restores_on_failure(self):
        bot = _bot()
        bot._daily_usage = {111: ["2026-08-14", 3]}
        with mock.patch.object(
            handlers_mod.db, "update_daily_usage_async", new=mock.AsyncMock(return_value=False)
        ):
            import asyncio
            asyncio.run(bot.flush_daily_usage())
        self.assertEqual(
            bot._daily_usage, {111: ["2026-08-14", 3]},
            "Hitungan harus dikembalikan ke memori bila flush gagal",
        )

    def test_flush_empty_is_noop(self):
        bot = _bot()
        bot._daily_usage = {}
        with mock.patch.object(
            handlers_mod.db, "update_daily_usage_async", new=mock.AsyncMock(return_value=True)
        ) as m:
            import asyncio
            asyncio.run(bot.flush_daily_usage())
        m.assert_not_called()

    def test_flush_uses_today_date_format(self):
        bot = _bot()
        bot._daily_usage = {111: ["2026-08-14", 5]}
        captured = {}

        async def fake_update(rows):
            captured["rows"] = rows
            return True

        with mock.patch.object(handlers_mod.db, "update_daily_usage_async", new=fake_update):
            import asyncio
            asyncio.run(bot.flush_daily_usage())
        self.assertEqual(captured["rows"], [(111, "2026-08-14", 5)])


class TestLoadDailyUsage(unittest.TestCase):
    def test_load_populates_memory(self):
        # Tanggal ditentukan oleh datetime.now (UTC) — di-mock agar test tidak
        # bergantung pada tanggal hari ini (sebelumnya hardcode '2026-08-14'
        # → suite merah saat tanggal berjalan melewatinya).
        from datetime import datetime, timezone

        fixed_now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        # datetime.datetime adalah tipe C yang immutable — atribut 'now' tidak
        # bisa di-patch. Ganti nama modul dengan Mock(wraps=...) yang hanya
        # meng-override .now (pemakaian lain tetap mendelegasikan ke datetime asli).
        fake_dt = mock.Mock(wraps=datetime)
        fake_dt.now = mock.Mock(return_value=fixed_now)
        bot = _bot()
        with mock.patch.object(handlers_mod, "datetime", fake_dt), mock.patch.object(
            handlers_mod.db, "get_daily_usage_async",
            new=mock.AsyncMock(return_value={111: 7, 222: 2}),
        ):
            import asyncio
            asyncio.run(bot.load_daily_usage())
        self.assertEqual(bot._daily_usage[111], ["2026-08-14", 7])
        self.assertEqual(bot._daily_usage[222], ["2026-08-14", 2])

    def test_load_empty_keeps_memory(self):
        bot = _bot()
        bot._daily_usage = {333: ["2026-08-14", 1]}
        with mock.patch.object(
            handlers_mod.db, "get_daily_usage_async", new=mock.AsyncMock(return_value={})
        ):
            import asyncio
            asyncio.run(bot.load_daily_usage())
        # Tidak menimpa dengan data kosong (bot tetap punya nilai lokal)
        self.assertEqual(bot._daily_usage, {333: ["2026-08-14", 1]})

    def test_load_error_is_safe(self):
        bot = _bot()
        with mock.patch.object(
            handlers_mod.db, "get_daily_usage_async",
            new=mock.AsyncMock(side_effect=RuntimeError("network down")),
        ):
            import asyncio
            asyncio.run(bot.load_daily_usage())  # tidak boleh raise
        self.assertEqual(bot._daily_usage, {})


class TestDailyUsageDatabase(unittest.TestCase):
    """Method Database.update_daily_usage / get_daily_usage."""

    def test_update_daily_usage_payload(self):
        from data import database as db_mod
        with mock.patch.object(db_mod, "_is_configured", return_value=True), \
                mock.patch.object(db_mod, "_session") as m:
            session = m.return_value
            resp = session.post.return_value
            resp.raise_for_status.return_value = None
            ok = db_mod.Database.update_daily_usage([(111, "2026-08-14", 3)])
            self.assertTrue(ok)
            args, kwargs = session.post.call_args
            self.assertIn("user_daily_usage", args[0])
            self.assertIn("resolution=merge-duplicates", kwargs["headers"]["Prefer"])
            self.assertEqual(kwargs["json"][0]["user_id"], 111)
            self.assertEqual(kwargs["json"][0]["count"], 3)

    def test_update_daily_usage_unconfigured_is_false(self):
        from data import database as db_mod
        with mock.patch.object(db_mod, "_is_configured", return_value=False):
            self.assertFalse(db_mod.Database.update_daily_usage([(111, "2026-08-14", 3)]))

    def test_get_daily_usage_filters_date(self):
        from data import database as db_mod
        with mock.patch.object(db_mod, "_is_configured", return_value=True), \
                mock.patch.object(db_mod, "_session") as m:
            session = m.return_value
            resp = session.get.return_value
            resp.raise_for_status.return_value = None
            resp.json.return_value = [
                {"user_id": 111, "usage_date": "2026-08-14", "count": 3},
                {"user_id": 222, "usage_date": "2026-08-13", "count": 9},
            ]
            out = db_mod.Database.get_daily_usage("2026-08-14")
            self.assertEqual(out, {111: 3}, "Hanya tanggal yang diminta yang diambil")


class TestFormatAiUsageReport(unittest.TestCase):
    def test_empty_stats(self):
        report = format_ai_usage_report({"usage": {}})
        self.assertIn("LAPORAN PEMAKAIAN AI", report)
        self.assertIn("0", report)

    def test_tokens_and_providers(self):
        stats = {
            "total_requests": 10,
            "successful": 9,
            "failed": 1,
            "available_providers": ["groq"],
            "provider_names": {"groq": "Groq"},
            "provider_usage": {"groq": 5},
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "by_provider": {"groq": {"prompt_tokens": 1000, "completion_tokens": 500}},
            },
        }
        report = format_ai_usage_report(stats)
        self.assertIn("1,500", report)
        self.assertIn("input 1,000", report)
        self.assertIn("output 500", report)
        self.assertIn("Groq", report)
        self.assertIn("9 sukses", report)
        self.assertIn("1 gagal", report)

    def test_degraded_providers_shown(self):
        stats = {"usage": {}, "degraded_providers": ["openrouter"]}
        report = format_ai_usage_report(stats)
        self.assertIn("openrouter", report)


if __name__ == "__main__":
    unittest.main()
