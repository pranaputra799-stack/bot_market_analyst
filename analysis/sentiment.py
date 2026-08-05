"""
Sentiment Analyzer — Analisis sentimen pasar berbasis berita.
Skor: -1.0 (sangat bearish) s/d +1.0 (sangat bullish).

Metode multi-sumber (weighted):
1. Finnhub sentiment score (dari API, bila key tersedia)
2. Lexicon scoring (kata bullish/bearish di headline + summary)
3. LLM refinement (AI engine, opsional) — menilai konteks & relevansi per instrumen

Hasil di-cache 10 menit per simbol agar tidak membanjiri API berita.
"""

import asyncio
import json
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from data.cache import cache, clean_json_response

logger = logging.getLogger(__name__)

# ===================== SENTIMENT PROMPTS =====================

SENTIMENT_SYSTEM = (
    "ROLE:\n"
    "Anda adalah analis sentimen pasar keuangan. Tugas: membaca daftar headline berita dan "
    "menentukan skor sentimen terhadap INSTRUMEN yang ditanyakan (bukan sentimen umum dunia).\n\n"
    "ALUR BERPIKIR:\n"
    "1. Baca setiap headline beserta skor awal dari sistem.\n"
    "2. Nilai apakah isi berita positif/negatif/netral UNTUK instrumen tersebut.\n"
    "3. Perhatikan relevansi, kualitas sumber, dan waktu berita.\n"
    "4. Susun skor akhir + driver utama + assessment singkat.\n\n"
    "ATURAN:\n"
    "- Skala skor: -1.0 (sangat bearish) sampai +1.0 (sangat bullish).\n"
    "- JANGAN mengarang berita yang tidak ada di daftar.\n"
    "- Jika berita tidak cukup atau tidak relevan, beri skor mendekati 0 dan confidence rendah.\n"
    "- Jawab dalam Bahasa Indonesia.\n"
    "- FORMAT OUTPUT: JANGAN gunakan simbol markdown (*, **, _, #)."
)

SENTIMENT_TEMPLATE = """\
INSTRUMEN: {instrument}
TANGGAL: {date}

DAFTAR BERITA (headline | skor awal sistem):
{articles}

Berikan analisis dalam JSON yang VALID, sesuai skema:
{{
    "score": float -1.0 s/d +1.0,
    "label": "string — sangat_bearish|bearish|netral|bullish|sangat_bullish",
    "bull_drivers": ["string", ...] — 1-3 faktor bullish (null jika tidak ada),
    "bear_drivers": ["string", ...] — 1-3 faktor bearish (null jika tidak ada),
    "assessment": "string — ringkasan sentimen 1-2 kalimat Bahasa Indonesia",
    "confidence": float 0.0-1.0 — seberapa yakin terhadap skor (jumlah & relevansi berita)
}}

Jawab HANYA JSON tanpa teks lain. Jangan pakai simbol * atau **.
"""

# ===================== LEXICON =====================

_BULLISH_WORDS = [
    "naik", "menguat", "melompat", "rally", "surges", "surge", "gains", "gain",
    "rises", "rise", "higher", "bullish", "optimis", "optimistic", "positif",
    "membaik", "rebound", "bounce", "dovish", "pelonggaran", "stimulus",
    "turunkan suku bunga", "cut rates", "rate cut", "kenaikan", "penguatan",
    "breakout", "terbang", "harapan", "diprediksi naik", "support",
    "all-time high", "record high", "kuat", "melesat", "mencetak rekor",
    "prospek cerah", "tumbuh", "pertumbuhan",
]

_BEARISH_WORDS = [
    "turun", "melemah", "anjlok", "jeblok", "drop", "falls", "fall", "decline",
    "lower", "bearish", "pesimis", "pessimistic", "negatif", "memburuk",
    "selloff", "sell-off", "crash", "hawkish", "kenaikan suku bunga", "hike",
    "resesi", "recession", "krisis", "crisis", "penurunan", "pelemahan",
    "breakdown", "panic", "kekhawatiran", "tekanan", "ancaman", "threat",
    "tank", "slump", "tertekan", "merosot", "jauh dari", "kehilangan",
    "kerugian", "loss", "worsen", "berisiko", "risk",
]


