"""Unit tests untuk utilitas di bot/handlers.py (split_long_text, strip asterisks)."""

import asyncio
import unittest

from bot.handlers import (
    MarketBot,
    split_long_text,
    strip_markdown_asterisks,
    label_to_symbol,
    _quick_action_keyboard,
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

    # Callback yang ditangani handle_callback (termasuk prefix generik chart_)
    SUPPORTED = {
        "morning", "overview", "gold_price", "eurusd", "macro",
        "chart_eurusd", "chart_gold", "chart_dxy", "chart_btc",
        "calendar", "sentiment", "sentimen_retail", "alert_on", "pa_usage",
        "watch_list", "riwayat_usage", "subscribe", "unsubscribe", "help",
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
                    or cb.startswith("chart_")
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
        # Fitur baru OANDA harus ada di menu
        self.assertIn("sentimen_retail", callbacks)
        self.assertIn("watch_list", callbacks)
        self.assertIn("riwayat_usage", callbacks)
        self.assertIn("alert_on", callbacks)


class TestMenuCallbacks(unittest.TestCase):
    """Callback tombol menu baru: alert_on menambah subscriber event."""

    def _run_callback(self, data):
        from bot.messages import ALERT_ON_MESSAGE

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
        from bot.messages import ALERT_ON_MESSAGE

        query, ctx = self._run_callback("alert_on")
        self.assertTrue(query.answered)
        self.assertIn(777, ctx.bot_data["event_alert_subscribers"])
        text, _ = query.message.replies[0]
        self.assertIn("AKTIF", text)

    def test_pa_usage_shows_usage(self):
        query, _ = self._run_callback("pa_usage")
        text, _ = query.message.replies[0]
        self.assertIn("ALERT HARGA", text)

    def test_riwayat_usage_shows_usage(self):
        query, _ = self._run_callback("riwayat_usage")
        text, _ = query.message.replies[0]
        self.assertIn("RIWAYAT HARGA", text)


class TestQuickActionKeyboard(unittest.TestCase):
    def test_chart_button_with_symbol(self):
        kb = _quick_action_keyboard("EURUSD=X")
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
        ]
        self.assertIn("qa:chart:EURUSD=X", callbacks)
        self.assertIn("qa:sr:EURUSD=X", callbacks)
        self.assertIn("qa:scenario:EURUSD=X", callbacks)

    def test_chart_button_without_symbol(self):
        kb = _quick_action_keyboard()
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
        ]
        self.assertIn("qa:chart", callbacks)


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


class TestPriceAlertHelpers(unittest.TestCase):
    def test_parse_valid(self):
        parsed = MarketBot._parse_price_alert_args("eurusd 1.0900")
        self.assertIsNotNone(parsed)
        symbol, display, target = parsed
        self.assertEqual(symbol, "EURUSD=X")
        self.assertEqual(target, 1.09)
        self.assertIn("EUR", display)

    def test_parse_gold_and_comma_decimal(self):
        # Koma diikuti tepat 3 digit = pemisah ribuan (gaya Eropa)
        symbol, _, target = MarketBot._parse_price_alert_args("gold 2,350")
        self.assertEqual(symbol, "GC=F")
        self.assertEqual(target, 2350.0)

    def test_parse_number_formats(self):
        # Titik diikuti 3 digit = ribuan (gaya Indonesia "2.350" = 2350)
        _, _, t1 = MarketBot._parse_price_alert_args("gold 2.350")
        self.assertEqual(t1, 2350.0)
        # Titik dengan 4 digit = desimal (kuotasi forex "1.0900" = 1.09)
        _, _, t2 = MarketBot._parse_price_alert_args("eurusd 1.0900")
        self.assertEqual(t2, 1.09)
        # Koma desimal gaya Indonesia "1,09" = 1.09
        _, _, t3 = MarketBot._parse_price_alert_args("eurusd 1,09")
        self.assertEqual(t3, 1.09)
        # Ribuan bertingkat
        _, _, t4 = MarketBot._parse_price_alert_args("btc 100,000")
        self.assertEqual(t4, 100000.0)
        # Nilai di bawah 1 selalu desimal walau ada 3 digit di belakang
        _, _, t5 = MarketBot._parse_price_alert_args("gold 0,500")
        self.assertEqual(t5, 0.5)
        _, _, t6 = MarketBot._parse_price_alert_args("gold 0.500")
        self.assertEqual(t6, 0.5)
        # Kedua pemisah: titik ribuan + koma desimal (gaya Indonesia)
        _, _, t7 = MarketBot._parse_price_alert_args("gold 2.350,50")
        self.assertEqual(t7, 2350.5)
        # Kedua pemisah: koma ribuan + titik desimal (gaya AS)
        _, _, t8 = MarketBot._parse_price_alert_args("btc 1,000.50")
        self.assertEqual(t8, 1000.5)

    def test_parse_invalid(self):
        self.assertIsNone(MarketBot._parse_price_alert_args("eurusd"))       # tanpa harga
        self.assertIsNone(MarketBot._parse_price_alert_args("1.0900"))       # tanpa simbol
        self.assertIsNone(MarketBot._parse_price_alert_args("eurusd abc"))   # harga bukan angka
        self.assertIsNone(MarketBot._parse_price_alert_args(""))

    def test_evaluate_above_triggers(self):
        alerts = [{"symbol": "EURUSD=X", "target": 1.10, "direction": "above", "id": 1}]
        triggered, remaining = MarketBot._evaluate_price_alerts(alerts, {"EURUSD=X": 1.1050})
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0]["current_price"], 1.1050)
        self.assertEqual(remaining, [])

    def test_evaluate_below_triggers(self):
        alerts = [{"symbol": "GC=F", "target": 2300.0, "direction": "below", "id": 2}]
        triggered, remaining = MarketBot._evaluate_price_alerts(alerts, {"GC=F": 2295.0})
        self.assertEqual(len(triggered), 1)
        self.assertEqual(remaining, [])

    def test_evaluate_not_crossed_keeps(self):
        alerts = [{"symbol": "EURUSD=X", "target": 1.10, "direction": "above", "id": 1}]
        triggered, remaining = MarketBot._evaluate_price_alerts(alerts, {"EURUSD=X": 1.0850})
        self.assertEqual(triggered, [])
        self.assertEqual(len(remaining), 1)

    def test_evaluate_missing_price_keeps(self):
        alerts = [{"symbol": "EURUSD=X", "target": 1.10, "direction": "above", "id": 1}]
        triggered, remaining = MarketBot._evaluate_price_alerts(alerts, {})
        self.assertEqual(triggered, [])
        self.assertEqual(len(remaining), 1)


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


