"""Unit tests untuk perintah admin /broadcast (khusus ADMIN_USER_IDS)."""

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


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class _FakeContext:
    def __init__(self, bot, bot_data):
        self.bot = bot
        self.bot_data = bot_data


class TestBroadcastAdmin(unittest.TestCase):
    ADMIN_IDS = [1, 2]

    def _run(
        self,
        text,
        user_id=1,
        db_subscribers=(),
        event_subscribers=(),
    ):
        bot = MarketBot.__new__(MarketBot)
        fake_bot = _FakeBot()
        ctx = _FakeContext(fake_bot, {"event_alert_subscribers": set(event_subscribers)})
        upd = _FakeUpdate(text, user_id)
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", self.ADMIN_IDS), \
             mock.patch.object(
                 handlers_mod.db,
                 "get_all_subscribers_async",
                 new=mock.AsyncMock(return_value=list(db_subscribers)),
             ):
            asyncio.run(bot.broadcast_command(upd, ctx))
        return upd.message.replies, fake_bot.sent

    def test_non_admin_denied(self):
        replies, _ = self._run("broadcast halo", user_id=99)
        self.assertTrue(replies)
        self.assertIn("khusus admin", replies[0][0])

    def test_no_args_shows_usage(self):
        replies, _ = self._run("/broadcast")
        self.assertTrue(replies)
        self.assertIn("BROADCAST", replies[0][0])

    def test_preview_shows_recipient_count_no_send(self):
        # Subscriber DB {777, 888} + event {777} → unik {777, 888} = 2 penerima
        replies, sent = self._run(
            "/broadcast halo semua",
            db_subscribers=[777, 888],
            event_subscribers=[777],
        )
        self.assertEqual(sent, [], "Preview TIDAK boleh mengirim pesan")
        self.assertIn("2", replies[0][0])
        self.assertIn("PRATINJAU", replies[0][0])

    def test_send_confirms_and_sends_to_each(self):
        replies, sent = self._run(
            "/broadcast send halo semua",
            db_subscribers=[777, 888],
            event_subscribers=[777],
        )
        # Unik {777, 888} → 2 pesan terkirim
        self.assertEqual({c for c, _ in sent}, {777, 888})
        self.assertTrue(all("PENGUMUMAN" in t for _, t in sent))
        self.assertIn("terkirim: 2", replies[-1][0])

    def test_no_subscribers(self):
        replies, sent = self._run("/broadcast send halo", db_subscribers=[])
        self.assertEqual(sent, [])
        self.assertIn("Belum ada subscriber", replies[0][0])

    def test_admin_excluded_from_recipients(self):
        # Admin (id 1) juga subscriber → tidak perlu menerima pesannya sendiri
        _, sent = self._run(
            "/broadcast send halo",
            db_subscribers=[1, 777],
        )
        self.assertEqual([c for c, _ in sent], [777])
