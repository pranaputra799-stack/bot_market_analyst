"""Unit tests untuk perintah /watchlist — tanpa network (DB & market di-mock)."""

import asyncio
import unittest
from unittest import mock

from bot import handlers as handlers_mod
from bot.handlers import MarketBot
from utils.chart_generator import ChartGenerator


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _FakeUpdate:
    def __init__(self, text, user_id=9999):
        self.message = _FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type(
            "C", (), {"id": 777, "send_chat_action": mock.AsyncMock()}
        )()


class _FakeContext:
    def __init__(self):
        self.bot_data = {}
        self.bot = mock.MagicMock()


def _make_bot():
    bot = MarketBot.__new__(MarketBot)
    bot.chart = ChartGenerator
    bot.market = mock.MagicMock()
    return bot


class TestWatchlistAdd(unittest.TestCase):
    def test_add_known_symbol(self):
        bot = _make_bot()
        upd = _FakeUpdate("/watchlist add gold")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=[])), \
             mock.patch.object(handlers_mod.db, "add_watchlist_symbol_async", new=mock.AsyncMock(return_value=True)) as m:
            asyncio.run(bot.watchlist_command(upd, ctx))
        m.assert_awaited_once_with(9999, "XAU/USD (Gold)")
        self.assertTrue(upd.message.replies)
        self.assertIn("XAU/USD (Gold)", upd.message.replies[0][0])

    def test_add_duplicate_rejected(self):
        bot = _make_bot()
        upd = _FakeUpdate("/watchlist add eurusd")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=["EUR/USD"])), \
             mock.patch.object(handlers_mod.db, "add_watchlist_symbol_async", new=mock.AsyncMock()) as m:
            asyncio.run(bot.watchlist_command(upd, ctx))
        m.assert_not_awaited()
        self.assertIn("sudah ada", upd.message.replies[0][0].lower())

    def test_add_unknown_symbol(self):
        bot = _make_bot()
        upd = _FakeUpdate("/watchlist add zzzz")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=[])), \
             mock.patch.object(handlers_mod.db, "add_watchlist_symbol_async", new=mock.AsyncMock()) as m:
            asyncio.run(bot.watchlist_command(upd, ctx))
        m.assert_not_awaited()
        self.assertIn("tidak dikenali", upd.message.replies[0][0])

    def test_add_without_argument_shows_usage(self):
        bot = _make_bot()
        upd = _FakeUpdate("/watchlist add")
        ctx = _FakeContext()
        asyncio.run(bot.watchlist_command(upd, ctx))
        self.assertIn("Format", upd.message.replies[0][0])

    def test_add_full_watchlist(self):
        bot = _make_bot()
        bot.WATCHLIST_MAX = 1
        upd = _FakeUpdate("/watchlist add gbpusd")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=["EUR/USD"])) as m:
            asyncio.run(bot.watchlist_command(upd, ctx))
        m.assert_awaited_once_with(9999)
        self.assertIn("penuh", upd.message.replies[0][0].lower())


class TestWatchlistRemove(unittest.TestCase):
    def test_remove_existing(self):
        bot = _make_bot()
        upd = _FakeUpdate("/watchlist remove gold")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=["XAU/USD (Gold)"])), \
             mock.patch.object(handlers_mod.db, "remove_watchlist_symbol_async", new=mock.AsyncMock(return_value=True)) as m:
            asyncio.run(bot.watchlist_command(upd, ctx))
        m.assert_awaited_once_with(9999, "XAU/USD (Gold)")
        self.assertIn("dihapus", upd.message.replies[0][0].lower())

    def test_remove_missing(self):
        bot = _make_bot()
        upd = _FakeUpdate("/watchlist remove xyz")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=["EUR/USD"])), \
             mock.patch.object(handlers_mod.db, "remove_watchlist_symbol_async", new=mock.AsyncMock()) as m:
            asyncio.run(bot.watchlist_command(upd, ctx))
        m.assert_not_awaited()
        self.assertIn("tidak ada", upd.message.replies[0][0])


class TestWatchlistShow(unittest.TestCase):
    def test_empty_watchlist_hints(self):
        bot = _make_bot()
        upd = _FakeUpdate("/watchlist")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=[])):
            asyncio.run(bot.watchlist_command(upd, ctx))
        self.assertIn("masih kosong", upd.message.replies[0][0].lower())

    def test_show_renders_rows(self):
        bot = _make_bot()
        ind = {"current_price": 2400.5, "price_5d_change": 1.23, "rsi": 62.4, "ema_20": 2390.0, "ema_50": 2380.0}
        bot.market.get_ohlcv_history = mock.MagicMock(return_value=[{"close": 2400.5}])
        upd = _FakeUpdate("/watchlist")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=["XAU/USD (Gold)"])), \
             mock.patch("bot.commands_watchlist.compute_indicators", return_value=ind):
            asyncio.run(bot.watchlist_command(upd, ctx))
        self.assertTrue(upd.message.replies)
        text = upd.message.replies[0][0]
        self.assertIn("WATCHLIST", text)
        self.assertIn("XAU/USD (Gold)", text)
        self.assertIn("2,400.5", text)


class TestWatchlistSettingsMenu(unittest.TestCase):
    def test_settings_menu_has_watchlist_button(self):
        bot = _make_bot()
        upd = _FakeUpdate("/settings")
        ctx = _FakeContext()
        with mock.patch.object(handlers_mod.db, "is_subscribed_async", new=mock.AsyncMock(return_value=False)), \
             mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=["EUR/USD"])):
            asyncio.run(bot.settings_command(upd, ctx))
        _, kwargs = upd.message.replies[0]
        kb = kwargs.get("reply_markup")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("settings_watchlist", callbacks)


if __name__ == "__main__":
    unittest.main()
