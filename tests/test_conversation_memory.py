"""Unit tests untuk conversation memory per-user + integrasi prompt."""

import unittest

from data.cache import cache
from data import conversation_memory as cm
from analysis.prompts import (
    RESEARCH_ANALYSIS_TEMPLATE,
    build_analysis_prompt,
)


class TestConversationMemory(unittest.TestCase):
    def setUp(self):
        cm.clear(999)
        cm.clear(1000)

    def tearDown(self):
        cm.clear(999)
        cm.clear(1000)

    def test_memory_ttl_from_settings(self):
        """TTL riwayat diambil dari settings (default 24 jam = 86400 dtk)."""
        from config import settings

        self.assertEqual(cm.MEMORY_TTL, settings.MEMORY_TTL_SECONDS)
        self.assertEqual(settings.MEMORY_TTL_SECONDS, 24 * 60 * 60)

    def test_empty_history(self):
        self.assertEqual(cm.get_history(999), [])
        self.assertEqual(cm.format_history(999), "")

    def test_add_and_get_roundtrip(self):
        cm.add_exchange(999, "berapa harga eurusd?", "EUR/USD: 1.0850")
        history = cm.get_history(999)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["q"], "berapa harga eurusd?")
        self.assertIn("1.0850", history[0]["a"])

    def test_max_entries_from_settings(self):
        """Jumlah entri & pertukaran-ke-prompt diambil dari settings (default 10/6)."""
        from config import settings

        self.assertEqual(cm.MAX_ENTRIES, settings.MEMORY_MAX_ENTRIES)
        self.assertEqual(settings.MEMORY_MAX_ENTRIES, 10)
        self.assertEqual(cm.MAX_EXCHANGES_IN_CONTEXT, settings.MEMORY_MAX_EXCHANGES_IN_CONTEXT)
        self.assertEqual(settings.MEMORY_MAX_EXCHANGES_IN_CONTEXT, 6)

    def test_entries_capped(self):
        n = cm.MAX_ENTRIES + 2  # seed melebihi batas agar truncation benar-benar teruji
        for i in range(n):
            cm.add_exchange(999, f"q{i}", f"a{i}")
        history = cm.get_history(999)
        self.assertEqual(len(history), cm.MAX_ENTRIES)
        # Yang tersimpan harus paling baru
        self.assertEqual(history[-1]["q"], f"q{n - 1}")
        self.assertEqual(history[0]["q"], f"q{n - cm.MAX_ENTRIES}")

    def test_answer_truncated(self):
        long_answer = "x" * 1000
        cm.add_exchange(999, "q", long_answer)
        stored = cm.get_history(999)[0]["a"]
        self.assertLessEqual(len(stored), cm.MAX_ANSWER_CHARS)

    def test_format_history_contains_exchanges(self):
        cm.add_exchange(999, "q1", "a1")
        cm.add_exchange(999, "q2", "a2")
        text = cm.format_history(999)
        self.assertIn('User: "q1"', text)
        self.assertIn("Bot: a2", text)

    def test_format_history_limits_exchanges(self):
        for i in range(6):
            cm.add_exchange(999, f"q{i}", f"a{i}")
        text = cm.format_history(999, max_exchanges=2)
        self.assertNotIn("q0", text)
        self.assertIn("q5", text)

    def test_clear(self):
        cm.add_exchange(999, "q", "a")
        cm.clear(999)
        self.assertEqual(cm.get_history(999), [])

    def test_per_user_isolation(self):
        cm.add_exchange(999, "q-a", "a-a")
        cm.add_exchange(1000, "q-b", "a-b")
        self.assertEqual(cm.get_history(999)[0]["q"], "q-a")
        self.assertEqual(cm.get_history(1000)[0]["q"], "q-b")

    def test_cache_miss_returns_empty(self):
        cache.delete(cm._key(999))
        self.assertEqual(cm.get_history(999), [])


