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

    def test_empty_history(self):
        self.assertEqual(cm.get_history(999), [])
        self.assertEqual(cm.format_history(999), "")

    def test_add_and_get_roundtrip(self):
        cm.add_exchange(999, "berapa harga eurusd?", "EUR/USD: 1.0850")
        history = cm.get_history(999)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["q"], "berapa harga eurusd?")
        self.assertIn("1.0850", history[0]["a"])

    def test_entries_capped(self):
        for i in range(10):
            cm.add_exchange(999, f"q{i}", f"a{i}")
        history = cm.get_history(999)
        self.assertEqual(len(history), cm.MAX_ENTRIES)
        # Yang tersimpan harus paling baru
        self.assertEqual(history[-1]["q"], "q9")
        self.assertEqual(history[0]["q"], f"q{10 - cm.MAX_ENTRIES}")

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


class TestPromptIntegration(unittest.TestCase):
    def test_research_template_accepts_history(self):
        prompt = RESEARCH_ANALYSIS_TEMPLATE.format(
            question="level support-nya di mana?",
            context_data="Data pasar",
            conversation_history='User: "analisis teknikal EUR/USD"\nBot: level 1.0800',
        )
        self.assertIn("KONTEKS PERCAKAPAN SEBELUMNYA", prompt)
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
        self.assertIn("Tidak ada percakapan sebelumnya.", prompt)

    def test_legacy_build_prompt_injects_history(self):
        # _build_prompt tidak memakai state instance — aman tanpa __init__ penuh
        from bot.handlers import MarketBot

        bot = MarketBot.__new__(MarketBot)
        prompt = bot._build_prompt(
            "level support-nya di mana?",
            "DATA PASAR",
            'User: "analisis teknikal EUR/USD"',
        )
        self.assertIn("PERCAKAPAN SEBELUMNYA", prompt)
        self.assertIn("EUR/USD", prompt)

        # Tanpa history → section tidak muncul
        prompt2 = bot._build_prompt("q", "DATA PASAR")
        self.assertNotIn("PERCAKAPAN SEBELUMNYA", prompt2)


if __name__ == "__main__":
    unittest.main()
