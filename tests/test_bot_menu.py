"""Unit tests untuk sinkronisasi menu perintah (utils/bot_menu.py + /syncmenu)."""

import asyncio
import unittest
from unittest import mock

from utils import bot_menu as menu_mod
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
    def __init__(self, bot=None):
        self.bot = bot or mock.MagicMock()
        self.user_data = {}


class TestSetBotCommands(unittest.TestCase):
    def _bot(self):
        bot = mock.MagicMock()
        bot.set_my_commands = mock.AsyncMock()
        return bot

    def test_success_sets_default_and_clears_scopes(self):
        bot = self._bot()
        ok = asyncio.run(menu_mod.set_bot_commands(bot, attempts=2))
        self.assertTrue(ok)
        self.assertEqual(bot.set_my_commands.await_count, 3)
        # 1x default scope (daftar), 2x scope kosong (group + admin)
        scopes = [c.kwargs.get("scope") for c in bot.set_my_commands.await_args_list]
        self.assertEqual(scopes, [None, mock.ANY, mock.ANY])
        cmds = bot.set_my_commands.await_args_list[0].args[0]
        names = {c.command for c in cmds}
        self.assertIn("start", names)
        self.assertIn("journal", names)
        # Command mati tidak boleh ada
        self.assertNotIn("pa", names)
        self.assertNotIn("chart", names)
        self.assertNotIn("watch", names)

    def test_retries_then_succeeds(self):
        bot = self._bot()
        bot.set_my_commands.side_effect = [
            RuntimeError("Telegram API 500 (transient)"),
            None,  # default scope sukses
            None,  # group scope
            None,  # admin scope
        ]
        with mock.patch.object(menu_mod.asyncio, "sleep", new=mock.AsyncMock()):
            ok = asyncio.run(menu_mod.set_bot_commands(bot, attempts=3))
        self.assertTrue(ok)
        self.assertEqual(bot.set_my_commands.await_count, 4)

    def test_total_failure_returns_false(self):
        bot = self._bot()
        bot.set_my_commands.side_effect = RuntimeError("API down")
        with mock.patch.object(menu_mod.asyncio, "sleep", new=mock.AsyncMock()):
            ok = asyncio.run(menu_mod.set_bot_commands(bot, attempts=2))
        self.assertFalse(ok)
        self.assertEqual(bot.set_my_commands.await_count, 2)


class TestSyncMenuCommand(unittest.TestCase):
    ADMIN_IDS = [1, 2]

    def test_non_admin_denied(self):
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/syncmenu", 99)
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", self.ADMIN_IDS):
            asyncio.run(bot.syncmenu_command(upd, ctx))
        self.assertTrue(upd.message.replies)
        self.assertIn("admin", upd.message.replies[0][0].lower())
        self.assertFalse(ctx.bot.set_my_commands.called)

    def test_admin_syncs_menu(self):
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/syncmenu", 1)
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", self.ADMIN_IDS), \
                mock.patch(
                    "utils.bot_menu.set_bot_commands", new=mock.AsyncMock(return_value=True)
                ) as m:
            asyncio.run(bot.syncmenu_command(upd, ctx))
        m.assert_awaited_once()
        self.assertIn("disinkronkan", upd.message.replies[0][0])


if __name__ == "__main__":
    unittest.main()
