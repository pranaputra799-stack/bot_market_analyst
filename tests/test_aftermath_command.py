"""
Unit tests untuk command /aftermath <event> — analisis dampak event manual.

Mencakup:
- _search_aftermath_events: pencarian kata kunci, peringkat event rilis, tie-break
- Branch /aftermath tanpa argumen → usage
- Header pesan manual berbeda dari notifikasi otomatis
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.handlers import MarketBot

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _event(name="CPI / Inflasi AS (YoY)", hours_ago=2, impact="high",
           actual=2.9, estimate=3.0, prev=3.2, unit="%", country="US"):
    return {
        "event": name,
        "country": country,
        "country_emoji": "🇺🇸" if country == "US" else "🌍",
        "time": "07 Agu 2026 19:30 WIB",
        "_dt_utc": NOW - timedelta(hours=hours_ago),
        "impact": impact,
        "impact_label": "🔥 HIGH" if impact == "high" else "⚠️ MEDIUM",
        "actual": actual,
        "estimate": estimate,
        "prev": prev,
        "unit": unit,
        "source": "fred",
    }


class TestSearchAftermathEvents(unittest.TestCase):

    def test_match_by_keyword(self):
        events = [
            _event("Non-Farm Payrolls (NFP) & Unemployment Rate", hours_ago=1, actual=250.0, unit="K"),
            _event("CPI / Inflasi AS (YoY)", hours_ago=2),
        ]
        out = MarketBot._search_aftermath_events(events, "nfp")
        self.assertEqual(
            [e["event"] for e in out],
            ["Non-Farm Payrolls (NFP) & Unemployment Rate"],
        )

    def test_case_insensitive(self):
        events = [_event("CPI / Inflasi AS (YoY)")]
        self.assertEqual(len(MarketBot._search_aftermath_events(events, "CPI")), 1)
        self.assertEqual(len(MarketBot._search_aftermath_events(events, "cpi")), 1)

    def test_released_events_rank_first(self):
        # Event yang sudah rilis (ada actual) diutamakan walau lebih lama dari
        # event yang sama yang belum rilis
        released = _event("Fed Funds Rate Decision (FOMC)", hours_ago=5, actual=4.75)
        upcoming = _event("Fed Funds Rate Decision (FOMC)", hours_ago=-2, actual=None)
        out = MarketBot._search_aftermath_events([upcoming, released], "fomc")
        self.assertEqual(out[0]["actual"], 4.75)

    def test_most_recent_first_on_tie(self):
        old = _event("GDP AS (QoQ)", hours_ago=8)
        new = _event("GDP AS (QoQ)", hours_ago=1)
        out = MarketBot._search_aftermath_events([old, new], "gdp")
        self.assertEqual(out[0]["_dt_utc"], new["_dt_utc"])

    def test_medium_impact_still_searchable(self):
        # Pencarian manual tidak dibatasi high-only — user bisa minta event apa pun
        events = [_event("Initial Jobless Claims (US)", impact="medium")]
        out = MarketBot._search_aftermath_events(events, "claims")
        self.assertEqual(len(out), 1)

    def test_no_match_and_empty_query(self):
        events = [_event("CPI / Inflasi AS (YoY)")]
        self.assertEqual(MarketBot._search_aftermath_events(events, "xyz"), [])
        self.assertEqual(MarketBot._search_aftermath_events([], "cpi"), [])
        self.assertEqual(MarketBot._search_aftermath_events(events, ""), [])
        self.assertEqual(MarketBot._search_aftermath_events(events, None), [])
        self.assertEqual(MarketBot._search_aftermath_events(None, "cpi"), [])


class TestAftermathMessage(unittest.TestCase):

    def _bot(self):
        bot = MarketBot.__new__(MarketBot)
        bot.ai = None  # paksa fallback interpretasi statis — tanpa network
        return bot

    def test_manual_header_differs_from_auto(self):
        bot = self._bot()
        manual = asyncio.run(
            bot._build_aftermath_message(_event(), "DXY: 104.2 🔴 -0.25%", manual=True)
        )
        auto = asyncio.run(
            bot._build_aftermath_message(_event(), "DXY: 104.2 🔴 -0.25%", manual=False)
        )
        self.assertIn("ANALISIS DAMPAK EVENT", manual)
        self.assertNotIn("AFTERMATH EVENT EKONOMI", manual)
        self.assertIn("AFTERMATH EVENT EKONOMI", auto)

    def test_message_contains_numbers_and_interpretation(self):
        bot = self._bot()
        msg = asyncio.run(
            bot._build_aftermath_message(_event(), "DXY: 104.2", manual=True)
        )
        self.assertIn("*Actual:*", msg)
        self.assertIn("2.9%", msg)
        self.assertIn("Interpretasi", msg)  # fallback statis saat AI tidak ada

    def test_usage_branch_no_arg(self):
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/aftermath")
        asyncio.run(bot.aftermath_command(upd, _FakeContext()))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("ANALISIS DAMPAK EVENT", joined)
        self.assertIn("/aftermath nfp", joined)

    def test_usage_branch_help(self):
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/aftermath help")
        asyncio.run(bot.aftermath_command(upd, _FakeContext()))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("/aftermath cpi", joined)
        self.assertIn("/aftermath fomc", joined)


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return None


class _FakeUpdate:
    def __init__(self, text):
        self.message = _FakeMessage(text)
        self.effective_chat = type("C", (), {"id": 777})()


class _FakeContext:
    def __init__(self):
        self.bot_data = {}
        self.bot = None


class _CallbackMessage:
    """Message fiktif untuk callback query — reply_text mencatat pesan."""

    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return None


class _CallbackQuery:
    """Query fiktif untuk handle_callback (answer + edit_message_text)."""

    def __init__(self, data, message=None):
        self.data = data
        self.message = message or _CallbackMessage()
        self.answered = False
        self.edits = []
        self.edit_kwargs = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
        self.edit_kwargs.append(kwargs)
        return None


class TestCalendarAftermathButtons(unittest.TestCase):
    """Tombol '📊 Analisis Dampak' di /calendar — label, ID, pemilihan, markup."""

    def test_short_event_label_known_mapping(self):
        self.assertEqual(MarketBot._short_event_label("Non-Farm Payrolls (NFP) & Unemployment Rate"), "NFP")
        self.assertEqual(MarketBot._short_event_label("Fed Funds Rate Decision (FOMC)"), "FOMC")
        self.assertEqual(MarketBot._short_event_label("CPI / Inflasi AS (YoY)"), "CPI")
        self.assertEqual(MarketBot._short_event_label("GDP AS (QoQ)"), "GDP")
        self.assertEqual(MarketBot._short_event_label(""), "EVENT")

    def test_short_event_label_fallback_truncates(self):
        label = MarketBot._short_event_label("Pertumbuhan Ekonomi Zona Euro (Proyeksi)")
        self.assertLessEqual(len(label), 16)
        self.assertEqual(label, label.upper())

    def test_event_short_id_stable_and_distinct(self):
        e1 = _event("CPI / Inflasi AS (YoY)")
        e2 = _event("CPI / Inflasi AS (YoY)", hours_ago=1)  # waktu berbeda
        e3 = _event("Non-Farm Payrolls (NFP) & Unemployment Rate", actual=250.0, unit="K")
        self.assertEqual(MarketBot._event_short_id(e1), MarketBot._event_short_id(dict(e1)))
        self.assertNotEqual(MarketBot._event_short_id(e1), MarketBot._event_short_id(e2))
        self.assertNotEqual(MarketBot._event_short_id(e1), MarketBot._event_short_id(e3))
        # Payload callback Telegram maks 64 byte
        self.assertLessEqual(len(f"aft:{MarketBot._event_short_id(e1)}"), 64)

    def test_all_displayed_events_get_buttons(self):
        # SEMUA event yang tampil diberi tombol — termasuk yang di luar jendela
        # relevan ±14 hari (mis. event awal bulan atau akhir bulan)
        events = [
            _event("CPI / Inflasi AS (YoY)", hours_ago=20),
            _event("Non-Farm Payrolls (NFP) & Unemployment Rate", hours_ago=1, actual=250.0, unit="K"),
            _event("FOMC", hours_ago=-10 * 24),   # 10 hari lagi
            _event("GDP AS (QoQ)", hours_ago=20 * 24),  # 20 hari lalu
        ]
        bot = MarketBot.__new__(MarketBot)
        kb = bot._build_calendar_aftermath_buttons(events)
        self.assertIsNotNone(kb)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        for e in events:
            self.assertIn(f"aft:{MarketBot._event_short_id(e)}", callbacks)

    def test_max_buttons_caps_at_15(self):
        events = [_event(f"Event {i}", hours_ago=i) for i in range(1, 25)]
        bot = MarketBot.__new__(MarketBot)
        kb = bot._build_calendar_aftermath_buttons(events)
        self.assertIsNotNone(kb)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertLessEqual(len(callbacks), 15)

    def test_numbered_button_labels_match_order(self):
        events = [
            _event("Non-Farm Payrolls (NFP) & Unemployment Rate", hours_ago=1, actual=250.0, unit="K"),
            _event("CPI / Inflasi AS (YoY)", hours_ago=2),
            _event("Fed Funds Rate Decision (FOMC)", hours_ago=3, actual=4.75),
        ]
        bot = MarketBot.__new__(MarketBot)
        kb = bot._build_calendar_aftermath_buttons(events, numbered=True)
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertEqual(labels, ["📊 1·NFP", "📊 2·CPI", "📊 3·FOMC"])
        # Callback tetap memakai ID event (bukan nomor)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(callbacks[1], f"aft:{MarketBot._event_short_id(events[1])}")

    def test_buttons_not_numbered_by_default(self):
        # Digest & reminder tanpa nomor (daftarnya pendek, nomor tak perlu)
        events = [
            _event("Non-Farm Payrolls (NFP) & Unemployment Rate", hours_ago=1, actual=250.0, unit="K"),
            _event("CPI / Inflasi AS (YoY)", hours_ago=2),
        ]
        bot = MarketBot.__new__(MarketBot)
        kb = bot._build_calendar_aftermath_buttons(events)
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertEqual(labels, ["📊 NFP", "📊 CPI"])

    def test_calendar_buttons_markup(self):
        bot = MarketBot.__new__(MarketBot)
        events = [
            _event("CPI / Inflasi AS (YoY)", hours_ago=2),
            _event("Non-Farm Payrolls (NFP) & Unemployment Rate", hours_ago=1, actual=250.0, unit="K"),
        ]
        kb = bot._build_calendar_aftermath_buttons(events)
        self.assertIsNotNone(kb)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        for e in events:
            self.assertIn(f"aft:{MarketBot._event_short_id(e)}", callbacks)
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("📊" in lbl and "NFP" in lbl for lbl in labels))

    def test_calendar_buttons_none_when_empty(self):
        bot = MarketBot.__new__(MarketBot)
        self.assertIsNone(bot._build_calendar_aftermath_buttons([]))
        self.assertIsNone(bot._build_calendar_aftermath_buttons(None))
        # Event medium/low tidak diberi tombol (konsisten dengan matching)
        self.assertIsNone(
            bot._build_calendar_aftermath_buttons([_event("CPI / Inflasi AS (YoY)", impact="medium")])
        )


class _FakeSendBot:
    """Bot fiktif yang menangkap send_message untuk memeriksa tombol."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode, **kwargs})
        return None


