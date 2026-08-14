"""Unit tests untuk rate limit command AI-heavy dan batas _user_activity.

Rate limit melindungi provider free tier & RAM dari spam command yang
menjalankan pipeline AI (/morning, /aftermath, /sentiment, ...). Batas
_user_activity mencegah dict membengkak tanpa batas bila Supabase tidak
dikonfigurasi (flush gagal → hitungan dikembalikan ke memori).
"""

import asyncio
import unittest

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
        self.user_data = {}


class TestCommandRateLimit(unittest.TestCase):
    def _run(self, update, context):
        bot = MarketBot.__new__(MarketBot)
        return asyncio.run(bot._check_command_rate_limit(update, context))

    def test_first_call_allowed(self):
        upd = _FakeUpdate("/morning", 111)
        ctx = _FakeContext()
        self.assertTrue(self._run(upd, ctx))
        self.assertIn("last_ai_command_time", ctx.user_data)

    def test_immediate_repeat_blocked_with_message(self):
        upd = _FakeUpdate("/morning", 111)
        ctx = _FakeContext()
        self.assertTrue(self._run(upd, ctx))
        # Panggilan kedua dalam jendela 15 detik → ditolak + user dikabari
        self.assertFalse(self._run(upd, ctx))
        self.assertTrue(upd.message.replies)
        self.assertIn("tunggu", upd.message.replies[0][0].lower())

    def test_rate_limit_is_per_user(self):
        # User 111 keburu rate-limit, user 222 tidak terpengaruh
        ctx_a = _FakeContext()
        upd_a = _FakeUpdate("/morning", 111)
        self.assertTrue(self._run(upd_a, ctx_a))
        self.assertFalse(self._run(upd_a, ctx_a))

        upd_b = _FakeUpdate("/morning", 222)
        ctx_b = _FakeContext()
        self.assertTrue(self._run(upd_b, ctx_b))

    def test_allowed_after_interval(self):
        upd = _FakeUpdate("/morning", 111)
        ctx = _FakeContext()
        self.assertTrue(self._run(upd, ctx))
        self.assertFalse(self._run(upd, ctx))
        # Lewati jendela 15 detik → boleh lagi
        ctx.user_data["last_ai_command_time"] = ctx.user_data["last_ai_command_time"] - 20
        self.assertTrue(self._run(upd, ctx))


class TestUserActivityCap(unittest.TestCase):
    def _bot_with_cap(self, cap):
        bot = MarketBot.__new__(MarketBot)
        bot._MAX_USER_ACTIVITY_ENTRIES = cap
        bot._user_activity = {}
        return bot

    def test_increments_existing_user(self):
        bot = self._bot_with_cap(10)
        bot._track_user_activity(111)
        bot._track_user_activity(111)
        self.assertEqual(bot._user_activity, {111: 2})

    def test_evicts_oldest_when_full(self):
        bot = self._bot_with_cap(3)
        bot._track_user_activity(1)
        bot._track_user_activity(2)
        bot._track_user_activity(3)
        # Penuh — user ke-4 memaksa evict user terlama (1)
        bot._track_user_activity(4)
        self.assertNotIn(1, bot._user_activity)
        self.assertEqual(bot._user_activity.get(4), 1)
        self.assertEqual(len(bot._user_activity), 3)

    def test_never_exceeds_cap(self):
        bot = self._bot_with_cap(5)
        for uid in range(100):
            bot._track_user_activity(uid)
        self.assertLessEqual(len(bot._user_activity), 5)


if __name__ == "__main__":
    unittest.main()
