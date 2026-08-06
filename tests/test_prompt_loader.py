"""Unit tests untuk prompts/loader.py (single source of truth prompt) + wiring ke handlers."""

import re
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


# Semua placeholder yang dipakai template agent multi-agent
AGENT_KWARGS = dict(
    question="q", context_data="d", conversation_history="h",
    research_output="r", signal_output="s", indicators_output="i",
    thesis_output="t", contradiction_output="c", scenarios_output="sc",
    confidence_output="cf", risk_output="rk", market_data="m",
    NO_MARKDOWN_RULE="NO",
)

AGENT_NAMES = [
    "director_system", "research_system", "research_analysis_template",
    "signals_system", "thesis_system", "thesis_formulation_template",
    "contradiction_system", "contradiction_template", "scenarios_system",
    "scenarios_template", "confidence_system", "confidence_template",
    "risk_system", "risk_template", "final_synthesis_template",
]


# Placeholder {identifier} yang belum terisi (kurung kurawal JSON skema TIDAK
# dihitung — itu bagian dari contoh output, bukan placeholder).
_UNFILLED = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


class TestAgentPrompts(unittest.TestCase):
    """Template agent multi-agent (dipindah ke prompts/*.txt) tetap berfungsi."""

    def test_agent_prompts_render_all_placeholders_filled(self):
        for name in AGENT_NAMES:
            prompt = format_prompt(name, **AGENT_KWARGS)
            self.assertGreater(len(prompt), 100, f"{name} terlalu pendek")
            self.assertIsNone(
                _UNFILLED.search(prompt), f"placeholder belum terisi: {name}"
            )

    def test_agent_system_prompts_have_timestamp_suffix(self):
        # system prompt agent diformat dengan NO_MARKDOWN_RULE + timestamp saat import
        from analysis.prompts import (
            DIRECTOR_SYSTEM, RESEARCH_SYSTEM, THESIS_SYSTEM,
        )
        for sp in (DIRECTOR_SYSTEM, RESEARCH_SYSTEM, THESIS_SYSTEM):
            self.assertIn("Current date and time:", sp)
            self.assertNotIn("{NO_MARKDOWN_RULE}", sp)  # placeholder sudah diformat

    def test_agent_templates_format_at_call_site(self):
        # Template (bukan system prompt) tetap bisa di-.format seperti sebelum refactor
        from analysis.prompts import RESEARCH_ANALYSIS_TEMPLATE, FINAL_SYNTHESIS_TEMPLATE

        p = RESEARCH_ANALYSIS_TEMPLATE.format(
            question="level support-nya di mana?",
            context_data="Data pasar",
            conversation_history="User: analisis EUR/USD",
        )
        self.assertIn("KONTEKS PERCAKAPAN SEBELUMNYA", p)
        self.assertIn("EUR/USD", p)
        self.assertIn('"price_context"', p)  # skema JSON tetap utuh ({{ }} → { })
        self.assertNotIn("{question}", p)
        self.assertNotIn("{context_data}", p)

        f = FINAL_SYNTHESIS_TEMPLATE.format(
            question="q", conversation_history="h", research_output="r",
            signal_output="s", indicators_output="i", thesis_output="t",
            contradiction_output="c", scenarios_output="sc",
            confidence_output="cf", risk_output="rk", NO_MARKDOWN_RULE="NO",
        )
        self.assertNotIn("{question}", f)
        self.assertNotIn("{NO_MARKDOWN_RULE}", f)


class TestPromptCLI(unittest.TestCase):
    """Dev CLI: python -m prompts.loader --show/--list/--sample/--data."""

    def test_cli_list_lists_all(self):
        out = loader.cli(["--list"])
        for name in loader.prompt_names():
            self.assertIn(name, out)

    def test_cli_show_raw_keeps_placeholders(self):
        out = loader.cli(["--show", "morning_brief"])
        self.assertIn("{DATE}", out)
        self.assertIn("{market_data}", out)

    def test_cli_show_sample_fills_placeholders(self):
        out = loader.cli(["--show", "market_analysis", "--sample"])
        self.assertIn("Analisis teknikal EUR/USD hari ini?", out)  # dari SAMPLE_DATA
        self.assertNotIn("{QUESTION}", out)
        self.assertNotIn("{CONTEXT}", out)

    def test_cli_show_agent_system_sample_matches_production(self):
        # System prompt agent → sama persis dgn konstanta produksi (timestamp + rule)
        from analysis.prompts import DIRECTOR_SYSTEM, RESEARCH_SYSTEM

        out = loader.cli(["--show", "director_system", "--sample"])
        self.assertIn("Analysis Director", out)
        self.assertIn("Current date and time:", out)
        self.assertEqual(out, DIRECTOR_SYSTEM)
        self.assertEqual(loader.cli(["--show", "research_system", "--sample"]), RESEARCH_SYSTEM)

    def test_cli_data_override(self):
        out = loader.cli(["--show", "market_analysis", "--sample", "--data", "QUESTION=Test?"])
        self.assertIn('"Test?"', out)
        self.assertNotIn("{QUESTION}", out)

    def test_cli_unknown_name(self):
        with self.assertRaises(SystemExit) as ctx:
            loader.cli(["--show", "nope"])
        self.assertIn("tidak dikenal", str(ctx.exception))
        self.assertIn("market_analysis", str(ctx.exception))  # daftar template valid

    def test_cli_invalid_data_format(self):
        with self.assertRaises(SystemExit) as ctx:
            loader.cli(["--show", "market_analysis", "--data", "BAD"])
        self.assertIn("KEY=VALUE", str(ctx.exception))

    def test_cli_no_args_prints_usage(self):
        out = loader.cli([])
        self.assertIn("--show", out)

    def test_render_preview_covers_all_templates(self):
        # Semua template harus bisa di-render dgn data contoh tanpa placeholder tersisa
        for name in loader.prompt_names():
            prompt = loader.render_preview(name)
            self.assertGreater(len(prompt), 50, f"{name} terlalu pendek")
            self.assertIsNone(_UNFILLED.search(prompt), f"placeholder belum terisi: {name}")

    def test_render_preview_unknown_name(self):
        with self.assertRaises(ValueError):
            loader.render_preview("nope")

    def test_python_m_entrypoint(self):
        # Verifikasi `python -m prompts.loader` benar-benar jalan (cwd = root repo)
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-m", "prompts.loader", "--list"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            cwd=str(repo_root),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("market_analysis", proc.stdout)

    def test_python_m_entrypoint_show_sample(self):
        # Preview ter-render (emoji UTF-8) tidak crash walau console cp1252
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-m", "prompts.loader", "--show", "morning_brief", "--sample"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            cwd=str(repo_root),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("📊", proc.stdout)
        self.assertNotIn("{DATE}", proc.stdout)


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
