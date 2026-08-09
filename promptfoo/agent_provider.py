"""
Promptfoo Python Provider — evaluasi kualitas prompt bot via pipeline NYATA.

Menjalankan provider custom untuk promptfoo (https://promptfoo.dev/docs/providers/python/).
Setiap fungsi menerima `(prompt, options, context)` dan mengembalikan dict
dengan minimal kunci "output" (string atau JSON).

Yang dievaluasi:
1. call_api        — Sintesis akhir (DIRECTOR_SYSTEM + build_analysis_prompt)
                      lewat AIFallbackEngine (OpenRouter free → Groq → Gemini, dll).
                      Assertion: jawaban mengandung instrumen, tanpa markdown
                      rusak (**), dan panjang memadai.
2. call_confidence — Agent Confidence (JSON): menjalankan prompt + parsing JSON
                      nyata. Assertion: is-json dengan skema {overall, level, ...}.

PENTING: butuh minimal satu API key AI (OPENROUTER_API_KEY direkomendasikan).
Set data policy OpenRouter ke "Allow all" agar model free bisa dipakai.
"""

import asyncio
import os
import sys
import time

# Provider dijalankan dari direktori promptfoo/ → tambahkan root proyek ke
# sys.path agar import modul bot berfungsi.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai.engine import AIFallbackEngine  # noqa: E402
from analysis.prompts import DIRECTOR_SYSTEM, build_analysis_prompt  # noqa: E402
from analysis.confidence_agent import ConfidenceAgent  # noqa: E402

_engine = None


def _get_engine() -> AIFallbackEngine:
    """Engine singleton — dibuat sekali per proses promptfoo (worker)."""
    global _engine
    if _engine is None:
        _engine = AIFallbackEngine()
    return _engine


def _strip_provider_prefix(text: str) -> str:
    """Hapus prefix '[via Provider] 🤖' yang ditambahkan engine (kalau ada)."""
    if "[via" in text:
        parts = text.split("\n\n", 1)
        return parts[1] if len(parts) > 1 else text
    return text


# Data agent realistis per simbol — eval ini menguji KUALITAS SINTESIS
# (template final_synthesis + DIRECTOR_SYSTEM + LLM nyata), bukan pipeline
# pengambilan data (yang butuh network & tak stabil di CI). Tanpa data ini
# template diisi "Not analyzed" → LLM jujur bilang tidak bisa menganalisis.
_SYNTH_DATA = {
    "EUR/USD": {
        "research_output": "EUR/USD diperdagangkan 1.0850, uptrend jangka pendek. "
            "DXY 104.2 melemah -0.2%. Data makro: CPI Zona Euro 2.4% vs forecast 2.5%, "
            "ECB berpeluang dovish.",
        "signal_output": "RSI 62 (bullish), EMA 20 di atas EMA 50, MACD positif, "
            "harga bertahan di atas support.",
        "indicators_output": "RSI 62, EMA20 1.0840, EMA50 1.0810, Bollinger atas 1.0920 "
            "bawah 1.0780, ATR 40 pip.",
        "thesis_output": "Bias naik menuju resistance 1.0920; base case konsolidasi "
            "1.0820-1.0920.",
        "contradiction_output": "Momentum bullish vs DXY yang masih kokoh — risiko "
            "koreksi bila DXY rebound.",
        "scenarios_output": "Bull: 1.0920 (prob 45%), Bear: 1.0780 (25%), Base: 1.0850 (30%).",
        "confidence_output": "Skor 0.70/1 — keyakinan sedang; data lengkap tapi ada "
            "kontradiksi DXY.",
        "risk_output": "Risiko: rilis CPI AS malam ini; stop-loss di bawah 1.0800.",
    },
    "XAU/USD": {
        "research_output": "Gold (XAU/USD) diperdagangkan 2412.50, uptrend. DXY 104.2 "
            "melemah mendukung emas. Data makro: CPI AS 3.0% vs forecast 3.1%, "
            "yield 10Y AS 4.2%.",
        "signal_output": "RSI 58 (bullish moderat), EMA 20 di atas EMA 50, MACD positif, "
            "harga di atas support 2385.",
        "indicators_output": "RSI 58, EMA20 2401, EMA50 2388, Bollinger atas 2430 "
            "bawah 2385.",
        "thesis_output": "Bias naik menuju resistance 2430; base case konsolidasi "
            "2390-2430.",
        "contradiction_output": "Tidak ada kontradiksi signifikan; yield naik tipis bisa "
            "menahan kenaikan emas.",
        "scenarios_output": "Bull: 2430 (prob 50%), Bear: 2390 (25%), Base: 2412 (25%).",
        "confidence_output": "Skor 0.75/1 — keyakinan sedang-tinggi; tren & makro sejalan.",
        "risk_output": "Risiko: data NFP lebih kuat dari ekspektasi; stop-loss di bawah 2385.",
    },
}


def _pick_synth(symbol: str) -> dict:
    q = (symbol or "").lower()
    if "xau" in q or "gold" in q:
        return _SYNTH_DATA["XAU/USD"]
    return _SYNTH_DATA["EUR/USD"]


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """
    Evaluasi sintesis akhir: prompt = pertanyaan user (hasil render template).

    Memakai pipeline nyata: build_analysis_prompt (template final_synthesis
    + data agent per simbol) + DIRECTOR_SYSTEM → AIFallbackEngine.generate
    (tanpa cache agar tiap eval benar-benar menguji model).
    """
    question = (prompt or "").strip()
    if not question:
        return {"output": "", "error": "prompt kosong"}
    started = time.time()
    try:
        data = _pick_synth(question)
        user_prompt = build_analysis_prompt(question, **data)
        raw = _get_engine().generate(
            user_prompt,
            system_override=DIRECTOR_SYSTEM,
            max_tokens=1024,
            use_cache=False,
        )
        return {
            "output": _strip_provider_prefix(raw or ""),
            "latencyMs": int((time.time() - started) * 1000),
            "cost": 0,
        }
    except Exception as e:
        return {
            "output": "",
            "error": str(e),
            "latencyMs": int((time.time() - started) * 1000),
        }


def call_confidence(prompt: str, options: dict, context: dict) -> dict:
    """
    Evaluasi agent Confidence (JSON): jalankan calibrate() dengan data contoh
    realistis, output = dict skor → promptfoo memvalidasi is-json + skema.

    Ini menguji dua hal sekaligus: kualitas prompt confidence DAN ketahanan
    parser JSON (clean_json_response) terhadap respons model.
    """
    question = (prompt or "").strip() or "Analisis EUR/USD sekarang"
    started = time.time()
    try:
        agent = ConfidenceAgent(_get_engine())
        score = asyncio.run(agent.calibrate(
            question,
            has_research_data=True,
            research_output=(
                "EUR/USD berada dalam uptrend jangka pendek; data makro CPI "
                "mendukung USD. Level kunci: support 1.0820, resistance 1.0920."
            ),
            signal_output="RSI 62 (bullish), EMA 20 di atas EMA 50 (bullish), MACD positif.",
            contradiction_output="Tidak ada kontradiksi signifikan.",
            scenarios_output="Skenario bull: 1.0920; skenario bear: 1.0780; base: 1.0850.",
            thesis_output="Target konsolidasi 1.0850-1.0920 dengan bias naik.",
        ))
        return {
            "output": score.to_dict(),
            "latencyMs": int((time.time() - started) * 1000),
            "cost": 0,
        }
    except Exception as e:
        return {
            "output": {},
            "error": str(e),
            "latencyMs": int((time.time() - started) * 1000),
        }
