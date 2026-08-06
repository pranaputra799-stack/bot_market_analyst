"""Unit tests untuk /clear dan tombol aksi cepat (qa:*) — TANPA network.

Menggunakan fake update/query/message sehingga tidak ada panggilan Telegram
atau jaringan keluar. Market di-fake untuk qa:sr (data OHLCV sintetis).
"""

import asyncio
import unittest

from bot.handlers import MarketBot
from data import conversation_memory as cm


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return None


class FakeUpdate:
    def __init__(self, user_id=9999, message=None, callback_query=None):
        class _User:
            id = user_id

        self.effective_user = _User()
        self.message = message
        self.callback_query = callback_query


class FakeQuery:
    def __init__(self, user_id, data, message=None):
        class _User:
            id = user_id

        self.data = data
        self.from_user = _User()
        self.message = message or FakeMessage()
        self.answered = False
        self.markup_removed = False

    async def answer(self):
        self.answered = True

    async def edit_message_reply_markup(self, markup):
        self.markup_removed = True


class TestClearCommand(unittest.TestCase):
    def setUp(self):
        cm.clear(9999)

    def tearDown(self):
        cm.clear(9999)

    def test_clear_command_wipes_memory_and_replies(self):
        cm.add_exchange(9999, "analisis eurusd", "bias bullish")
        self.assertNotEqual(cm.get_context(9999), {})

        bot = MarketBot.__new__(MarketBot)
        msg = FakeMessage()
        update = FakeUpdate(user_id=9999, message=msg)

        asyncio.run(bot.clear_command(update, None))

        self.assertEqual(cm.get_context(9999), {})
        self.assertEqual(cm.get_history(9999), [])
        self.assertTrue(any("dibersihkan" in t for t, _ in msg.replies))


class TestQuickActionClear(unittest.TestCase):
    def setUp(self):
        cm.clear(9999)

    def tearDown(self):
        cm.clear(9999)

    def test_qa_clear_removes_context_and_buttons(self):
        cm.add_exchange(9999, "analisis eurusd", "bias bullish")

        bot = MarketBot.__new__(MarketBot)
        msg = FakeMessage()
        query = FakeQuery(user_id=9999, data="qa:clear", message=msg)
        update = FakeUpdate(callback_query=query)

        asyncio.run(bot.handle_callback(update, None))

        self.assertTrue(query.answered)
        self.assertTrue(query.markup_removed)
        self.assertEqual(cm.get_context(9999), {})
        self.assertTrue(any("dibersihkan" in t for t, _ in msg.replies))


class TestQuickActionNoContext(unittest.TestCase):
    def setUp(self):
        cm.clear(9999)

    def tearDown(self):
        cm.clear(9999)

    def test_qa_without_asset_asks_for_instrument(self):
        bot = MarketBot.__new__(MarketBot)
        msg = FakeMessage()
        query = FakeQuery(user_id=9999, data="qa:sr", message=msg)
        update = FakeUpdate(callback_query=query)

        asyncio.run(bot.handle_callback(update, None))

        joined = "\n".join(t for t, _ in msg.replies)
        self.assertIn("Belum ada konteks aset", joined)


class TestQuickActionSr(unittest.TestCase):
    def setUp(self):
        cm.clear(9999)
        cm.set_context(9999, asset_focus="EUR/USD")

    def tearDown(self):
        cm.clear(9999)

    def test_qa_sr_builds_levels_with_fake_market(self):
        class FakeMarket:
            @staticmethod
            def get_ohlcv_history(symbol, period, interval, limit):
                # 60 bar uptrend sintetis — cukup untuk pivot, fib, RSI, EMA
                bars = []
                for i in range(60):
                    c = 1.0800 + i * 0.0002
                    bars.append({
                        "date": f"2026-08-{i % 28 + 1:02d}",
                        "open": c - 0.0001,
                        "high": c + 0.0003,
                        "low": c - 0.0003,
                        "close": c,
                    })
                return bars

        bot = MarketBot.__new__(MarketBot)
        bot.market = FakeMarket()
        msg = FakeMessage()
        query = FakeQuery(user_id=9999, data="qa:sr", message=msg)
        update = FakeUpdate(callback_query=query)

        asyncio.run(bot.handle_callback(update, None))

        self.assertTrue(query.answered)
        joined = "\n".join(t for t, _ in msg.replies)
        self.assertIn("LEVEL KUNCI", joined)
        self.assertIn("EUR/USD", joined)


class TestQuickActionEmbeddedSymbol(unittest.TestCase):
    """Tombol lama dengan simbol ter-embed harus tetap pakai simbol tombol,
    bukan konteks terakhir (regresi untuk tombol yang tersisa di pesan lama)."""

    def setUp(self):
        cm.clear(9999)
        # Konteks terakhir = XAU (berbeda dari simbol di tombol lama)
        cm.set_context(9999, asset_focus="XAU/USD (Gold)")

    def tearDown(self):
        cm.clear(9999)

    def test_qa_sr_with_embedded_symbol_uses_button_symbol(self):
        class FakeMarket:
            @staticmethod
            def get_ohlcv_history(symbol, period, interval, limit):
                bars = []
                for i in range(60):
                    c = 1.0800 + i * 0.0002
                    bars.append({
                        "date": f"2026-08-{i % 28 + 1:02d}",
                        "open": c - 0.0001,
                        "high": c + 0.0003,
                        "low": c - 0.0003,
                        "close": c,
                    })
                return bars

        bot = MarketBot.__new__(MarketBot)
        bot.market = FakeMarket()
        msg = FakeMessage()
        # Tombol lama dari pesan EUR/USD
        query = FakeQuery(user_id=9999, data="qa:sr:EURUSD=X", message=msg)
        update = FakeUpdate(callback_query=query)

        asyncio.run(bot.handle_callback(update, None))

        joined = "\n".join(t for t, _ in msg.replies)
        self.assertIn("EUR/USD", joined)  # pakai simbol tombol, bukan konteks XAU
        self.assertNotIn("XAU", joined)


if __name__ == "__main__":
    unittest.main()
