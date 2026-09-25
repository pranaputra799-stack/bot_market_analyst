"""Unit tests untuk perintah /news & klien ForexFactory (tanpa network).

Menyentuh fungsi murni/normalisasi:
- MarketCommandsMixin._format_forexfactory_news & _ff_md_escape
- ForexFactoryClient._normalize_event
- ForexFactoryClient.get_news (dedup, dengan _call di-stub)
"""

import unittest
from datetime import datetime

from bot.commands_market import MarketCommandsMixin, _ff_md_escape
from data.forexfactory_client import ForexFactoryClient


class TestFormatForexFactoryNews(unittest.TestCase):
    def test_basic_format(self):
        articles = [{
            "title": "Gold rallies on Fed cut bets",
            "description": "Harga emas naik.",
            "url": "https://www.forexfactory.com/news/1",
            "impact": "high",
        }]
        text = MarketCommandsMixin._format_forexfactory_news(articles)
        self.assertIn("📰 *BERITA FOREXFACTORY*", text)
        self.assertIn("Gold rallies on Fed cut bets", text)
        self.assertIn("🔥", text)  # tag high impact
        self.assertIn("https://www.forexfactory.com/news/1", text)
        self.assertIn("1. ", text)

    def test_impact_tags(self):
        text = MarketCommandsMixin._format_forexfactory_news([
            {"title": "High", "impact": "high", "url": ""},
            {"title": "Med", "impact": "medium", "url": ""},
            {"title": "Low", "impact": "low", "url": ""},
        ])
        self.assertIn("High* 🔥", text)
        self.assertIn("Med* ⚠️", text)
        self.assertIn("Low*\n", text)

    def test_keyword_label(self):
        text = MarketCommandsMixin._format_forexfactory_news([], keyword="gold")
        self.assertIn("Filter:", text)
        self.assertIn("gold", text)

    def test_missing_description_and_url(self):
        text = MarketCommandsMixin._format_forexfactory_news([
            {"title": "Only title", "impact": "low"},
        ])
        self.assertIn("Only title", text)
        self.assertNotIn("Baca selengkapnya", text)

    def test_markdown_escaping(self):
        self.assertEqual(_ff_md_escape("a_b*c`d[e"), "a\\_b\\*c\\`d\\[e")
        text = MarketCommandsMixin._format_forexfactory_news([
            {"title": "US_Dollar *rally*", "impact": "low"},
        ])
        self.assertIn("US\\_Dollar \\*rally\\*", text)


class TestNormalizeEvent(unittest.TestCase):
    def setUp(self):
        self.client = ForexFactoryClient()
        self.week_start = datetime(2026, 9, 21)

    def test_forecast_previous_mapping_and_wib(self):
        ev = self.client._normalize_event({
            "id": 1,
            "date": "Wed Sep 23",
            "time": "1:30am",
            "currency": "AUD",
            "impact": "high",
            "name": "Employment Change",
            "actual": "39.5K",
            "forecast": "21.5K",
            "previous": "-15.8K",
            "country": "AU",
            "event_time_utc": "2026-09-24T01:30:00Z",
        }, self.week_start)
        self.assertEqual(ev["event"], "Employment Change")
        self.assertEqual(ev["estimate"], "21.5K")   # forecast -> estimate
        self.assertEqual(ev["prev"], "-15.8K")        # previous -> prev
        self.assertEqual(ev["actual"], "39.5K")
        self.assertEqual(ev["source"], "forexfactory")
        self.assertEqual(ev["country_emoji"], "🇦🇺")
        # 01:30 UTC -> 08:30 WIB
        self.assertIn("08:30 WIB", ev["time"])
        self.assertEqual(ev["_dt_utc"].hour, 1)

    def test_empty_values_become_none(self):
        ev = self.client._normalize_event({
            "name": "BOE Gov Bailey Speaks",
            "impact": "high",
            "country": "UK",
            "actual": "",
            "forecast": "",
            "previous": "",
            "event_time_utc": "2026-09-25T09:15:00Z",
        }, self.week_start)
        self.assertIsNone(ev["actual"])
        self.assertIsNone(ev["estimate"])
        self.assertIsNone(ev["prev"])

    def test_holiday_grouped_as_low(self):
        ev = self.client._normalize_event({
            "name": "Bank Holiday",
            "impact": "holiday",
            "country": "JN",
            "date": "Mon Sep 21",
        }, self.week_start)
        # format_calendar_text hanya mengenal high/medium/low
        self.assertEqual(ev["impact"], "low")
        self.assertIn("HOLIDAY", ev["impact_label"])

    def test_nameless_event_ignored(self):
        self.assertIsNone(self.client._normalize_event({"impact": "high"}, self.week_start))


class TestNewsDedup(unittest.IsolatedAsyncioTestCase):
    async def test_dedup_by_url(self):
        client = ForexFactoryClient()
        client.api_key = "test"
        client.scraper_id = "test"
        client.force_enabled = True

        async def fake_call(endpoint, params=None):
            return {"stories": [
                {"headline": "A", "url": "u1", "preview": "p", "impact": "low"},
                {"headline": "A (dup)", "url": "u1", "preview": "p", "impact": "low"},
                {"headline": "B", "url": "u2", "preview": "p", "impact": "low"},
            ]}

        client._call = fake_call
        result = await client.get_news(limit=5)
        self.assertEqual([a["title"] for a in result["articles"]], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
