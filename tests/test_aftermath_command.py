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

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
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

    def test_pick_aftermath_buttons_window_and_order(self):
        events = [
            _event("CPI / Inflasi AS (YoY)", hours_ago=20),
            _event("NFP", hours_ago=1, actual=250.0, unit="K"),
            _event("GDP AS (QoQ)", hours_ago=-1),
            _event("FOMC", hours_ago=3 * 24, actual=4.75),
            _event("PPI", hours_ago=-10 * 24),  # 10 hari lagi → di luar jendela +7 hari
        ]
        picked = MarketBot._pick_aftermath_buttons(events, max_buttons=5)
        names = [e["event"] for e in picked]
        self.assertNotIn("PPI", names)
        self.assertEqual(names[0], "NFP")  # paling dekat dengan sekarang
        self.assertEqual(names[1], "GDP AS (QoQ)")

    def test_pick_aftermath_buttons_max_and_empty(self):
        events = [_event("CPI", hours_ago=i) for i in range(1, 10)]
        self.assertEqual(len(MarketBot._pick_aftermath_buttons(events, max_buttons=3)), 3)
        self.assertEqual(MarketBot._pick_aftermath_buttons([], 5), [])
        self.assertEqual(MarketBot._pick_aftermath_buttons(None, 5), [])

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

    def test_calendar_buttons_none_when_no_relevant(self):
        bot = MarketBot.__new__(MarketBot)
        events = [_event("PPI", hours_ago=-10 * 24)]  # di luar jendela
        self.assertIsNone(bot._build_calendar_aftermath_buttons(events))
        self.assertIsNone(bot._build_calendar_aftermath_buttons([]))


class TestAftermathCalendarCallback(unittest.TestCase):
    """Alur callback 'aft:<id>' — analisis event langsung dari tombol kalender."""

    def _bot(self, events):
        class FakeMacro:
            def __init__(self):
                self.events = events

            async def get_economic_calendar_month(self):
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
