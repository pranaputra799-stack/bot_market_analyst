"""
Intent Classifier — Menganalisis maksud pertanyaan user secara cerdas.
Menggunakan hybrid approach: keyword matching cepat + LLM untuk kasus kompleks.

Layered Detection:
  1. Quick keyword scan (instant, no API call)
  2. Pattern matching (regex untuk pertanyaan umum)
  3. LLM fallback (untuk pertanyaan ambigu/kosakata baru)

Intent Categories:
  - price_check:    "EUR/USD berapa?", "harga gold"
  - technical:      "analisis teknikal EUR/USD", "support resistance"
  - fundamental:    "dampak NFP", "kenapa inflasi naik"
  - macro:          "data CPI terbaru", "suku bunga Fed"
  - news_sentiment: "berita forex hari ini", "sentimen pasar"
  - correlation:    "korelasi gold dan DXY", "hubungan EUR/USD dan USD/JPY"
  - education:      "apa itu forex", "bagaimana cara baca chart"
  - comparison:     "gold vs bitcoin", "EUR lebih kuat dari USD?"
  - prediction:     "prediksi EUR/USD besok", "gold akan naik?"
  - calendar:       "jadwal rilis data ekonomi", "kalender NFP"
  - risk:           "risiko trading gold", "apa yang perlu diwaspadai"
  - general:        catch-all
"""

import asyncio
import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """Hasil klasifikasi intent dengan metadata."""
    intent: str  # Kategori utama
    sub_intent: str = ""  # Sub-kategori (misal: "forex", "gold", "macro")
    confidence: float = 0.0  # 0.0 - 1.0
    detected_entities: List[str] = field(default_factory=list)  # Entitas yang terdeteksi
    detected_pairs: List[str] = field(default_factory=list)  # Pair forex
    detected_indicators: List[str] = field(default_factory=list)  # Indikator teknikal
    needs_market_data: bool = False
    needs_macro_data: bool = False
    needs_news_data: bool = False
    needs_calendar: bool = False
    is_urgent: bool = False  # Pertanyaan tentang pergerakan harga saat ini

    def to_dict(self) -> Dict:
        return {
            "intent": self.intent,
            "sub_intent": self.sub_intent,
            "confidence": self.confidence,
            "entities": self.detected_entities,
            "pairs": self.detected_pairs,
            "indicators": self.detected_indicators,
        }


