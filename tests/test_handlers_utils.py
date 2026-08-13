"""Unit tests untuk utilitas di bot/handlers.py (split_long_text, strip asterisks)."""

import asyncio
import unittest
from unittest import mock

from bot import handlers as handlers_mod
from bot.handlers import (
    MarketBot,
    split_long_text,
    strip_markdown_asterisks,
    label_to_symbol,
    _quick_action_keyboard,
    _menu_reply_keyboard,
    MENU_KEYBOARD_ACTIONS,
    MENU_KEYBOARD_LABELS,
    TG_MAX_MESSAGE_CHARS,
)


class TestSplitLongText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_long_text("Halo dunia"), ["Halo dunia"])

    def test_long_paragraph_split(self):
        text = "paragraf " + "x" * 5000
        chunks = split_long_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), TG_MAX_MESSAGE_CHARS)
        # Konten harus tetap utuh setelah digabung kembali
        self.assertEqual("".join(chunks), text)

    def test_empty_and_whitespace(self):
        self.assertEqual(split_long_text(""), [""])
        self.assertEqual([c for c in split_long_text("") if c], [])

    def test_multiple_paragraphs_preserved(self):
        text = "\n\n".join(["A" * 3000, "B" * 3000, "C" * 100])
        chunks = split_long_text(text)
        joined = "".join(chunks)
        self.assertIn("AAA", joined)
        self.assertIn("BBB", joined)
        self.assertIn("CCC", joined)
        for c in chunks:
            self.assertLessEqual(len(c), TG_MAX_MESSAGE_CHARS)


class TestSanitizeText(unittest.TestCase):
    """sanitize_text harus mengganti kontrol char dengan spasi, bukan menghapus
    (agar kata tidak menempel: 'gold\\nanalysis' → 'gold analysis')."""

    def test_newlines_replaced_not_concatenated(self):
        from utils.validators import sanitize_text

        self.assertEqual(
            sanitize_text("kenapa gold naik\napa penyebabnya?"),
            "kenapa gold naik apa penyebabnya?",
        )

    def test_tabs_replaced(self):
        from utils.validators import sanitize_text

        self.assertEqual(sanitize_text("harga\tgold"), "harga gold")

    def test_truncation(self):
        from utils.validators import sanitize_text

        out = sanitize_text("x" * 600)
        self.assertLessEqual(len(out), 500)


class TestLabelToSymbol(unittest.TestCase):
    """Label fokus aset (dari conversation memory) → simbol Yahoo Finance."""

    def test_pair_labels(self):
        self.assertEqual(label_to_symbol("EUR/USD"), "EURUSD=X")
        self.assertEqual(label_to_symbol("USD/JPY"), "USDJPY=X")
        self.assertEqual(label_to_symbol("USD/IDR"), "USDIDR=X")
        self.assertEqual(label_to_symbol("gbp/usd"), "GBPUSD=X")  # case-insensitive

    def test_special_labels(self):
        self.assertEqual(label_to_symbol("XAU/USD (Gold)"), "GC=F")
        self.assertEqual(label_to_symbol("XAG/USD (Silver)"), "SI=F")
        self.assertEqual(label_to_symbol("BTC/USD (Bitcoin)"), "BTC-USD")
        self.assertEqual(label_to_symbol("ETH/USD (Ethereum)"), "ETH-USD")
        self.assertEqual(label_to_symbol("DXY (Dollar Index)"), "DX-Y.NYB")
        self.assertEqual(label_to_symbol("S&P 500"), "^GSPC")
        self.assertEqual(label_to_symbol("NASDAQ"), "^IXIC")
        self.assertEqual(label_to_symbol("VIX"), "^VIX")

    def test_invalid_labels(self):
        self.assertIsNone(label_to_symbol(None))
        self.assertIsNone(label_to_symbol(""))
        self.assertIsNone(label_to_symbol("saham"))


