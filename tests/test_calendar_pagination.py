"""Unit tests /calendar: filter USD+high, urutan (upcoming dulu), pagination,
seksi event terakhir, dan toggle mode — tanpa network.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from bot.handlers import MarketBot
from data.macro_data import MacroDataFetcher

NOW = datetime.now(timezone.utc)


def _ev(name, *, currency="USD", country="US", impact="high", hours=1,
        actual=None, estimate=None, prev=None, unit=""):
    return {
        "event": name,
        "country": country,
        "country_emoji": "🇺🇸" if country == "US" else "🌍",
        "currency": currency,
        "time": "01 Jan 2026 00:00 WIB",
        "_dt_utc": NOW + timedelta(hours=hours),
        "impact": impact,
        "impact_label": "🔥 HIGH" if impact == "high" else "📊 LOW",
        "actual": actual,
        "estimate": estimate,
        "prev": prev,
        "unit": unit,
        "source": "forexfactory",
    }


class _FakeMacro:
    def __init__(self, events):
        self.events = events
        self.refresh_flags = []

    async def get_economic_calendar_month(self, refresh=False):
        self.refresh_flags.append(refresh)
        return self.events

    def format_calendar_text(self, events, **kwargs):
        return MacroDataFetcher.__new__(MacroDataFetcher).format_calendar_text(events, **kwargs)


def _bot(events):
    bot = MarketBot.__new__(MarketBot)
    bot.macro = _FakeMacro(events)
    return bot


def _callbacks(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


class TestUsdDetection(unittest.TestCase):
    def test_currency_field(self):
        self.assertTrue(MarketBot._is_usd_event(_ev("X", currency="USD")))
        self.assertFalse(MarketBot._is_usd_event(_ev("X", currency="EUR", country="EU")))

    def test_country_fallback_when_currency_missing(self):
        self.assertTrue(MarketBot._is_usd_event({"event": "X", "country": "US"}))
        self.assertFalse(MarketBot._is_usd_event({"event": "X", "country": "JP"}))


class TestOrderedEvents(unittest.TestCase):
    def test_usd_high_filter(self):
        bot = _bot([])
        events = [
            _ev("US High", hours=1),
            _ev("EU High", currency="EUR", country="EU", hours=2),
            _ev("US Low", impact="low", hours=3),
        ]
        upcoming, past = bot._ordered_calendar_events(events, "usd_high")
        self.assertEqual([e["event"] for e in upcoming], ["US High"])
        self.assertEqual(past, [])

    def test_upcoming_before_past_and_ordering(self):
        bot = _bot([])
        events = [
            _ev("Past recent", hours=-1),
            _ev("Upcoming far", hours=72),
            _ev("Past old", hours=-48),
            _ev("Upcoming near", hours=2),
        ]
        upcoming, past = bot._ordered_calendar_events(events, "all")
        self.assertEqual([e["event"] for e in upcoming], ["Upcoming near", "Upcoming far"])
        self.assertEqual([e["event"] for e in past], ["Past recent", "Past old"])

    def test_all_mode_keeps_everything(self):
        bot = _bot([])
        events = [_ev("A"), _ev("B", currency="EUR", country="EU", impact="low")]
        upcoming, _ = bot._ordered_calendar_events(events, "all")
        self.assertEqual(len(upcoming), 2)


class TestCalendarReply(unittest.TestCase):
    def test_default_mode_filters_to_usd_high(self):
        bot = _bot([
            _ev("US CPI", hours=5),
            _ev("ECB Rate Decision", currency="EUR", country="EU", hours=6),
        ])
        message, kb = asyncio.run(bot._build_calendar_reply())
        self.assertIn("US CPI", message)
        self.assertNotIn("ECB Rate Decision", message)
        self.assertIn("Filter: USD · High Impact", message)
        # Toggle ke semua event tersedia
        self.assertIn("cal:all:0", _callbacks(kb))
        self.assertIn("calendar_refresh", _callbacks(kb))

    def test_pagination(self):
        events = [_ev(f"US Event {i}", hours=i + 1) for i in range(6)]
        bot = _bot(events)
        msg0, kb0 = asyncio.run(bot._build_calendar_reply(page=0))
        self.assertIn("Halaman *1/2*", msg0)
        self.assertIn("cal:usd_high:1", _callbacks(kb0))  # tombol Berikutnya
        msg1, kb1 = asyncio.run(bot._build_calendar_reply(page=1))
        self.assertIn("Halaman *2/2*", msg1)
        self.assertIn("cal:usd_high:0", _callbacks(kb1))  # tombol Sebelumnya

    def test_recent_released_events_section(self):
        bot = _bot([
            _ev("NF Payrolls", hours=-2, actual="250K", estimate="180K", prev="160K"),
            _ev("Upcoming USD", hours=10),
        ])
        message, _ = asyncio.run(bot._build_calendar_reply())
        self.assertIn("EVENT TERAKHIR (sudah rilis)", message)
        self.assertIn("NF Payrolls", message)
        self.assertIn("250K", message)

    def test_toggle_to_all_mode(self):
        bot = _bot([_ev("US CPI", hours=3), _ev("BoJ", currency="JPY", country="JP", hours=4)])
        message, kb = asyncio.run(bot._build_calendar_reply(mode="all"))
        self.assertIn("Filter: Semua Event", message)
        self.assertIn("BoJ", message)
        self.assertIn("cal:usd_high:0", _callbacks(kb))  # toggle kembali ke USD High

    def test_invalid_mode_falls_back_to_default(self):
        bot = _bot([_ev("US CPI", hours=3)])
        message, _ = asyncio.run(bot._build_calendar_reply(mode="bogus"))
        self.assertIn("Filter: USD · High Impact", message)


class TestWeekJumpButtons(unittest.TestCase):
    def test_present_in_usd_mode(self):
        bot = _bot([_ev("US Event", hours=0)])  # minggu ini
        _, kb = asyncio.run(bot._build_calendar_reply())
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Minggu Ini" in lbl for lbl in labels))

    def test_absent_in_all_mode(self):
        bot = _bot([_ev("US Event", hours=0), _ev("EU Event", currency="EUR", country="EU", hours=1)])
        _, kb = asyncio.run(bot._build_calendar_reply(mode="all"))
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertFalse(any("Minggu" in lbl for lbl in labels))

    def test_targets_map_to_pages(self):
        # 6 event dalam beberapa minggu + 1 jauh → target halaman 0 dan 1
        events = [_ev(f"E{i}", hours=24 * (i + 1)) for i in range(6)] + [_ev("Far", hours=24 * 30)]
        bot = _bot(events)
        _, kb = asyncio.run(bot._build_calendar_reply(page=0))
        cbs = _callbacks(kb)
        self.assertIn("cal:usd_high:0", cbs)
        self.assertIn("cal:usd_high:1", cbs)


class TestRecentEventsFormat(unittest.TestCase):
    def test_format_with_actual_and_prev(self):
        text = MarketBot._format_recent_events([
            _ev("NF Payrolls", hours=-1, actual="250K", estimate="180K", prev="160K"),
        ])
        self.assertIn("NF Payrolls", text)
        self.assertIn("Actual", text)
        self.assertIn("250K", text)

    def test_empty_returns_empty(self):
        self.assertEqual(MarketBot._format_recent_events([]), "")


if __name__ == "__main__":
    unittest.main()