class _FakeApp:
    """Application fiktif untuk job scheduler (digest & reminder)."""

    def __init__(self, subscribers):
        self.bot = _FakeSendBot()
        self.bot_data = {"event_alert_subscribers": set(subscribers)}


class _FakeJobMacro:
    """Macro fiktif untuk job scheduler — mengembalikan daftar event yang sama."""

    def __init__(self, events):
        self.events = events

    async def get_economic_calendar(self, from_date=None, to_date=None):
        return self.events

    async def get_economic_calendar_month(self):
        return self.events


def _today_event(name, hour, impact="high", actual=2.9, estimate=3.0, prev=3.2, unit="%"):
    """Event ber-waktu HARI INI (WIB) — agar lolos filter digest."""
    tz_wib = ZoneInfo("Asia/Jakarta")
    today = datetime.now(tz_wib).date()
    dt_utc = datetime(
        today.year, today.month, today.day, hour, 0, tzinfo=tz_wib
    ).astimezone(timezone.utc)
    return {
        "event": name,
        "country": "US",
        "country_emoji": "🇺🇸",
        "time": f"{today.day:02d} {today.strftime('%b')} {today.year} {hour:02d}:00 WIB",
        "_dt_utc": dt_utc,
        "impact": impact,
        "impact_label": "🔥 HIGH" if impact == "high" else "⚠️ MEDIUM",
        "actual": actual,
        "estimate": estimate,
        "prev": prev,
        "unit": unit,
        "source": "fred",
    }