class TestDetectPairs(unittest.TestCase):
    """_detect_pairs harus mengenali pair dengan & tanpa garis miring."""

    def _bot(self):
        return MarketBot.__new__(MarketBot)

    def test_slash_form(self):
        bot = self._bot()
        self.assertEqual(bot._detect_pairs("analisis eur/usd"), [("eur/usd", "EURUSD=X")])

    def test_slashless_form(self):
        bot = self._bot()
        detected = bot._detect_pairs("analisis eurusd")
        self.assertEqual(detected, [("eur/usd", "EURUSD=X")])

    def test_slashless_with_space(self):
        bot = self._bot()
        self.assertEqual(bot._detect_pairs("usd jpy sekarang"), [("usd/jpy", "USDJPY=X")])

    def test_multiple_pairs(self):
        bot = self._bot()
        detected = bot._detect_pairs("bandingkan eurusd dan xauusd")
        symbols = [s for _, s in detected]
        self.assertIn("EURUSD=X", symbols)
        self.assertIn("GC=F", symbols)

    def test_no_pair(self):
        bot = self._bot()
        self.assertEqual(bot._detect_pairs("apa itu inflasi?"), [])


class TestStartMenuKeyboard(unittest.TestCase):
    """Menu /start: semua callback_data tombol harus didukung handle_callback."""

    # Callback yang ditangani handle_callback
    SUPPORTED = {
        "morning", "overview", "gold_price", "eurusd", "macro",
        "calendar", "sentiment", "sentimen_retail", "alert_on",
        "prediksi", "subscribe", "unsubscribe", "help", "settings",
        "settings_alert", "settings_brief", "settings_clear", "menu",
    }

    def _run_start(self):
        # start() membaca user.id/username/first_name → fake user lengkap
        bot = MarketBot.__new__(MarketBot)
        user = type("U", (), {"id": 9999, "username": "tester", "first_name": "Tester"})()
        upd = _FakeUpdate("/start", user_id=9999)
        upd.effective_user = user
        asyncio.run(bot.start(upd, _FakeContext()))
        text, kwargs = upd.message.replies[0]
        return text, kwargs

    def test_welcome_message_rendered(self):
        from bot.messages import WELCOME_MESSAGE

        text, _ = self._run_start()
        self.assertEqual(text, WELCOME_MESSAGE)

    def test_all_buttons_have_supported_callbacks(self):
        _, kwargs = self._run_start()
        kb = kwargs.get("reply_markup")
        self.assertIsNotNone(kb, "Menu /start harus punya inline keyboard")
        for row in kb.inline_keyboard:
            for btn in row:
                cb = btn.callback_data
                supported = (
                    cb in self.SUPPORTED
                    or cb.startswith("qa:")
                )
                self.assertTrue(
                    supported, f"Tombol menu {cb} tidak punya handler callback!"
                )

    def test_menu_has_new_features(self):
        _, kwargs = self._run_start()
        kb = kwargs.get("reply_markup")
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
        ]
        # Fitur notifikasi event harus ada di menu
        self.assertIn("alert_on", callbacks)
        # Fitur baru prediksi news (XAU/USD) harus terlihat di menu
        self.assertIn("prediksi", callbacks)
        # Menu TIDAK boleh menyisakan tombol fitur yang sudah dihapus (chart)
        self.assertFalse(
            any(cb.startswith("chart") for cb in callbacks),
            "Menu masih memuat tombol fitur yang sudah dihapus",
        )