# ===================== ANALYZER =====================

class SentimentAnalyzer:
    """
    Menghitung skor sentimen pasar (-1 s/d +1) dari berita terkini.
    """

    def __init__(self, ai_engine: Any = None, news_fetcher: Any = None):
        self.ai = ai_engine
        self.news_fetcher = news_fetcher

    # ── Public API ───────────────────────────────────────────────

    async def analyze(self, symbol: str = "FOREX", use_llm: bool = True) -> Dict:
        """
        Analisis sentimen untuk simbol tertentu.

        Args:
            symbol: Simbol Yahoo/berita (FOREX, EURUSD=X, GC=F, ...)
            use_llm: Gunakan LLM untuk refinement skor (lebih akurat)

        Returns:
            Dict: {score, label, confidence, count, articles, bull_drivers,
                   bear_drivers, assessment, error?}
        """
        cache_key = f"sentiment:{symbol}:{use_llm}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        try:
            result = await self._analyze_internal(symbol, use_llm)
        except Exception as e:
            logger.warning(f"Sentiment analysis failed for {symbol}: {e}")
            result = {
                "error": "analysis_failed",
                "message": "Analisis sentimen gagal. Silakan coba lagi nanti.",
                "symbol": symbol,
            }

        # Cache 10 menit (hasil error juga di-cache pendek agar tidak spam API)
        ttl = 300 if result.get("error") else 600
        cache.set(cache_key, result, ttl)
        return result

    async def _analyze_internal(self, symbol: str, use_llm: bool) -> Dict:
        if not self.news_fetcher:
            return {"error": "no_fetcher", "message": "News fetcher belum dikonfigurasi."}

        finnhub = await self.news_fetcher.get_finnhub_news(symbol, limit=10)
        articles = finnhub.get("articles", [])

        # Fallback: kategori umum bila simbol tidak didukung Finnhub (DXY, BTC, dll)
        if not articles and symbol != "FOREX":
            finnhub = await self.news_fetcher.get_finnhub_news("FOREX", limit=10)
            articles = finnhub.get("articles", [])

        if not articles:
            return {
                "error": "no_news",
                "message": "Berita untuk instrumen ini tidak tersedia saat ini. "
                           "Coba lagi beberapa menit lagi.",
                "symbol": symbol,
            }

        # 1) Skor per artikel: Finnhub (65%) + Lexicon (35%)
        scored: List[Dict] = []
        for a in articles:
            fh_score = self._safe_float(a.get("sentiment"))
            text = f"{a.get('headline', '')} {a.get('summary', '')}"
            lex_score = self._lexicon_score(text)
            combined = self._combine_scores(fh_score, lex_score)
            scored.append({**a, "score": combined})

        # 2) Agregasi dengan pembobotan kedekatan waktu
        data_score, data_confidence, data_std = self._aggregate(scored)

        result: Dict[str, Any] = {
            "symbol": symbol,
            "score": round(data_score, 3),
            "label": self._score_to_label(data_score),
            "confidence": round(data_confidence, 3),
            "count": len(scored),
            "articles": scored,
            "bull_drivers": [],
            "bear_drivers": [],
            "assessment": "",
            "method": "finnhub+lexicon",
        }

        # 3) Refinement LLM (opsional)
        if use_llm and self.ai:
            llm = await self._llm_refine(symbol, scored, data_score)
            if llm:
                # Clamp ke [-1, 1] — skor LLM bisa keluar rentang (model liar)
                blended = 0.65 * data_score + 0.35 * llm.get("score", data_score)
                blended = round(max(-1.0, min(1.0, blended)), 3)
                result["score"] = blended
                result["label"] = self._score_to_label(blended)
                result["bull_drivers"] = llm.get("bull_drivers", []) or []
                result["bear_drivers"] = llm.get("bear_drivers", []) or []
                result["assessment"] = llm.get("assessment", "")
                llm_conf = self._safe_float(llm.get("confidence"))
                if llm_conf > 0:
                    result["confidence"] = round(max(data_confidence, llm_conf), 3)
                result["method"] = "finnhub+lexicon+llm"

        return result

    # ── Komponen skoring ─────────────────────────────────────────

    @staticmethod
    def _as_list(value) -> List[str]:
        """Pastikan value berupa list string (LLM kadang balas string, bukan array)."""
        if isinstance(value, list):
            return [str(v) for v in value if v]
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return []

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            v = float(value)
            return v if math.isfinite(v) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _lexicon_score(text: str) -> float:
        """Skor berbasis kata: range kira-kira [-1, 1]."""
        low = text.lower()
        bull = sum(1 for w in _BULLISH_WORDS if w in low)
        bear = sum(1 for w in _BEARISH_WORDS if w in low)
        total = bull + bear
        if total == 0:
            return 0.0
        return (bull - bear) / total

    @staticmethod
    def _combine_scores(finnhub_score: float, lexicon_score: float) -> float:
        """Gabungkan skor Finnhub & lexicon, clamp ke [-1, 1]."""
        # Finnhub 0 = tidak ada data → pakai lexicon murni
        if finnhub_score == 0.0:
            combined = lexicon_score
        else:
            combined = 0.65 * finnhub_score + 0.35 * lexicon_score
        return max(-1.0, min(1.0, combined))

    @staticmethod
    def _recency_weight(article: Dict, index: int) -> float:
        """Bobot berdasarkan kedekatan waktu (berita terbaru lebih penting)."""
        ts = SentimentAnalyzer._safe_float(article.get("datetime_ts"))
        if ts > 0:
            try:
                age_hours = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds() / 3600
            except (OverflowError, OSError, ValueError):
                age_hours = float(index) * 6  # fallback kasar
            return max(0.3, 1.0 / (1.0 + age_hours / 12.0))
        # Tanpa timestamp: index 0 = terbaru (Finnhub urut menurun)
        return min(1.0, max(0.3, 1.0 - 0.06 * index))

    @staticmethod
    def _aggregate(scored: List[Dict]) -> tuple:
        """Rata-rata terbobot + confidence berdasarkan jumlah & konsistensi berita."""
        weights = [SentimentAnalyzer._recency_weight(a, i) for i, a in enumerate(scored)]
        total_w = sum(weights)
        if total_w <= 0:
            return 0.0, 0.1, 0.0
        score = sum(a["score"] * w for a, w in zip(scored, weights)) / total_w

        scores = [a["score"] for a in scored]
        std = statistics.pstdev(scores) if len(scores) > 1 else 0.5
        count = len(scores)
        # Confidence naik dengan jumlah berita, turun dengan dispersi skor
        confidence = max(0.1, min(0.9, 0.35 + 0.06 * count - 0.25 * std))
        if count < 2:
            confidence = 0.15
        return max(-1.0, min(1.0, score)), confidence, std

    @staticmethod
    def _score_to_label(score: float) -> str:
        if score >= 0.6:
            return "sangat_bullish"
        if score >= 0.2:
            return "bullish"
        if score > -0.2:
            return "netral"
        if score > -0.6:
            return "bearish"
        return "sangat_bearish"

    # ── LLM Refinement ───────────────────────────────────────────

    async def _llm_refine(self, symbol: str, scored: List[Dict], data_score: float) -> Optional[Dict]:
        """Refinement skor & ekstraksi driver menggunakan AI engine."""
        try:
            top = scored[:8]
            lines = []
            for a in top:
                headline = (a.get("headline") or "").replace("\n", " ")[:150]
                lines.append(f"{a.get('score', 0):+.2f} | {a.get('source', '?')} | {headline}")

            prompt = SENTIMENT_TEMPLATE.format(
                instrument=symbol,
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                articles="\n".join(lines) if lines else "Tidak ada berita.",
            )

            # generate() sinkron (requests) → jalankan di thread
            response = await asyncio.to_thread(
                self.ai.generate, prompt, 2, True, SENTIMENT_SYSTEM
            )

            text = clean_json_response(response)
            data = json.loads(text)

            return {
                "score": self._safe_float(data.get("score"), data_score),
                "confidence": self._safe_float(data.get("confidence")),
                "bull_drivers": self._as_list(data.get("bull_drivers")),
                "bear_drivers": self._as_list(data.get("bear_drivers")),
                "assessment": data.get("assessment", ""),
            }
        except Exception as e:
            logger.warning(f"LLM sentiment refinement failed: {e}")
            return None

    # ── Formatting ───────────────────────────────────────────────

    @staticmethod
    def score_icon(score: float) -> str:
        if score >= 0.6:
            return "🟢🟢"
        if score >= 0.2:
            return "🟢"
        if score > -0.2:
            return "⚪"
        if score > -0.6:
            return "🔴"
        return "🔴🔴"

    def format_report(self, result: Dict, display_name: str = "") -> str:
        """Format laporan sentimen lengkap untuk Telegram (Markdown)."""
        if result.get("error"):
            return f"🧠 *Sentimen Pasar* — {display_name or result.get('symbol', '')}\n\n⚠️ {result.get('message', 'Tidak tersedia.')}"

        score = self._safe_float(result.get("score"))
        label_map = {
            "sangat_bullish": "Sangat Bullish",
            "bullish": "Bullish",
            "netral": "Netral",
            "bearish": "Bearish",
            "sangat_bearish": "Sangat Bearish",
        }
        label = label_map.get(result.get("label", ""), "Netral")
        conf = result.get("confidence", 0)
        count = result.get("count", 0)

        lines = [
            f"🧠 *SENTIMEN PASAR — {display_name or result.get('symbol', '')}*",
            f"📊 Skor: *{score:+.2f}* {self.score_icon(score)} ({label})",
            f"🎯 Confidence: {conf:.0%} dari {count} berita",
            f"🧪 Metode: {result.get('method', 'finnhub+lexicon')}",
            "",
        ]

        bull = result.get("bull_drivers") or []
        bear = result.get("bear_drivers") or []
        if bull:
            lines.append("🔺 *Driver Bullish:*")
            for d in bull[:3]:
                lines.append(f"• {d}")
            lines.append("")
        if bear:
            lines.append("🔻 *Driver Bearish:*")
            for d in bear[:3]:
                lines.append(f"• {d}")
            lines.append("")

        if result.get("assessment"):
            lines.append(f"💬 *Ringkasan:* {result['assessment']}")
            lines.append("")

        lines.append("📰 *Headline Teratas:*")
        for a in (result.get("articles") or [])[:5]:
            s = self._safe_float(a.get("score"))
            headline = (a.get("headline") or "")[:70]
            source = a.get("source") or "?"
            lines.append(f"{self.score_icon(s)} {s:+.2f} — {headline} ({source})")

        lines.append("")
        lines.append("---")
        lines.append("⚠️ *Disclaimer:* Sentimen berbasis berita publik, bukan sinyal trading.")
        return "\n".join(lines)

    def format_short(self, result: Dict, display_name: str = "") -> str:
        """Format singkat untuk morning brief."""
        if result.get("error"):
            return "Sentimen pasar tidak tersedia."
        score = self._safe_float(result.get("score"))
        label = result.get("label", "netral")
        label_map = {
            "sangat_bullish": "Sangat Bullish",
            "bullish": "Bullish",
            "netral": "Netral",
            "bearish": "Bearish",
            "sangat_bearish": "Sangat Bearish",
        }
        conf = result.get("confidence", 0)
        return (
            f"🧠 *Sentimen Pasar:* {self.score_icon(score)} {label_map.get(label, 'Netral')} "
            f"(skor {score:+.2f}, confidence {conf:.0%}, {result.get('count', 0)} berita)"
        )