class TestPriceAlertCommand(unittest.TestCase):
    """Branch /pa tanpa network: usage, list, clear, del."""

    def _bot(self):
        return MarketBot.__new__(MarketBot)

    def _run(self, text, ctx=None, user_id=9999):
        bot = self._bot()
        ctx = ctx or _FakeContext()
        asyncio.run(bot.price_alert_command(_FakeUpdate(text, user_id=user_id), ctx))
        return ctx

    def test_usage(self):
        upd = _FakeUpdate("/pa")
        bot = self._bot()
        asyncio.run(bot.price_alert_command(upd, _FakeContext()))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("ALERT HARGA", joined)
        self.assertIn("/pa eurusd 1.0900", joined)

    def test_empty_list(self):
        ctx = _FakeContext()
        bot = self._bot()
        upd = _FakeUpdate("/pa list")
        asyncio.run(bot.price_alert_command(upd, ctx))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("belum ada", joined.lower())
        self.assertEqual(ctx.bot_data, {})

    def test_list_clear_del(self):
        ctx = _FakeContext()
        # Seed alert milik user 9999 + alert user lain
        ctx.bot_data["price_alerts"] = [
            {"id": 1, "user_id": 9999, "chat_id": 777, "symbol": "EURUSD=X",
             "display_name": "EUR/USD", "target": 1.10, "direction": "above"},
            {"id": 2, "user_id": 8888, "chat_id": 888, "symbol": "GC=F",
             "display_name": "XAU/USD (Gold)", "target": 2300.0, "direction": "below"},
        ]
        bot = self._bot()
        upd = _FakeUpdate("/pa list", user_id=9999)
        asyncio.run(bot.price_alert_command(upd, ctx))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("EUR/USD", joined)
        self.assertNotIn("Gold", joined)  # hanya alert user sendiri

        # del 1 → alert 9999 hilang, alert 8888 tetap
        upd2 = _FakeUpdate("/pa del 1", user_id=9999)
        asyncio.run(bot.price_alert_command(upd2, ctx))
        remaining = ctx.bot_data["price_alerts"]
        self.assertEqual([a["id"] for a in remaining], [2])

        # clear → hanya alert milik user 9999 yang dihapus (isolasi per-user,
        # sama seperti list/del — alert user lain tidak boleh tersentuh)
        upd3 = _FakeUpdate("/pa clear", user_id=9999)
        asyncio.run(bot.price_alert_command(upd3, ctx))
        remaining = ctx.bot_data["price_alerts"]
        self.assertEqual([a["id"] for a in remaining], [2])

        # user 8888 masih bisa melihat alert-nya sendiri setelah clear user lain
        upd4 = _FakeUpdate("/pa list", user_id=8888)
        asyncio.run(bot.price_alert_command(upd4, ctx))
        joined4 = "\n".join(t for t, _ in upd4.message.replies)
        self.assertIn("Gold", joined4)


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