class TestReplyKeyboardMenu(unittest.TestCase):
    """Menu di keyboard bawah (Reply Keyboard): tombol harus ada & labelnya
    terpetakan ke aksi yang sama dengan tombol inline."""

    def test_reply_keyboard_built(self):
        kb = _menu_reply_keyboard()
        rows = kb.keyboard
        # 5 baris × 2 tombol + 1 baris bantuan, sama dengan menu inline
        self.assertEqual(len(rows), 6)
        for row in rows[:-1]:
            self.assertEqual(len(row), 2)
        self.assertEqual(len(rows[-1]), 1)
        # Tombolnya KeyboardButton (bukan inline)
        self.assertTrue(all(b.text for row in rows for b in row))

    def test_all_labels_map_to_supported_actions(self):
        # Setiap label tombol keyboard harus terpetakan ke aksi yang punya
        # handler (gold_price, eurusd, overview, calendar, sentiment, macro,
        # morning, prediksi, alert_on, settings, help).
        supported = {
            "gold_price", "eurusd", "overview", "calendar", "sentiment",
            "macro", "morning", "prediksi", "alert_on", "settings", "help",
        }
        for row in MENU_KEYBOARD_LABELS:
            for label in row:
                self.assertIn(label, MENU_KEYBOARD_ACTIONS)
                self.assertIn(MENU_KEYBOARD_ACTIONS[label], supported)

    def test_labels_match_inline_menu(self):
        # Label keyboard = label inline menu → user melihat menu yang konsisten
        inline_labels = {
            "🥇 Harga Gold", "💱 EUR/USD", "🌍 Overview Pasar", "📅 Kalender",
            "📰 Sentimen Pasar", "🏛️ Data Makro", "🌅 Morning Brief",
            "🎯 Prediksi News", "🔔 Alert Event", "⚙️ Pengaturan", "❓ Bantuan",
        }
        keyboard_labels = {label for row in MENU_KEYBOARD_LABELS for label in row}
        self.assertEqual(keyboard_labels, inline_labels)

    def test_start_sends_reply_keyboard(self):
        bot = MarketBot.__new__(MarketBot)
        user = type("U", (), {"id": 9999, "username": "tester", "first_name": "Tester"})()
        upd = _FakeUpdate("/start", user_id=9999)
        upd.effective_user = user
        asyncio.run(bot.start(upd, _FakeContext()))
        # start() mengirim 2 pesan: welcome (inline) + petunjuk (reply keyboard)
        self.assertGreaterEqual(len(upd.message.replies), 2)
        reply_kb = upd.message.replies[1][1].get("reply_markup")
        self.assertIsNotNone(reply_kb, "Pesan kedua harus memasang reply keyboard")
        self.assertTrue(hasattr(reply_kb, "keyboard"))
        self.assertEqual(len(reply_kb.keyboard), 6)


class TestMenuCallbacks(unittest.TestCase):
    """Callback tombol menu baru: alert_on menambah subscriber event."""

    def _run_callback(self, data):

        class _QMsg:
            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append((text, kwargs))

        class _Query:
            def __init__(self):
                self.data = data
                self.answered = False
                self.message = _QMsg()

            async def answer(self):
                self.answered = True

        query = _Query()
        upd = type("U", (), {
            "callback_query": query,
            "effective_chat": type("C", (), {"id": 777})(),
        })()
        bot = MarketBot.__new__(MarketBot)
        ctx = _FakeContext()
        asyncio.run(bot.handle_callback(upd, ctx))
        return query, ctx

    def test_alert_on_adds_subscriber(self):

        query, ctx = self._run_callback("alert_on")
        self.assertTrue(query.answered)
        self.assertIn(777, ctx.bot_data["event_alert_subscribers"])
        text, _ = query.message.replies[0]
        self.assertIn("AKTIF", text)