class TestDigestAndReminderButtons(unittest.TestCase):
    """Digest pagi & reminder sebelum event punya tombol '📊 Analisis Dampak'."""

    def test_digest_sends_aftermath_buttons(self):
        events = [
            _today_event("Non-Farm Payrolls (NFP) & Unemployment Rate", 13, actual=250.0, unit="K"),
            _today_event("CPI / Inflasi AS (YoY)", 19),
        ]
        bot = MarketBot.__new__(MarketBot)
        bot.macro = _FakeJobMacro(events)
        app = _FakeApp([777])
        asyncio.run(bot.send_scheduled_event_digest(app))
        self.assertEqual(len(app.bot.sent), 1)
        sent = app.bot.sent[0]
        self.assertIn("HIGH-IMPACT HARI INI", sent["text"])
        kb = sent.get("reply_markup")
        self.assertIsNotNone(kb)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        for e in events:
            self.assertIn(f"aft:{MarketBot._event_short_id(e)}", callbacks)
        self.assertIn("analisis dampak", sent["text"])

    def test_digest_no_events_no_buttons(self):
        bot = MarketBot.__new__(MarketBot)
        bot.macro = _FakeJobMacro([])
        app = _FakeApp([777])
        asyncio.run(bot.send_scheduled_event_digest(app))
        self.assertEqual(len(app.bot.sent), 1)
        sent = app.bot.sent[0]
        self.assertIn("Tidak ada rilis", sent["text"])
        self.assertIsNone(sent.get("reply_markup"))

    def test_reminder_sends_aftermath_button(self):
        from config.settings import ECONOMIC_ALERT_LEAD_HOURS

        dt = datetime.now(timezone.utc) + timedelta(minutes=30)
        event = {
            "event": "Fed Funds Rate Decision (FOMC)",
            "country": "US",
            "country_emoji": "🇺🇸",
            "time": "30 menit lagi",
            "_dt_utc": dt,
            "impact": "high",
            "impact_label": "🔥 HIGH",
            "actual": None,
            "estimate": 4.75,
            "prev": 4.50,
            "unit": "%",
            "source": "fred",
        }
        self.assertGreaterEqual(ECONOMIC_ALERT_LEAD_HOURS, 1)
        bot = MarketBot.__new__(MarketBot)
        bot.macro = _FakeJobMacro([event])
        app = _FakeApp([777])
        asyncio.run(bot.check_event_reminders(app))
        self.assertEqual(len(app.bot.sent), 1)
        sent = app.bot.sent[0]
        self.assertIn("REMINDER", sent["text"])
        kb = sent.get("reply_markup")
        self.assertIsNotNone(kb)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn(f"aft:{MarketBot._event_short_id(event)}", callbacks)
        self.assertIn("analisis dampak", sent["text"])


