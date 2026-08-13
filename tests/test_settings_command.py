"""Unit tests untuk perintah /settings — membuka menu Pengaturan (⚙️).

Sama seperti tombol menu: menampilkan status alert event / morning brief /
konteks + tombol toggle inline. Tanpa network (DB di-mock).
"""

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
    def __init__(self, text, user_id=9999, chat_id=777):
        self.message = _FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id, "username": "t", "first_name": "T"})()
        self.effective_chat = type("C", (), {"id": chat_id})()


class _FakeContext:
    def __init__(self):
        self.bot_data = {}
        self.bot = None


class TestSettingsCommand(unittest.TestCase):
    def _run(self, text="/settings", subscribed=True):
        bot = MarketBot.__new__(MarketBot)
        bot.news_preds = mock.MagicMock()
        upd = _FakeUpdate(text)
        ctx = _FakeContext()
        with mock.patch.object(
            handlers_mod.db, "is_subscribed_async", new=mock.AsyncMock(return_value=subscribed)
        ):
            asyncio.run(bot.settings_command(upd, ctx))
        return upd.message.replies

    def test_settings_replies_with_menu(self):
        replies = self._run()
        self.assertTrue(replies)
        text, kwargs = replies[0]
        self.assertIn("PENGATURAN", text)
        self.assertIn("Alert Event", text)
        self.assertIn("Morning Brief", text)

    def test_settings_shows_toggle_buttons(self):
        replies = self._run()
        _, kwargs = replies[0]
        kb = kwargs.get("reply_markup")
        self.assertIsNotNone(kb, "Menu pengaturan harus punya keyboard inline")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("settings_alert", callbacks)
        self.assertIn("settings_brief", callbacks)
        self.assertIn("settings_clear", callbacks)
        self.assertIn("menu", callbacks)

    def test_settings_reflects_current_status(self):
        # Subscribed=True → label tombol brief menunjukkan "Berhenti"
        replies = self._run(subscribed=True)
        _, kwargs = replies[0]
        kb = kwargs.get("reply_markup")
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("Berhenti" in t for t in texts), f"Tombol brief: {texts}")
        # Subscribed=False → label tombol brief menunjukkan "Langganan"
        replies = self._run(subscribed=False)
        _, kwargs = replies[0]
        kb = kwargs.get("reply_markup")
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("Langganan" in t for t in texts), f"Tombol brief: {texts}")


if __name__ == "__main__":
    unittest.main()