class TestSettingsMenu(unittest.TestCase):
    """Menu Pengaturan (⚙️): menampilkan status & toggle alert/brief/konteks."""

    class _QMsg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append((text, kwargs))

    class _Query:
        def __init__(self, data, user_id=9999):
            self.data = data
            self.answered = False
            self.message = TestSettingsMenu._QMsg()
            self.from_user = type("U", (), {"id": user_id})()
            self.edited = []

        async def answer(self):
            self.answered = True

        async def edit_message_text(self, text, **kwargs):
            self.edited.append((text, kwargs))

    def _run(self, data):
        query = self._Query(data)
        upd = type("U", (), {
            "callback_query": query,
            "effective_chat": type("C", (), {"id": 777})(),
            "effective_user": type("U", (), {"id": 9999})(),
        })()
        bot = MarketBot.__new__(MarketBot)
        ctx = _FakeContext()
        with mock.patch.object(
            handlers_mod.db, "is_subscribed_async", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            handlers_mod.db, "remove_subscriber_async", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            handlers_mod.db, "add_subscriber_async", new=mock.AsyncMock(return_value=True)
        ):
            asyncio.run(bot.handle_callback(upd, ctx))
        return query, ctx

    def test_settings_shows_status_and_buttons(self):
        query, ctx = self._run("settings")
        self.assertTrue(query.answered)
        text, kwargs = query.edited[0]
        self.assertIn("PENGATURAN", text)
        self.assertIn("Alert Event", text)
        self.assertIn("Morning Brief", text)
        kb = kwargs.get("reply_markup")
        callbacks = [
            btn.callback_data for row in kb.inline_keyboard for btn in row
        ]
        self.assertIn("settings_alert", callbacks)
        self.assertIn("settings_brief", callbacks)
        self.assertIn("settings_clear", callbacks)
        self.assertIn("menu", callbacks)

    def test_settings_alert_toggles_on(self):
        query, _ = self._run("settings_alert")
        text, _ = query.edited[0]
        self.assertIn("diaktifkan", text)

    def test_settings_brief_toggles(self):
        query, _ = self._run("settings_brief")
        text, _ = query.edited[0]
        self.assertIn("dihentikan", text)  # is_subscribed mocked True → berhenti

    def test_settings_clear(self):
        query, _ = self._run("settings_clear")
        text, _ = query.edited[0]
        self.assertIn("dibersihkan", text)

    def test_menu_returns_to_main(self):
        query, _ = self._run("menu")
        text, kwargs = query.edited[0]
        from bot.messages import WELCOME_MESSAGE
        self.assertEqual(text, WELCOME_MESSAGE)
        kb = kwargs.get("reply_markup")
        self.assertIsNotNone(kb)


class TestQuickActionKeyboard(unittest.TestCase):
    def test_buttons_with_symbol(self):
        kb = _quick_action_keyboard("EURUSD=X")
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
        ]
        self.assertIn("qa:sr:EURUSD=X", callbacks)
        self.assertIn("qa:scenario:EURUSD=X", callbacks)

    def test_buttons_without_symbol(self):
        kb = _quick_action_keyboard()
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
        ]
        self.assertIn("qa:sr", callbacks)
        self.assertIn("qa:scenario", callbacks)


class TestResolveSymbolFromText(unittest.TestCase):
    """_resolve_symbol_from_text: keyword map, reverse YAHOO_SYMBOLS, direct OANDA."""

    def _bot(self):
        from utils.chart_generator import ChartGenerator

        bot = MarketBot.__new__(MarketBot)
        bot.chart = ChartGenerator()
        return bot

    def test_keyword_map(self):
        bot = self._bot()
        self.assertEqual(bot._resolve_symbol_from_text("eurusd"), ("EURUSD=X", "EUR/USD"))
        self.assertEqual(bot._resolve_symbol_from_text("emas")[0], "GC=F")
        self.assertEqual(bot._resolve_symbol_from_text("bitcoin")[0], "BTC-USD")

    def test_reverse_yahoo_symbols(self):
        bot = self._bot()
        symbol, display = bot._resolve_symbol_from_text("dow jones")
        self.assertEqual(symbol, "^DJI")
        symbol2, display2 = bot._resolve_symbol_from_text("usd/idr")
        self.assertEqual(symbol2, "USDIDR=X")

    def test_direct_oanda_symbols(self):
        bot = self._bot()
        symbol, display = bot._resolve_symbol_from_text("eurgbp")
        self.assertEqual(symbol, "EURGBP=X")
        self.assertEqual(display, "EUR/GBP")
        symbol2, display2 = bot._resolve_symbol_from_text("cl")
        self.assertEqual(symbol2, "CL=F")

    def test_invalid(self):
        bot = self._bot()
        self.assertEqual(bot._resolve_symbol_from_text("xyzzy"), (None, None))
        self.assertEqual(bot._resolve_symbol_from_text(""), (None, None))
        self.assertEqual(bot._resolve_symbol_from_text(None), (None, None))

    def test_normalize_input(self):
        self.assertEqual(MarketBot._normalize_symbol_input("EUR/USD"), "eurusd")
        self.assertEqual(MarketBot._normalize_symbol_input("eur usd"), "eurusd")
        self.assertEqual(MarketBot._normalize_symbol_input(" Gold "), "gold")


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return None