class TestCalendarRefreshButton(unittest.TestCase):
    """Tombol '🔁 Refresh' — muat ulang kalender tanpa mengetik /calendar."""

    @staticmethod
    def _real_format(events, **kwargs):
        # format_calendar_text adalah method instance murni — aman via __new__
        from data.macro_data import MacroDataFetcher

        return MacroDataFetcher.__new__(MacroDataFetcher).format_calendar_text(events, **kwargs)

    def _bot_with_macro(self, events, calls):
        class FakeMacro:
            async def get_economic_calendar_month(self, refresh=False):
                calls.append(refresh)
                return events

            def format_calendar_text(self, events, **kwargs):
                return TestCalendarRefreshButton._real_format(events, **kwargs)

        bot = MarketBot.__new__(MarketBot)
        bot.macro = FakeMacro()
        return bot

    def test_add_refresh_button_always_present(self):
        bot = MarketBot.__new__(MarketBot)
        # Tanpa tombol analisis → tetap ada baris refresh
        kb = bot._add_refresh_button(None)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(callbacks, ["calendar_refresh"])
        # Dengan tombol analisis → refresh jadi baris terakhir
        events = [_event("Non-Farm Payrolls (NFP) & Unemployment Rate", hours_ago=1, actual=250.0, unit="K")]
        aft = bot._build_calendar_aftermath_buttons(events, numbered=True)
        kb2 = bot._add_refresh_button(aft)
        all_callbacks = [btn.callback_data for row in kb2.inline_keyboard for btn in row]
        self.assertIn(f"aft:{MarketBot._event_short_id(events[0])}", all_callbacks)
        self.assertEqual(all_callbacks[-1], "calendar_refresh")

    def test_build_calendar_reply_passes_refresh_flag(self):
        calls = []
        bot = self._bot_with_macro([_event("CPI / Inflasi AS (YoY)", hours_ago=2)], calls)
        message, kb = asyncio.run(bot._build_calendar_reply(refresh=True))
        self.assertEqual(calls, [True])  # refresh=True → bypass cache
        self.assertIsNotNone(kb)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("calendar_refresh", callbacks)
        self.assertIn("Ketuk tombol event", message)

    def test_refresh_callback_edits_calendar(self):
        calls = []
        bot = self._bot_with_macro([_event("CPI / Inflasi AS (YoY)", hours_ago=2)], calls)
        query = _CallbackQuery(data="calendar_refresh")
        update = type("U", (), {
            "callback_query": query,
            "effective_chat": type("C", (), {"id": 777})(),
        })()
        asyncio.run(bot.handle_callback(update, None))
        self.assertTrue(query.answered)
        self.assertEqual(calls, [True])
        self.assertTrue(any("KALENDER EKONOMI" in e for e in query.edits))
        self.assertTrue(any(kw.get("reply_markup") is not None for kw in query.edit_kwargs))


