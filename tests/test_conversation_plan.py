"""Unit tests untuk alur tanya-jawab /plan setup (ConversationHandler).

Tanpa network: bot (MarketBot) & database di-mock. Memverifikasi:
- Entry point /plan membuka percakapan untuk 'setup' (state ASK_MODAL).
- Alur lengkap modal → risiko → gaya (tombol) → pair → jam → simpan profil.
- Tombol 'Lewati' (callback query) menyelesaikan setup tanpa jam trading.
- /cancel membatalkan tanpa menyimpan.
- Struktur ConversationHandler (entry/states/fallbacks) benar.
"""

import asyncio
import unittest
from unittest import mock

from bot import conversation_plan as conv
from bot.commands_plan import PlanCommandsMixin
from bot.handlers import MarketBot


class _FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _FakeCallbackQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


class _FakeUpdate:
    """Update tiruan: mendukung message ATAU callback_query."""

    def __init__(self, text=None, callback_data=None, user_id=9999, chat_id=777):
        self.message = _FakeMessage(text) if text is not None else None
        self.callback_query = (
            _FakeCallbackQuery(callback_data, self.message or _FakeMessage())
            if callback_data is not None
            else None
        )
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": chat_id})()

    @property
    def effective_message(self):
        return self.message if self.message is not None else self.callback_query.message


class _FakeContext:
    def __init__(self):
        self.user_data = {}
        self.bot_data = {}
        self.bot = mock.MagicMock()