class TestContextTracking(unittest.TestCase):
    """Konteks multi-turn: fokus aset & arah tren agar follow-up ambigu
    ("support-nya berapa?", "bagaimana targetnya?") tetap punya konteks."""

    def setUp(self):
        cm.clear(2001)
        cm.clear(2002)

    def tearDown(self):
        cm.clear(2001)
        cm.clear(2002)

    def test_extract_asset_focus(self):
        self.assertEqual(cm.extract_asset_focus("analisis teknikal eurusd"), "EUR/USD")
        self.assertEqual(cm.extract_asset_focus("berapa harga gold?"), "XAU/USD (Gold)")
        self.assertEqual(cm.extract_asset_focus("harga emas sekarang"), "XAU/USD (Gold)")
        self.assertEqual(cm.extract_asset_focus("analisis usd/jpy"), "USD/JPY")
        self.assertIsNone(cm.extract_asset_focus("support-nya berapa?"))
        self.assertIsNone(cm.extract_asset_focus(None))

    def test_extract_asset_focus_no_false_positive(self):
        # Word boundary: "gold" ≠ "goldman", "eth" ≠ "method"
        self.assertIsNone(cm.extract_asset_focus("analisis saham goldman sachs"))
        self.assertIsNone(cm.extract_asset_focus("metode analisis teknikal"))

    def test_extract_trend_direction(self):
        self.assertEqual(cm.extract_trend_direction("Bias bullish, target naik ke 1.1000"), "bullish")
        self.assertEqual(cm.extract_trend_direction("tren turun, resistance kuat"), "bearish")
        self.assertEqual(cm.extract_trend_direction("harga bergerak sideways"), "sideways")
        self.assertIsNone(cm.extract_trend_direction("apa itu CPI?"))
        self.assertIsNone(cm.extract_trend_direction(None))

    def test_add_exchange_updates_context_automatically(self):
        cm.add_exchange(2001, "analisis teknikal eurusd", "Bias bullish. Support 1.0800, resistance 1.0950.")
        ctx = cm.get_context(2001)
        self.assertEqual(ctx["asset_focus"], "EUR/USD")
        self.assertEqual(ctx["direction"], "bullish")

    def test_set_context_keeps_existing_fields(self):
        cm.set_context(2002, asset_focus="XAU/USD (Gold)")
        cm.set_context(2002, direction="bearish")
        ctx = cm.get_context(2002)
        self.assertEqual(ctx["asset_focus"], "XAU/USD (Gold)")
        self.assertEqual(ctx["direction"], "bearish")

    def test_format_history_includes_context_section(self):
        cm.add_exchange(2001, "analisis teknikal eurusd", "Bias bullish, target 1.1000")
        text = cm.format_history(2001)
        self.assertIn("RECENT CONVERSATION CONTEXT", text)
        self.assertIn("Asset focus: EUR/USD", text)
        self.assertIn("Trend direction: bullish", text)

    def test_clear_removes_context(self):
        cm.add_exchange(2001, "analisis eurusd", "bullish")
        cm.clear(2001)
        self.assertEqual(cm.get_context(2001), {})


class TestPromptIntegration(unittest.TestCase):
    def test_research_template_accepts_history(self):
        prompt = RESEARCH_ANALYSIS_TEMPLATE.format(
            question="level support-nya di mana?",
            context_data="Data pasar",
            conversation_history='User: "analisis teknikal EUR/USD"\nBot: level 1.0800',
        )
        self.assertIn("PREVIOUS CONVERSATION CONTEXT", prompt)
        self.assertIn("EUR/USD", prompt)

    def test_research_template_default_history(self):
        prompt = RESEARCH_ANALYSIS_TEMPLATE.format(
            question="q",
            context_data="d",
            conversation_history="Tidak ada percakapan sebelumnya.",
        )
        self.assertIn("Tidak ada percakapan sebelumnya.", prompt)

    def test_build_analysis_prompt_includes_history(self):
        prompt = build_analysis_prompt(
            question="level support-nya?",
            conversation_history='User: "analisis teknikal EUR/USD"',
        )
        self.assertIn("CONVERSATION HISTORY", prompt)
        self.assertIn("EUR/USD", prompt)

    def test_build_analysis_prompt_default_history(self):
        prompt = build_analysis_prompt(question="q")
        self.assertIn("No previous conversation.", prompt)

    def test_legacy_build_prompt_injects_history(self):
        # _build_prompt tidak memakai state instance — aman tanpa __init__ penuh
        from bot.handlers import MarketBot

        bot = MarketBot.__new__(MarketBot)
        prompt = bot._build_prompt(
            "level support-nya di mana?",
            "DATA PASAR",
            'User: "analisis teknikal EUR/USD"',
        )
        self.assertIn("PREVIOUS CONVERSATION", prompt)
        self.assertIn("EUR/USD", prompt)

        # Tanpa history → section tidak muncul
        prompt2 = bot._build_prompt("q", "DATA PASAR")
        self.assertNotIn("PREVIOUS CONVERSATION", prompt2)


if __name__ == "__main__":
    unittest.main()
