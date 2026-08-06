"""Unit tests untuk prompts/loader.py (single source of truth prompt) + wiring ke handlers."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prompts import loader
from prompts.loader import format_prompt, load_prompt, reload_prompts

# Semua placeholder yang harus diisi untuk setiap template (agar output bebas `{`).
_FULL_KWARGS = {
    "market_analysis": dict(
        QUESTION="analisis eurusd",
        USER_QUESTION="analisis eurusd",
        CONTEXT="DATA PASAR",
        CONVERSATION_HISTORY="",
        CURRENT_TIME="2026-08-06 09:00 WIB",
        INTENT_INSTRUCTION="",
        INSTRUMENT="EUR/USD",
        INSTRUMENTS="EUR/USD",
    ),
    "technical_analysis": dict(
        QUESTION="korelasi gold dan dxy",
        USER_QUESTION="korelasi gold dan dxy",
        CONTEXT="DATA PASAR",
        CONVERSATION_HISTORY="",
        CURRENT_TIME="2026-08-06 09:00 WIB",
        INTENT_INSTRUCTION="",
        INSTRUMENT="Gold",
        INSTRUMENTS="Gold, DXY",
    ),
    "macro_explanation": dict(
        QUESTION="apa dampak cpi?",
        USER_QUESTION="apa dampak cpi?",
        CONTEXT="DATA MAKRO",
        CONVERSATION_HISTORY="",
        CURRENT_TIME="2026-08-06 09:00 WIB",
        INTENT_INSTRUCTION="",
        INSTRUMENT="Pasar",
        INSTRUMENTS="Pasar",
    ),
    "morning_brief": dict(
        DATE="Kamis, 06 Agustus 2026",
        market_data="DATA PASAR",
        macro_data="DATA MAKRO",
        calendar_data="KALENDER",
        news_data="BERITA",
        sentiment_data="+0.4",
    ),
}


class TestPromptLoader(unittest.TestCase):
    def tearDown(self):
        reload_prompts()  # jangan bocorkan cache antar test

    def test_all_prompts_loadable(self):
        for name in loader.prompt_names():
            text = load_prompt(name)
            self.assertIsInstance(text, str)
            self.assertGreater(len(text), 150, f"prompt {name} terlalu pendek")

    def test_format_prompt_fills_placeholders(self):
        for name, kwargs in _FULL_KWARGS.items():
            prompt = format_prompt(name, **kwargs)
            context_value = kwargs.get("CONTEXT") or kwargs.get("market_data")
            self.assertIn(context_value, prompt)  # data konteks tersisip
            self.assertIn(kwargs.get("CURRENT_TIME", kwargs.get("DATE", "")), prompt)
            self.assertNotIn("{", prompt, f"placeholder tersisa di {name}")
            self.assertNotIn("}", prompt, f"placeholder tersisa di {name}")

    def test_missing_placeholder_does_not_crash(self):
        # Placeholder yang tidak diisi → string kosong (bukan exception)
        prompt = format_prompt("market_analysis", QUESTION="q")
        self.assertIn('"q"', prompt)
        self.assertNotIn("{QUESTION}", prompt)

    def test_fallback_when_file_missing(self):
        # File .txt tidak ada → fallback ke DEFAULT_PROMPTS (konten identik)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(loader, "PROMPTS_DIR", Path(tmp)):
                reload_prompts()
                text = load_prompt("morning_brief")
                self.assertGreater(len(text), 150)
                self.assertIn("OUTLOOK:", text)
                reload_prompts()

    def test_reload_picks_up_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "market_analysis.txt").write_text("VERSI 1 {QUESTION}", encoding="utf-8")
            with mock.patch.object(loader, "PROMPTS_DIR", tmp_dir):
                reload_prompts()
                self.assertEqual(format_prompt("market_analysis", QUESTION="x"), "VERSI 1 x")
                # Edit file → setelah reload cache, konten baru terpakai
                (tmp_dir / "market_analysis.txt").write_text("VERSI 2 {QUESTION}", encoding="utf-8")
                self.assertEqual(format_prompt("market_analysis", QUESTION="x"), "VERSI 1 x")  # cache
                reload_prompts()
                self.assertEqual(format_prompt("market_analysis", QUESTION="x"), "VERSI 2 x")
                reload_prompts()

    def test_morning_brief_has_parser_markers(self):
        # Parser _generate_morning_brief memotong di marker ini — wajib ada
        prompt = format_prompt("morning_brief", **_FULL_KWARGS["morning_brief"])
        self.assertIn("OUTLOOK:", prompt)
        self.assertIn("KATALIS UTAMA:", prompt)

    def test_defaults_in_sync_with_files(self):
        # DEFAULT_PROMPTS harus identik dengan isi file .txt (fallback darurat
        # tidak boleh melenceng dari sumber kebenaran).
        for name in loader.prompt_names():
            file_text = (loader.PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
            self.assertEqual(
                file_text.strip(),
                loader.DEFAULT_PROMPTS[name].strip(),
                f"DEFAULT_PROMPTS[{name}] tidak sinkron dengan {name}.txt",
            )


class TestPromptWiring(unittest.TestCase):
    """Verifikasi handlers memakai loader (single source of truth)."""

    def _bot(self):
        from bot.handlers import MarketBot

        return MarketBot.__new__(MarketBot)  # tanpa __init__ — tanpa state instance

    def test_build_morning_brief_prompt_uses_template(self):
        bot = self._bot()
        prompt = bot._build_morning_brief_prompt(
            today="Kamis, 06 Agustus 2026",
            market_summary="EUR/USD 1.0850",
            macro_summary="CPI 3.2%",
            calendar_text="NFP 15:30 WIB",
            news_summary="Berita",
            sentiment_text="+0.4",
        )
        self.assertIn("Kamis, 06 Agustus 2026", prompt)
        self.assertIn("EUR/USD 1.0850", prompt)
        self.assertIn("CPI 3.2%", prompt)
        self.assertIn("NFP 15:30 WIB", prompt)
        self.assertIn("Berita", prompt)
        self.assertIn("+0.4", prompt)
        self.assertIn("KATALIS UTAMA:", prompt)
        self.assertNotIn("{", prompt)

    def test_build_morning_brief_prompt_sentiment_default(self):
        bot = self._bot()
        prompt = bot._build_morning_brief_prompt(
            today="x", market_summary="m", macro_summary="mc",
            calendar_text="c", news_summary="n",
        )
        self.assertIn("Sentimen pasar tidak tersedia.", prompt)

    def test_build_prompt_selects_template_by_intent(self):
        bot = self._bot()
        # Intent makro → template Chief Economist
        macro = bot._build_prompt("apa dampak cpi ke pasar?", "DATA MAKRO")
        self.assertIn("Chief Economist", macro)
        self.assertIn("DATA MAKRO", macro)
        # Intent korelasi → template Global Macro Strategist
        corr = bot._build_prompt("korelasi antara gold dan dxy?", "DATA PASAR")
        self.assertIn("Global Macro Strategist", corr)
        # Default (teknikal/market) → template Technical Analyst CMT
        default = bot._build_prompt("analisis teknikal eurusd", "DATA PASAR")
        self.assertIn("Technical Analyst", default)

    def test_build_prompt_no_stray_placeholders(self):
        bot = self._bot()
        for q in ["apa itu inflasi?", "analisis eurusd", "korelasi gold dxy", "q"]:
            prompt = bot._build_prompt(q, "DATA")
            self.assertNotIn("{", prompt, f"placeholder tersisa untuk: {q}")


if __name__ == "__main__":
    unittest.main()