class _FakeUpdate:
    def __init__(self, text, user_id=9999, chat_id=777):
        self.message = _FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": chat_id})()


class _FakeContext:
    def __init__(self):
        self.bot_data = {}
        self.bot = None


class TestOverviewRefreshButton(unittest.TestCase):
    """Tombol '🔁 Refresh' di /overview — harga segar tanpa nunggu cache 10 menit."""

    class _FakeQuery:
        def __init__(self, data):
            self.data = data
            self.answered = False
            self.edits = []
            self.edit_kwargs = []

        async def answer(self):
            self.answered = True

        async def edit_message_text(self, text, **kwargs):
            self.edits.append(text)
            self.edit_kwargs.append(kwargs)
            return None

    def _bot(self, calls):
        class FakeMarketSummary:
            def get_market_summary(self, refresh=False):
                calls.append(refresh)
                return "📊 *RINGKASAN PASAR*\n🟢 *EUR/USD*: 1.0850 (+0.10%)"

        bot = MarketBot.__new__(MarketBot)
        bot.market = FakeMarketSummary()
        return bot

    def test_overview_reply_has_refresh_button(self):
        calls = []
        bot = self._bot(calls)
        message, kb = asyncio.run(bot._build_overview_reply(refresh=True))
        self.assertEqual(calls, [True])  # refresh=True → bypass cache
        self.assertIn("MARKET OVERVIEW", message)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(callbacks, ["overview_refresh"])

    def test_overview_refresh_callback_edits_message(self):
        calls = []
        bot = self._bot(calls)
        query = self._FakeQuery("overview_refresh")
        update = type("U", (), {
            "callback_query": query,
            "effective_chat": type("C", (), {"id": 777})(),
        })()
        asyncio.run(bot.handle_callback(update, None))
        self.assertTrue(query.answered)
        self.assertEqual(calls, [True])
        self.assertTrue(any("MARKET OVERVIEW" in e for e in query.edits))
        self.assertTrue(any(kw.get("reply_markup") is not None for kw in query.edit_kwargs))

    def test_overview_menu_callback_has_refresh_button(self):
        calls = []
        bot = self._bot(calls)
        query = self._FakeQuery("overview")
        update = type("U", (), {
            "callback_query": query,
            "effective_chat": type("C", (), {"id": 777})(),
        })()
        async def _noop(*a, **k):
            return None

        ctx = type("Ctx", (), {"bot": type("B", (), {"send_chat_action": _noop})()})()
        asyncio.run(bot.handle_callback(update, ctx))
        self.assertTrue(query.answered)
        self.assertTrue(any(kw.get("reply_markup") is not None for kw in query.edit_kwargs))


class TestStripMarkdownAsterisks(unittest.TestCase):
    def test_double_asterisk_bold(self):
        self.assertEqual(strip_markdown_asterisks("**bold** teks"), "bold teks")

    def test_single_asterisk_italic(self):
        self.assertEqual(strip_markdown_asterisks("*miring*"), "miring")

    def test_numeric_multiplication_preserved(self):
        self.assertEqual(strip_markdown_asterisks("5*3=15"), "5*3=15")

    def test_none_input(self):
        self.assertIsNone(strip_markdown_asterisks(None))

    def test_empty_input(self):
        self.assertEqual(strip_markdown_asterisks(""), "")

    def test_mixed_content(self):
        out = strip_markdown_asterisks("**Judul** harga *naik* 2*3")
        self.assertNotIn("*", out.replace("2*3", ""))
        self.assertIn("2*3", out)


if __name__ == "__main__":
    unittest.main()
