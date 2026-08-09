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


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """
    Evaluasi sintesis akhir: prompt = pertanyaan user (hasil render template).

    Memakai pipeline nyata: build_analysis_prompt (template final_synthesis)
    + DIRECTOR_SYSTEM → AIFallbackEngine.generate (tanpa cache agar tiap eval
    benar-benar menguji model).
    """
    question = (prompt or "").strip()
    if not question:
        return {"output": "", "error": "prompt kosong"}
    started = time.time()
    try:
        user_prompt = build_analysis_prompt(question, context_data="")
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
