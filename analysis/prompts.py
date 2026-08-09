"""
Prompt Templates — For each agent in the multi-agent analysis pipeline.
Adapted from MarketLens BTC's LLM prompt architecture with AutoHedge-inspired patterns.

Konten prompt DIAMBIL dari file `prompts/*.txt` (single source of truth) —
edit file .txt untuk mengubah perilaku agent TANPA mengubah kode. Lihat
prompts/loader.py untuk detail pemuatan & fallback.

Prompt engineering best practices applied (riset 2026):
- ROLE / persona spesifik per agent + target pembaca (trader retail Indonesia)
- ALUR BERPIKIR (chain-of-thought) eksplisit sebelum output
- Skema output JSON eksplisit dengan tipe data & penanganan null
- Guardrail anti-halusinasi data (harga, tanggal, event ekonomi)
- Larangan penggunaan simbol markdown (* / **) pada output — bot menampilkan plain text
- Instruksi bahasa & panjang jawaban yang jelas
"""

from datetime import datetime, timezone
from typing import Optional

from prompts.loader import load_prompt
from utils.token_budget import truncate_to_budget

# Aturan format output bersama untuk semua prompt: bot menampilkan plain text,
# jadi AI dilarang memakai simbol markdown yang bisa tampil mentah di Telegram.
NO_MARKDOWN_RULE = (
    "FORMAT OUTPUT: JANGAN gunakan simbol markdown (*, **, _, #) pada jawaban. "
    "Gunakan emoji, angka, bullet (•/-), dan baris baru untuk struktur. "
    "Jawab dalam Bahasa Indonesia yang santai namun profesional."
)


def with_timestamp(prompt: str) -> str:
    """Append current timestamp (UTC + WIB) to a system prompt."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    wib = ""
    try:
        from zoneinfo import ZoneInfo
        wib = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M WIB")
    except Exception:
        pass
    suffix = f"\n\nCurrent date and time: {now}"
    if wib:
        suffix += f" ({wib})"
    return prompt + suffix


# ═══════════════════════════════════════════════════════════════════════════════
# DIRECTOR — Orchestrator System Prompt
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/director_system.txt

DIRECTOR_SYSTEM = with_timestamp(
    load_prompt("director_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)


# ═══════════════════════════════════════════════════════════════════════════════
# RESEARCH AGENT — Market Context Gatherer
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/research_system.txt & prompts/research_analysis_template.txt

RESEARCH_SYSTEM = with_timestamp(
    load_prompt("research_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)

RESEARCH_ANALYSIS_TEMPLATE = load_prompt("research_analysis_template")


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNALS AGENT — Technical Signal Aggregator
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/signals_system.txt

SIGNALS_SYSTEM = with_timestamp(
    load_prompt("signals_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)


# ═══════════════════════════════════════════════════════════════════════════════
# THESIS AGENT — Analysis Thesis Formulator
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/thesis_system.txt & prompts/thesis_formulation_template.txt

THESIS_SYSTEM = with_timestamp(
    load_prompt("thesis_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)

THESIS_FORMULATION_TEMPLATE = load_prompt("thesis_formulation_template")


# ═══════════════════════════════════════════════════════════════════════════════
# CONTRADICTION AGENT — Detects Conflicting Signals
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/contradiction_system.txt & prompts/contradiction_template.txt

CONTRADICTION_SYSTEM = with_timestamp(
    load_prompt("contradiction_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)

CONTRADICTION_TEMPLATE = load_prompt("contradiction_template")


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIOS AGENT — Generates Market Scenarios
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/scenarios_system.txt & prompts/scenarios_template.txt

SCENARIOS_SYSTEM = with_timestamp(
    load_prompt("scenarios_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)

SCENARIOS_TEMPLATE = load_prompt("scenarios_template")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIDENCE AGENT — Scores Analysis Confidence
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/confidence_system.txt & prompts/confidence_template.txt

CONFIDENCE_SYSTEM = with_timestamp(
    load_prompt("confidence_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)

CONFIDENCE_TEMPLATE = load_prompt("confidence_template")


# ═══════════════════════════════════════════════════════════════════════════════
# RISK GATES — Educational Risk Assessment
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/risk_system.txt & prompts/risk_template.txt

RISK_SYSTEM = with_timestamp(
    load_prompt("risk_system").format(NO_MARKDOWN_RULE=NO_MARKDOWN_RULE)
)

RISK_TEMPLATE = load_prompt("risk_template")


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL SYNTHESIS — Director's Final Response
# ═══════════════════════════════════════════════════════════════════════════════
# Sumber: prompts/final_synthesis_template.txt

FINAL_SYNTHESIS_TEMPLATE = load_prompt("final_synthesis_template")


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY: Format helpers
# ═══════════════════════════════════════════════════════════════════════════════

def format_context_for_prompt(
    context_data: str,
    max_length: int = 3000,
    max_tokens: Optional[int] = None,
) -> str:
    """
    Format context data for prompt, truncating if needed.

    Args:
        context_data: Data konteks mentah
        max_length: Batas panjang karakter (dipakai bila max_tokens None)
        max_tokens: Bila diisi, potong berdasarkan BUDGET TOKEN (lebih akurat
            dari karakter — teknik tiktoken; lihat utils/token_budget.py)

    Returns:
        Teks konteks yang muat di batas (dengan marker terpotong bila perlu)
    """
    if not context_data:
        return "Tidak ada data konteks yang tersedia."
    if max_tokens is not None:
        return truncate_to_budget(context_data, max_tokens, "context")
    if len(context_data) > max_length:
        return context_data[:max_length] + "\n...[data truncated]"
    return context_data


def build_analysis_prompt(
    question: str,
    context_data: str = "",
    research_output: str = "",
    signal_output: str = "",
    indicators_output: str = "",
    thesis_output: str = "",
    contradiction_output: str = "",
    scenarios_output: str = "",
    confidence_output: str = "",
    risk_output: str = "",
    conversation_history: str = "",
) -> str:
    """Build the final synthesis prompt for the Director."""
    return FINAL_SYNTHESIS_TEMPLATE.format(
        question=question,
        conversation_history=conversation_history or "Tidak ada percakapan sebelumnya.",
        research_output=research_output or "Not analyzed",
        signal_output=signal_output or "Not analyzed",
        indicators_output=indicators_output or "Tidak ada data indikator — jangan mengarang angka RSI/MACD/level.",
        thesis_output=thesis_output or "Not analyzed",
        contradiction_output=contradiction_output or "Not analyzed",
        scenarios_output=scenarios_output or "Not analyzed",
        confidence_output=confidence_output or "Not analyzed",
        risk_output=risk_output or "Not analyzed",
        NO_MARKDOWN_RULE=NO_MARKDOWN_RULE,
    )