class TestConversationPlan(unittest.TestCase):
    def _conversation_bot(self):
        """MarketBot dengan method plan yang di-stub — tanpa network/AI."""
        bot = MarketBot.__new__(MarketBot)
        bot.plan_command = mock.AsyncMock()
        bot.plan_conversation_entry = PlanCommandsMixin.plan_conversation_entry.__get__(bot, MarketBot)
        bot._plan_setup = mock.AsyncMock()
        bot._plan_generate = mock.AsyncMock()
        return bot

    def test_entry_starts_conversation_on_setup(self):
        bot = self._conversation_bot()
        upd = _FakeUpdate("/plan setup")
        ctx = _FakeContext()
        state = asyncio.run(bot.plan_conversation_entry(upd, ctx))
        self.assertEqual(state, conv.ASK_MODAL)
        self.assertTrue(upd.message.replies)
        self.assertIn("modal", upd.message.replies[0][0].lower())
        self.assertIn("1/5", upd.message.replies[0][0])

    def test_entry_one_shot_setup_still_works(self):
        bot = self._conversation_bot()
        upd = _FakeUpdate("/plan setup 1000 2 swing XAU/USD")
        ctx = _FakeContext()
        state = asyncio.run(bot.plan_conversation_entry(upd, ctx))
        self.assertEqual(state, conv.ConversationHandler.END)
        bot._plan_setup.assert_awaited_once()

    def test_entry_generate_without_args(self):
        bot = self._conversation_bot()
        upd = _FakeUpdate("/plan")
        ctx = _FakeContext()
        state = asyncio.run(bot.plan_conversation_entry(upd, ctx))
        self.assertEqual(state, conv.ConversationHandler.END)
        bot._plan_generate.assert_awaited_once()

    def test_entry_help_and_clear(self):
        bot = self._conversation_bot()
        ctx = _FakeContext()
        upd_help = _FakeUpdate("/plan help")
        self.assertEqual(asyncio.run(bot.plan_conversation_entry(upd_help, ctx)), conv.ConversationHandler.END)
        self.assertIn("TRADING PLAN", upd_help.message.replies[0][0])
        upd_clear = _FakeUpdate("/plan clear")
        with mock.patch.object(conv.db, "delete_user_profile_async", new=mock.AsyncMock(return_value=True)):
            self.assertEqual(
                asyncio.run(bot.plan_conversation_entry(upd_clear, ctx)), conv.ConversationHandler.END
            )
        self.assertIn("dihapus", upd_clear.message.replies[0][0])

    def test_full_conversation_flow(self):
        """Modal → risiko → gaya → pair → jam → tersimpan (profil lengkap)."""
        ctx = _FakeContext()
        with mock.patch.object(conv.db, "upsert_user_profile_async", new=mock.AsyncMock(return_value=True)) as m:
            upd1 = _FakeUpdate("1000")
            self.assertEqual(asyncio.run(conv._ask_modal(upd1, ctx)), conv.ASK_RISK)
            self.assertEqual(ctx.user_data["plan_setup_profile"]["balance"], 1000.0)

            upd2 = _FakeUpdate("2")
            self.assertEqual(asyncio.run(conv._ask_risk(upd2, ctx)), conv.ASK_STYLE)
            # Keyboard gaya trading harus muncul
            _, kwargs = upd2.message.replies[0]
            kb = kwargs.get("reply_markup")
            self.assertIsNotNone(kb)
            data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
            self.assertIn("plan_style:swing", data)

            upd3 = _FakeUpdate(callback_data="plan_style:swing")
            self.assertEqual(asyncio.run(conv._ask_style(upd3, ctx)), conv.ASK_PAIRS)
            self.assertTrue(upd3.callback_query.answered)

            upd4 = _FakeUpdate("XAU/USD, EUR/USD")
            self.assertEqual(asyncio.run(conv._ask_pairs(upd4, ctx)), conv.ASK_HOURS)

            upd5 = _FakeUpdate("09:00-16:00")
            self.assertEqual(asyncio.run(conv._ask_hours(upd5, ctx)), conv.ConversationHandler.END)
            self.assertTrue(upd5.message.replies)
            self.assertIn("tersimpan", upd5.message.replies[0][0])

        m.assert_awaited_once()
        profile = m.await_args.args[1]
        self.assertEqual(profile["balance"], 1000.0)
        self.assertEqual(profile["risk_per_trade"], 2.0)
        self.assertEqual(profile["trading_style"], "swing")
        self.assertIn("XAU/USD", profile["favorite_pairs"])
        self.assertEqual(profile["trading_hours"], "09:00-16:00")
        self.assertNotIn("plan_setup_profile", ctx.user_data)

    def test_skip_hours_via_callback(self):
        """Tombol 'Lewati' (callback query) → jam kosong, profil tetap tersimpan.

        Regression: dulu _finish_setup memakai update.message yang None pada
        callback query → AttributeError; sekarang update.effective_message.
        """
        ctx = _FakeContext()
        ctx.user_data["plan_setup_profile"] = {
            "balance": 500.0,
            "risk_per_trade": 1.0,
            "trading_style": "day_trade",
            "favorite_pairs": "EUR/USD",
        }
        with mock.patch.object(conv.db, "upsert_user_profile_async", new=mock.AsyncMock(return_value=True)) as m:
            upd = _FakeUpdate(callback_data="plan_hours_skip")
            state = asyncio.run(conv._skip_hours(upd, ctx))
        self.assertEqual(state, conv.ConversationHandler.END)
        self.assertTrue(upd.callback_query.answered)
        m.assert_awaited_once()
        profile = m.await_args.args[1]
        self.assertEqual(profile["trading_hours"], "")
        self.assertTrue(upd.callback_query.message.replies)
        self.assertIn("tersimpan", upd.callback_query.message.replies[0][0])

    def test_invalid_input_keeps_state(self):
        ctx = _FakeContext()
        upd = _FakeUpdate("abc")
        self.assertEqual(asyncio.run(conv._ask_modal(upd, ctx)), conv.ASK_MODAL)
        self.assertIn("Coba lagi", upd.message.replies[0][0])

        upd2 = _FakeUpdate("200")
        self.assertEqual(asyncio.run(conv._ask_risk(upd2, ctx)), conv.ASK_RISK)
        self.assertIn("Coba lagi", upd2.message.replies[0][0])

    def test_cancel_clears_partial_profile(self):
        ctx = _FakeContext()
        ctx.user_data["plan_setup_profile"] = {"balance": 1000.0}
        upd = _FakeUpdate("/cancel")
        state = asyncio.run(conv._cancel(upd, ctx))
        self.assertEqual(state, conv.ConversationHandler.END)
        self.assertNotIn("plan_setup_profile", ctx.user_data)
        self.assertIn("dibatalkan", upd.message.replies[0][0])

    def test_builder_wiring(self):
        entry = mock.AsyncMock()
        ch = conv.build_plan_setup_conversation(entry)
        self.assertEqual(ch.name, "plan_setup")
        # Entry point: perintah /plan
        self.assertEqual(len(ch.entry_points), 1)
        self.assertIn("plan", ch.entry_points[0].commands)
        # Semua state terpasang
        for state in (conv.ASK_MODAL, conv.ASK_RISK, conv.ASK_STYLE, conv.ASK_PAIRS, conv.ASK_HOURS):
            self.assertIn(state, ch.states, f"state {state} tidak terdaftar")
        # Fallback /cancel & /batal
        cmds = {next(iter(h.commands)) for h in ch.fallbacks}
        self.assertIn("cancel", cmds)
        self.assertIn("batal", cmds)


if __name__ == "__main__":
    unittest.main()