class TestAftermathCalendarCallback(unittest.TestCase):
    """Alur callback 'aft:<id>' — analisis event langsung dari tombol kalender."""

    def _bot(self, events):
        class FakeMacro:
            def __init__(self):
                self.events = events

            async def get_economic_calendar_month(self):
                return self.events

            async def get_economic_calendar(self, from_date=None, to_date=None):
                return self.events

        class FakeMarket:
            @staticmethod
            def get_yahoo_data(*args, **kwargs):
                raise RuntimeError("no network in tests")

        bot = MarketBot.__new__(MarketBot)
        bot.macro = FakeMacro()
        bot.market = FakeMarket()
        bot.ai = None  # paksa fallback interpretasi statis
        return bot

    def _update(self, query):
        return type("U", (), {
            "callback_query": query,
            "effective_chat": type("C", (), {"id": 777})(),
        })()

    def test_aft_callback_sends_analysis(self):
        event = _event(
            "Non-Farm Payrolls (NFP) & Unemployment Rate", hours_ago=2, actual=250.0, unit="K"
        )
        bot = self._bot([event])
        query = _CallbackQuery(data=f"aft:{MarketBot._event_short_id(event)}")
        asyncio.run(bot.handle_callback(self._update(query), None))
        self.assertTrue(query.answered)
        joined = "\n".join(t for t, _ in query.message.replies)
        self.assertIn("ANALISIS DAMPAK EVENT", joined)
        self.assertIn("NFP", joined)
        self.assertIn("Actual", joined)

    def test_aft_callback_unknown_event(self):
        bot = self._bot([_event("CPI / Inflasi AS (YoY)")])
        query = _CallbackQuery(data="aft:deadbeef00")
        asyncio.run(bot.handle_callback(self._update(query), None))
        self.assertTrue(query.answered)
        self.assertTrue(any("tidak ditemukan" in e for e in query.edits))

    def test_aft_callback_empty_payload(self):
        bot = self._bot([_event("CPI / Inflasi AS (YoY)")])
        query = _CallbackQuery(data="aft:")
        asyncio.run(bot.handle_callback(self._update(query), None))
        self.assertTrue(query.answered)
        self.assertTrue(any("Tombol tidak valid" in e for e in query.edits))


if __name__ == "__main__":
    unittest.main()
