"""Regresi: /morning harus tetap berjalan walau sumber data / AI gagal.

Latar: bot baru pertama kali jalan end-to-end — /morning gagal diam-diam saat
salah satu sumber data (Yahoo/FRED/news) ATAU AI error (multi-agent raise lalu
fallback legacy memakai ai.generate sync di luar try/except). Fix:
- asyncio.gather(..., return_exceptions=True) + fallback teks per-bagian
- fallback memakai generate_async (tidak blokir event loop) + try/except
- placeholder ramah saat AI gagal, bukan exception
"""

import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from bot.handlers import MarketBot


def _enter_mocks(*patchers):
    """Masukkan semua mock.patch ke ExitStack (return stack untuk `with`)."""
    stack = contextlib.ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


class FakeDirector:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def analyze(self, prompt):
        if self._error:
            raise self._error
        return self._result


class FakeAI:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def generate_async(self, prompt, **kw):
        if self._error:
            raise self._error
        return self._response


def _make_bot() -> MarketBot:
    return MarketBot()


class TestMorningBriefResilience(unittest.TestCase):
    def _brief(self, bot):
        return asyncio.run(bot._generate_morning_brief())

    def _mock_data(self, bot, market_ok=True, news_ok=True):
        market = (
            "📊 *RINGKASAN PASAR*\n🟢 *XAU/USD*: 2350.50 (+0.4%)"
            if market_ok else RuntimeError("yahoo down")
        )
        macro = "🏛️ Fed Funds Rate: 5.50%"
        calendar = [{
            "date": "2026-08-10", "time": "14:30", "currency": "USD",
            "event": "CPI YoY", "impact": "high", "forecast": "2.9%",
        }]
        news = "📰 Berita." if news_ok else RuntimeError("news down")
        return [
            mock.patch.object(bot.market, "get_market_summary", return_value=market),
            mock.patch.object(bot.macro, "get_macro_summary", return_value=macro),
            mock.patch.object(bot.macro, "get_economic_calendar", return_value=calendar),
            mock.patch.object(
                bot.macro, "format_calendar_text",
                return_value="• CPI YoY (14:30 WIB) — High",
            ),
            mock.patch.object(bot.news, "get_news_summary", return_value=news),
            mock.patch.object(bot, "_get_sentiment_text", return_value="Sentimen: 55% bullish"),
        ]

    def test_all_sources_ok_multi_agent(self):
        bot = _make_bot()
        director = FakeDirector(result=SimpleNamespace(
            final_response="OUTLOOK: Dolar melemah.\n\nKEY CATALYSTS: CPI AS."
        ))
        with _enter_mocks(*self._mock_data(bot)), \
                mock.patch.object(bot, "analysis_director", director):
            brief = self._brief(bot)
        self.assertIn("Dolar melemah", brief)
        self.assertIn("CPI AS", brief)
        self.assertIn("2350.50", brief)

    def test_one_source_fails_still_returns_brief(self):
        """Semua sumber data gagal + AI gagal → brief TETAP terkirim (placeholder)."""
        bot = _make_bot()
        with _enter_mocks(*self._mock_data(bot, market_ok=False, news_ok=False)), \
                mock.patch.object(
                    bot, "analysis_director",
                    FakeDirector(error=RuntimeError("multi-agent down")),
                ), \
                mock.patch.object(
                    bot.ai, "generate_async",
                    FakeAI(error=RuntimeError("all providers down")).generate_async,
                ):
            brief = self._brief(bot)
        self.assertIn("Data pasar tidak tersedia", brief)
        self.assertIn("Berita tidak tersedia", brief)
        self.assertIn("Analisis AI tidak tersedia", brief)
        self.assertIn("Fed Funds Rate", brief)  # bagian yang OK tetap muncul

    def test_ai_failure_uses_placeholder(self):
        """Multi-agent gagal → fallback legacy gagal → placeholder, bukan exception."""
        bot = _make_bot()
        with _enter_mocks(*self._mock_data(bot)), \
                mock.patch.object(
                    bot, "analysis_director",
                    FakeDirector(error=RuntimeError("multi-agent fail")),
                ), \
                mock.patch.object(
                    bot.ai, "generate_async",
                    FakeAI(error=RuntimeError("legacy fail")).generate_async,
                ):
            brief = self._brief(bot)
        self.assertIn("Analisis AI tidak tersedia", brief)
        self.assertIn("CPI YoY", brief)  # kalender tetap tampil

    def test_split_outlook_catalysts(self):
        out, cat = MarketBot._split_outlook_catalysts(
            "OUTLOOK: Gold menguat.\n\nKEY CATALYSTS: NFP Jumat."
        )
        self.assertEqual(out, "Gold menguat.")
        self.assertEqual(cat, "NFP Jumat.")

        out2, cat2 = MarketBot._split_outlook_catalysts("Hanya outlook.")
        self.assertEqual(out2, "Hanya outlook.")
        self.assertEqual(cat2, "Belum ada katalis utama yang teridentifikasi hari ini.")

        out3, cat3 = MarketBot._split_outlook_catalysts("")
        self.assertIn("Belum ada data analisis", out3)
        self.assertIn("Belum ada katalis", cat3)


if __name__ == "__main__":
    unittest.main()
