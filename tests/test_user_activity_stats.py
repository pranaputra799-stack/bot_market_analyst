"""Unit tests untuk pelacakan aktivitas user (flush batch ke Supabase)
dan perintah admin /stats (pemakaian token AI + data Supabase)."""

import asyncio
import unittest
from unittest import mock

from bot import handlers as handlers_mod
from bot.handlers import MarketBot


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _FakeUpdate:
    def __init__(self, text, user_id):
        self.message = _FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()


class _FakeContext:
    def __init__(self):
        self.bot_data = {}


class TestUserActivityFlush(unittest.TestCase):
    def test_flush_empty_is_noop(self):
        bot = MarketBot.__new__(MarketBot)
        bot._user_activity = {}
        with mock.patch.object(
            handlers_mod.db, "update_user_activity_async", new=mock.AsyncMock()
        ) as m:
            asyncio.run(bot.flush_user_activity())
        m.assert_not_awaited()

    def test_flush_sends_rows_and_clears(self):
        bot = MarketBot.__new__(MarketBot)
        bot._user_activity = {111: 3, 222: 1}
        with mock.patch.object(
            handlers_mod.db, "update_user_activity_async", new=mock.AsyncMock(return_value=True)
        ) as m:
            asyncio.run(bot.flush_user_activity())
        m.assert_awaited_once()
        rows = m.await_args.args[0]
        self.assertEqual(len(rows), 2)
        counts = {uid: count for uid, _t, count in rows}
        self.assertEqual(counts, {111: 3, 222: 1})
        self.assertEqual(bot._user_activity, {}, "Buffer harus dikosongkan setelah flush")

    def test_flush_failure_restores_counts(self):
        bot = MarketBot.__new__(MarketBot)
        bot._user_activity = {111: 2}
        with mock.patch.object(
            handlers_mod.db, "update_user_activity_async", new=mock.AsyncMock(return_value=False)
        ):
            asyncio.run(bot.flush_user_activity())
        self.assertEqual(bot._user_activity, {111: 2}, "Hitungan dipulihkan bila gagal")


class TestStatsAdmin(unittest.TestCase):
    ADMIN_IDS = [1, 2]

    def _run(self, text, user_id=1):
        bot = MarketBot.__new__(MarketBot)
        bot.start_time = 1000.0
        bot.total_questions = 42
        bot._user_activity = {}
        bot.news_preds = mock.MagicMock()
        bot.news_preds.get_stats.return_value = {
            "total": 10, "settled": 8, "pending": 2,
            "benar": 6, "salah": 2, "flat": 0, "win_rate": 75.0,
        }
        bot.ai = mock.MagicMock()
        bot.ai.get_stats.return_value = {
            "usage": {
                "total_tokens": 5000,
                "prompt_tokens": 3000,
                "completion_tokens": 2000,
                "by_provider": {
                    "openrouter": {"prompt_tokens": 3000, "completion_tokens": 2000},
                },
            },
        }
        ctx = _FakeContext()
        upd = _FakeUpdate(text, user_id)
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", self.ADMIN_IDS), \
             mock.patch.object(handlers_mod.db, "get_user_stats", return_value={
                 "total_users": 50, "active_24h": 12, "total_questions": 1200,
             }), \
             mock.patch.object(handlers_mod.db, "get_counts", return_value={
                 "subscribers": 30, "event_alert_subscribers": 15,
             }), \
             mock.patch.object(handlers_mod.db, "is_connected", return_value=True), \
             mock.patch.object(handlers_mod.cache, "get_stats", return_value={
                 "active_entries": 123,
             }):
            asyncio.run(bot.stats_command(upd, ctx))
        return upd.message.replies

    def test_non_admin_denied(self):
        replies = self._run("/stats", user_id=99)
        self.assertTrue(replies)
        self.assertIn("khusus admin", replies[0][0])

    def test_shows_token_usage(self):
        replies = self._run("/stats")
        self.assertTrue(replies)
        text = replies[0][0]
        self.assertIn("STATISTIK SISTEM", text)
        self.assertIn("5,000", text)          # total token
        self.assertIn("openrouter", text)
        self.assertIn("3,000", text)          # prompt tokens

    def test_shows_supabase_data(self):
        replies = self._run("/stats")
        text = replies[0][0]
        self.assertIn("User terdaftar: 50", text)
        self.assertIn("User aktif (24 jam): 12", text)
        self.assertIn("Subscriber morning brief: 30", text)
        self.assertIn("Subscriber alert event: 15", text)

    def test_shows_news_prediction_win_rate(self):
        replies = self._run("/stats")
        text = replies[0][0]
        self.assertIn("Win rate: 75.0%", text)


if __name__ == "__main__":
    unittest.main()