class IntentClassifier:
    """
    Classifier cerdas untuk memahami maksud pertanyaan user.

    Menggunakan 3 layer:
    Layer 1: Keyword matching (cepat, real-time)
    Layer 2: Pattern matching (regex untuk struktur pertanyaan umum)
    Layer 3: LLM fallback (hanya jika layer 1&2 gagal dengan confidence rendah)
    """

    # ── Layer 1: Keyword Dictionary ───────────────────────────────────────

    # Kata kunci per intent (urut dari paling spesifik ke general)
    INTENT_KEYWORDS: Dict[str, List[str]] = {
        "price_check": [
            "harga", "price", "berapa", "rate", "kurs", "nilai", "quote",
            "market price", "current price", "spot", "live price",
        ],
        "technical": [
            "teknikal", "support", "resistance", "rsi", "macd", "ema", "sma",
            "bollinger", "chart pattern", "head and shoulder", "double top",
            "double bottom", "trendline", "fibonacci", "retracement",
            "breakout", "moving average", "indikator", "oscillator",
            "golden cross", "death cross", "divergence", "candlestick",
            "bearish", "bullish", "overbought", "oversold",
        ],
        "fundamental": [
            "nfp", "non-farm payroll", "cpi", "inflasi", "inflation",
            "gdp", "fed", "fomc", "suku bunga", "interest rate",
            "tenaga kerja", "unemployment", "pengangguran",
            "ppI", "producer price", "retail sales", "consumer confidence",
            "pmI", "manufacturing", "service", "bisnis",
        ],
        "macro": [
            "makro", "macro", "ekonomi", "economy", "global",
            "resesi", "recession", "pertumbuhan", "growth",
            "utang", "debt", "defisit", "deficit", "neraca",
        ],
        "news_sentiment": [
            "berita", "news", "sentimen", "sentiment", "headline",
            "terkini", "update", "kabar", "info terbaru",
            "market news", "breaking", "today", "hari ini apa",
        ],
        "correlation": [
            "korelasi", "correlation", "hubungan", "relationship",
            "dampak", "impact", "pengaruh", "effect", "terkait",
            "against", "versus", "vs", "compared",
        ],
        "education": [
            "apa itu", "what is", "pengertian", "definisi", "definition",
            "bagaimana", "how to", "cara baca", "cara menghitung",
            "belajar", "learn", "pemula", "beginner", "tutorial",
            "jelaskan", "explain", "maksudnya", "artinya",
        ],
        "comparison": [
            "bandingkan", "compare", "lebih baik", "lebih kuat",
            "mana yang", "which one", "perbedaan", "difference",
            "vs", "versus", "atau", "or",
        ],
        "prediction": [
            "prediksi", "prediction", "forecast", "proyeksi",
            "ramalan", "akan", "future", "next week", "next month",
            "target", "estimate", "perkiraan", "kemana",
            "naik", "turun", "akan naik", "akan turun",
        ],
        "calendar": [
            "kalender", "calendar", "jadwal", "schedule",
            "rilis", "release", "event", "datang",
        ],
        "risk": [
            "risiko", "risk", "bahaya", "danger", "waspada",
            "volatilitas", "volatility", "crash", "krisis",
            "safe haven", "hedge", "lindung nilai",
        ],
    }

    # Entitas/Instrumen yang perlu dideteksi
    FOREX_PAIRS: Dict[str, str] = {
        "eur/usd": "EUR/USD", "gbp/usd": "GBP/USD", "usd/jpy": "USD/JPY",
        "usd/chf": "USD/CHF", "aud/usd": "AUD/USD", "usd/cad": "USD/CAD",
        "nzd/usd": "NZD/USD", "eur/jpy": "EUR/JPY", "gbp/jpy": "GBP/JPY",
        "eur/gbp": "EUR/GBP", "usd/idr": "USD/IDR",
        "eur": "EUR", "usd": "USD", "gbp": "GBP", "jpy": "JPY",
    }

    COMMODITIES: Dict[str, str] = {
        "gold": "XAU/USD", "emas": "XAU/USD", "xau/usd": "XAU/USD",
        "silver": "XAG/USD", "perak": "XAG/USD", "xag/usd": "XAG/USD",
        "oil": "Crude Oil", "minyak": "Crude Oil", "emas hitam": "Crude Oil",
    }

    INDICES: Dict[str, str] = {
        "dxy": "DXY", "dollar index": "DXY",
        "s&p": "S&P 500", "sp500": "S&P 500", "snp": "S&P 500",
        "nasdaq": "NASDAQ", "dow jones": "Dow Jones",
        "vix": "VIX", "nikkei": "Nikkei", "hangseng": "Hang Seng",
    }

    CRYPTO: Dict[str, str] = {
        "bitcoin": "BTC/USD", "btc": "BTC/USD",
        "ethereum": "ETH/USD", "eth": "ETH/USD",
    }

    # ── Layer 2: Pattern Dictionary ───────────────────────────────────────

    QUESTION_PATTERNS: Dict[str, List[str]] = {
        "price_check": [
            r"(?:berapa|harga|price)\s+(?:harga\s+)?(?:saat\s+ini\s+)?(\w+(?:/)?\w+)",
            r"(?:rate|kurs)\s+(\w+(?:/)?\w+)",
            r"(\w+(?:/)?\w+)\s+(?:ke\s+)?(?:berapa|price|rate)",
            r"(?:current|live|spot)\s+(?:price|rate|harga)",
        ],
        "technical": [
            r"(?:analisis\s+)?teknikal\s+(\w+(?:/)?\w+)",
            r"(?:support|resistance)\s+(?:dan\s+)?(?:untuk\s+)?(\w+(?:/)?\w+)",
            r"(?:rsi|macd|ema|sma|bollinger)\s+(\w+(?:/)?\w+)",
        ],
        "fundamental": [
            r"(?:dampak|pengaruh|efek)\s+(?:dari\s+)?(nfp|cpi|fed|fomc)",
            r"(?:nfp|cpi|gdp|fed\s+rate)\s+(?:terbaru|hari\s+ini|bulan\s+ini)",
        ],
        "prediction": [
            r"(?:prediksi|perkiraan|ramalan)\s+(\w+(?:/)?\w+)",
            r"(\w+(?:/)?\w+)\s+(?:akan\s+)?(?:naik|turun|bergerak)",
            r"(?:kemana|kearah)\s+(?:mana\s+)?(\w+(?:/)?\w+)",
            r"(?:bullish|bearish)\s+(?:pada\s+)?(\w+(?:/)?\w+)",
        ],
        "education": [
            r"^apa\s+(?:itu|yang\s+dimaksud\s+dengan)",
            r"^jelaskan\s+(?:tentang|mengenai|apa\s+itu)",
            r"^bagaimana\s+(?:cara\s+)?(?:membaca|menghitung|menganalisis)",
        ],
        "comparison": [
            r"(?:bandingkan|perbedaan)\s+(\w+(?:\/)?\w+)\s+(?:dan|vs|dengan)\s+(\w+(?:\/)?\w+)",
            r"(\w+(?:\/)?\w+)\s+(?:vs|versus|atau)\s+(\w+(?:\/)?\w+)",
        ],
        "calendar": [
            r"(?:jadwal|kalender)\s+(?:rilis\s+)?(?:data\s+)?ekonomi",
            r"(?:rilis|event|data)\s+(?:ekonomi|nfp|cpi|fomc)",
        ],
    }

    def __init__(self, ai_engine: Optional[object] = None):
        """
        Args:
            ai_engine: Optional AI engine untuk LLM fallback classification
        """
        self.ai = ai_engine

    async def classify(self, question: str) -> IntentResult:
        """
        Klasifikasi intent dari pertanyaan user menggunakan layered approach.

        Args:
            question: Pertanyaan user dalam text

        Returns:
            IntentResult dengan intent, sub_intent, entities, dan metadata
        """
        q = question.lower().strip()

        # Bersihkan dari noise (multi-spasi, dll)
        q = re.sub(r'\s+', ' ', q)

        # Step 1: Deteksi entitas (pairs, commodities, indices, crypto)
        detected_entities = self._detect_entities(q)
        detected_pairs = self._detect_pairs(q)

        # Step 2: Layer 1 — Keyword scoring
        intent_scores = self._keyword_scoring(q)
        best_intent, best_score = max(intent_scores.items(), key=lambda x: x[1])

        # Step 3: Layer 2 — Pattern matching (override keyword jika cocok)
        pattern_intent = self._pattern_matching(q)
        if pattern_intent and pattern_intent[1] > best_score:
            best_intent, best_score = pattern_intent

        # Step 4: Tentukan sub-intent berdasarkan entitas
        sub_intent = self._determine_sub_intent(q, detected_entities)

        # Step 5: Tentukan kebutuhan data
        needs_market = best_intent in ("price_check", "technical", "prediction", "comparison")
        needs_macro = best_intent in ("fundamental", "macro")
        needs_news = best_intent in ("news_sentiment",)
        needs_calendar = best_intent in ("calendar", "fundamental")
        is_urgent = self._detect_urgency(q)

        # Jika confidence rendah dan AI engine tersedia, coba LLM fallback
        if best_score < 0.4 and self.ai:
            llm_intent = await self._llm_classify(q)
            if llm_intent:
                best_intent = llm_intent
                best_score = 0.7  # LLM classification dianggap cukup yakin

        # Fallback untuk general intent
        if best_score < 0.2:
            # Deteksi jika ini pertanyaan market meski tanpa keyword spesifik
            if detected_entities:
                best_intent = "price_check"
                best_score = 0.5
                needs_market = True

        return IntentResult(
            intent=best_intent,
            sub_intent=sub_intent,
            confidence=min(1.0, best_score),
            detected_entities=detected_entities,
            detected_pairs=detected_pairs,
            needs_market_data=needs_market,
            needs_macro_data=needs_macro,
            needs_news_data=needs_news,
            needs_calendar=needs_calendar,
            is_urgent=is_urgent,
        )

    # ── Layer 1: Keyword Scoring ────────────────────────────────────────

    def _keyword_scoring(self, q: str) -> Dict[str, float]:
        """
        Skoring intent berdasarkan keyword matching.
        Weighted: keyword lebih panjang/spesifik = skor lebih tinggi.
        """
        scores: Dict[str, float] = {}
        for intent, keywords in self.INTENT_KEYWORDS.items():
            score = 0.0
            matched = []
            for kw in keywords:
                if kw in q:
                    # Keyword lebih panjang = lebih spesifik = skor lebih tinggi
                    kw_score = min(1.0, len(kw) / 15)
                    matched.append(kw)
                    score += kw_score

            # Normalize by log of keyword count
            if matched:
                score = score * (1.0 + 0.1 * len(matched))
                scores[intent] = score
            else:
                scores[intent] = 0.0

        return scores

    # ── Layer 2: Pattern Matching ───────────────────────────────────────

    def _pattern_matching(self, q: str) -> Optional[Tuple[str, float]]:
        """
        Pattern matching dengan regex untuk struktur pertanyaan umum.
        """
        for intent, patterns in self.QUESTION_PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, q)
                if match:
                    # Skor lebih tinggi untuk pattern match (lebih spesifik)
                    return (intent, 0.8)
        return None

    # ── Layer 3: LLM Fallback ───────────────────────────────────────────

    async def _llm_classify(self, q: str) -> Optional[str]:
        """
        Gunakan LLM untuk klasifikasi intent jika keyword gagal.
        """
        if not self.ai:
            return None

        prompt = (
            f"ROLE: Anda adalah classifier intent untuk bot analisis pasar. "
            f"Tugas: tentukan SATU kategori intent dari pertanyaan user.\n\n"
            f"DAFTAR KATEGORI (pilih salah satu):\n"
            f"- price_check: Menanyakan harga/nilai pasar\n"
            f"- technical: Analisis teknikal, indikator, chart\n"
            f"- fundamental: Data fundamental ekonomi\n"
            f"- macro: Kondisi makroekonomi global\n"
            f"- news_sentiment: Berita dan sentimen pasar\n"
            f"- correlation: Hubungan antar instrumen\n"
            f"- education: Pembelajaran, definisi, konsep\n"
            f"- comparison: Perbandingan instrumen\n"
            f"- prediction: Prediksi arah pasar\n"
            f"- calendar: Jadwal rilis data\n"
            f"- risk: Analisis risiko\n\n"
            f"CONTOH:\n"
            f"Pertanyaan: \"gold naik apa turun besok?\" → prediction\n"
            f"Pertanyaan: \"berapa harga EUR/USD sekarang?\" → price_check\n"
            f"Pertanyaan: \"jelaskan apa itu RSI\" → education\n\n"
            f"Pertanyaan: \"{q}\"\n\n"
            f"Jawab HANYA dengan satu nama kategori (huruf kecil), tanpa tanda kutip, "
            f"tanpa penjelasan, tanpa simbol lain."
        )

        try:
            # generate() sinkron (requests) → jalankan di thread agar tidak
            # memblokir event loop saat klassifikasi ambigu.
            response = await asyncio.to_thread(self.ai.generate, prompt, use_cache=False)
            response = response.strip().lower()
            valid_intents = self.INTENT_KEYWORDS.keys()
            for intent in valid_intents:
                if intent in response:
                    return intent
        except Exception as e:
            logger.debug(f"LLM classification failed: {e}")

        return None

    # ── Entity Detection ───────────────────────────────────────────────

    def _detect_entities(self, q: str) -> List[str]:
        """Deteksi semua entitas pasar dalam pertanyaan."""
        entities = []

        # Cek forex pairs
        for key, name in self.FOREX_PAIRS.items():
            if key in q:
                entities.append(name)

        # Cek commodities
        for key, name in self.COMMODITIES.items():
            if key in q:
                entities.append(name)

        # Cek indices
        for key, name in self.INDICES.items():
            if key in q:
                entities.append(name)

        # Cek crypto
        for key, name in self.CRYPTO.items():
            if key in q:
                entities.append(name)

        # Deduplicate while preserving order
        seen: Set[str] = set()
        return [e for e in entities if not (e in seen or seen.add(e))]

    def _detect_pairs(self, q: str) -> List[str]:
        """Deteksi pair forex spesifik dalam pertanyaan."""
        pairs = []
        # Cari pola seperti EUR/USD, GBP/USD, dll
        pair_pattern = r'\b([A-Za-z]{3})\s*/\s*([A-Za-z]{3})\b'
        matches = re.findall(pair_pattern, q)
        for base, quote in matches:
            pairs.append(f"{base.upper()}/{quote.upper()}")

        # Juga cek format tanpa slash: EURUSD
        single_pattern = r'\b(EURUSD|GBPUSD|USDJPY|USDCHF|AUDUSD|USDCAD|NZDUSD|USDIDR)\b'
        matches = re.findall(single_pattern, q.upper())
        for m in matches:
            if len(m) == 6:
                pairs.append(f"{m[:3]}/{m[3:]}")

        return list(set(pairs))

    # ── Sub-intent Detection ────────────────────────────────────────────

    def _determine_sub_intent(self, q: str, entities: List[str]) -> str:
        """Tentukan sub-intent berdasarkan entitas yang terdeteksi."""
        if any(e in ("XAU/USD", "emas") for e in entities) or "gold" in q or "emas" in q:
            return "gold"
        if "btc" in q or "bitcoin" in q or "crypto" in q or "kripto" in q:
            return "crypto"
        if any("JPY" in e for e in entities) or "jpy" in q or "yen" in q:
            return "jpy"
        if any("IDR" in e for e in entities) or "idr" in q or "rupiah" in q:
            return "idr"
        if any(e in ("DXY",) for e in entities) or "dxy" in q or "dollar index" in q:
            return "dxy"
        if any(e in ("XAG/USD",) for e in entities) or "silver" in q or "perak" in q:
            return "silver"
        if "saham" in q or "stock" in q or "s&p" in q or "nasdaq" in q:
            return "equity"
        if any("EUR" in e for e in entities):
            return "forex_major"
        return ""

    # ── Urgency Detection ──────────────────────────────────────────────

    @staticmethod
    def _detect_urgency(q: str) -> bool:
        """Deteksi urgensi dari pertanyaan."""
        urgent_words = [
            "sedang", "lagi", "saat ini", "tadi", "baru saja",
            "breaking", "urgent", "darurat", "crash",
            "tiba-tiba", "mendadak", "hari ini juga",
        ]
        return any(w in q for w in urgent_words)

    # ── Convenience Methods ────────────────────────────────────────────

    def get_data_requirements(self, intent: str) -> Dict[str, bool]:
        """Dapatkan kebutuhan data berdasarkan intent."""
        mapping = {
            "price_check": {"market": True, "macro": False, "news": False, "calendar": False},
            "technical": {"market": True, "macro": False, "news": False, "calendar": False},
            "fundamental": {"market": True, "macro": True, "news": True, "calendar": True},
            "macro": {"market": True, "macro": True, "news": True, "calendar": True},
            "news_sentiment": {"market": False, "macro": False, "news": True, "calendar": True},
            "correlation": {"market": True, "macro": True, "news": False, "calendar": False},
            "education": {"market": False, "macro": False, "news": False, "calendar": False},
            "comparison": {"market": True, "macro": False, "news": False, "calendar": False},
            "prediction": {"market": True, "macro": True, "news": True, "calendar": True},
            "calendar": {"market": False, "macro": True, "news": False, "calendar": True},
            "risk": {"market": True, "macro": True, "news": True, "calendar": True},
            "general": {"market": False, "macro": False, "news": False, "calendar": False},
        }
        return mapping.get(intent, {"market": False, "macro": False, "news": False, "calendar": False})


# Singleton instance untuk digunakan di seluruh aplikasi
_classifier_instance: Optional[IntentClassifier] = None


def get_classifier(ai_engine: Optional[object] = None) -> IntentClassifier:
    """Dapatkan singleton IntentClassifier."""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = IntentClassifier(ai_engine)
    return _classifier_instance
