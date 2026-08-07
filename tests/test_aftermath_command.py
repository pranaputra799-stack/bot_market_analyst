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


if __name__ == "__main__":
    unittest.main()
