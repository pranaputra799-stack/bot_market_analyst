"""
Conversation Memory — Memory percakapan per-user (L1 memori + L2 Supabase).

Menyimpan beberapa pertanyaan-jawaban terakhir per user agar pertanyaan
follow-up ("kalau begitu support-nya di mana?") punya konteks percakapan
sebelumnya. Riwayat otomatis kedaluwarsa setelah MEMORY_TTL detik.

Selain riwayat Q&A mentah, modul ini menyimpan KONTEKS TERSTRUKTUR per user:
- asset_focus: instrumen yang sedang dibahas (mis. "EUR/USD", "XAU/USD (Gold)")
- direction:    arah tren terakhir yang disebut (bullish/bearish/sideways)
Konteks ini diekstrak otomatis dari pertanyaan & jawaban sehingga follow-up
ambigu seperti "support-nya berapa?" atau "bagaimana targetnya?" tetap tahu
instrumen & bias yang sedang dibicarakan.

Penyimpanan dua lapis:
- L1: memori (cepat) — hanya menyimpan entri aktif, dibatasi MAX_ENTRIES.
- L2: Supabase (persisten, via data.cache.persistent) — agar riwayat tidak
  menambah beban RAM proses bot dan tetap ada saat bot restart.
  Aman no-op bila Supabase tidak dikonfigurasi.

Catatan privasi: riwayat otomatis terhapus setelah MEMORY_TTL_SECONDS (default
24 jam) — bukan penyimpanan permanen jangka panjang.
"""

import logging
import re
import time
from typing import Dict, List, Optional

from data.cache import cache, persistent
from config.providers import YAHOO_SYMBOLS
from config.settings import (
    MEMORY_TTL_SECONDS,
    MEMORY_MAX_ENTRIES,
    MEMORY_MAX_EXCHANGES_IN_CONTEXT,
)

logger = logging.getLogger(__name__)

# ===================== KONFIGURASI =====================
# 24 jam default (env MEMORY_TTL_SECONDS) — riwayat follow-up bertahan lama,
# termasuk lintas restart/deploy (tersimpan di app_cache Supabase).
MEMORY_TTL = MEMORY_TTL_SECONDS
# Maksimal pasangan Q&A yang disimpan per user (env MEMORY_MAX_ENTRIES, default
# 10) — lebih banyak konteks untuk follow-up beruntun.
MAX_ENTRIES = MEMORY_MAX_ENTRIES
# Potong jawaban agar hemat token — cukup panjang agar level/angka kunci dari
# jawaban sebelumnya (support/resistance/harga) ikut tersimpan, sehingga
# jawaban follow-up tetap KONSISTEN dengan jawaban bot sebelumnya.
MAX_ANSWER_CHARS = 500
MAX_QUESTION_CHARS = 200
# Berapa pertukaran terakhir yang dimasukkan ke prompt (env
# MEMORY_MAX_EXCHANGES_IN_CONTEXT, default 6).
MAX_EXCHANGES_IN_CONTEXT = MEMORY_MAX_EXCHANGES_IN_CONTEXT

# ===================== EKSTRAKSI KONTEKS =====================
# Alias instrumen populer (no-space / istilah Indonesia) → label fokus aset.
# Urutan penting: entri yang lebih spesifik harus didahulukan.
_ASSET_ALIASES = {
    "xauusd": "XAU/USD (Gold)",
    "gold": "XAU/USD (Gold)",
    "emas": "XAU/USD (Gold)",
    "xagusd": "XAG/USD (Silver)",
    "silver": "XAG/USD (Silver)",
    "perak": "XAG/USD (Silver)",
    "btcusd": "BTC/USD (Bitcoin)",
    "bitcoin": "BTC/USD (Bitcoin)",
    "btc": "BTC/USD (Bitcoin)",
    "ethusd": "ETH/USD (Ethereum)",
    "ethereum": "ETH/USD (Ethereum)",
    "eth": "ETH/USD (Ethereum)",
    "dxy": "DXY (Dollar Index)",
    "dolar index": "DXY (Dollar Index)",
    "usdidr": "USD/IDR",
    "eurusd": "EUR/USD",
    "gbpusd": "GBP/USD",
    "usdjpy": "USD/JPY",
    "usdchf": "USD/CHF",
    "audusd": "AUD/USD",
    "nzdusd": "NZD/USD",
    "usdcad": "USD/CAD",
    "sp500": "S&P 500",
    "nasdaq": "NASDAQ",
    "vix": "VIX",
}

_BULLISH_KEYWORDS = ("bullish", "uptrend", "trend naik", "cenderung naik", "menguat", "naik", "rally", "buy")
_BEARISH_KEYWORDS = ("bearish", "downtrend", "trend turun", "cenderung turun", "melemah", "turun", "sell")
_SIDEWAYS_KEYWORDS = ("sideways", "netral", "konsolidasi", "ranging", "range", "flat")

# Alias dikompilasi dengan word boundary agar tidak false-positive:
# "gold" tidak cocok dengan "goldman", "eth" tidak cocok dengan "method".
# Diurutkan dari yang terpanjang agar kecocokan lebih spesifik didahulukan.
_ALIAS_PATTERNS = [
    (re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)"), label)
    for alias, label in sorted(_ASSET_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)
]


def extract_asset_focus(text: Optional[str]) -> Optional[str]:
    """
    Deteksi fokus aset dari teks (pertanyaan user biasanya).

    Mengenali alias populer ("gold", "emas", "eurusd", "dxy") serta pasangan
    dari YAHOO_SYMBOLS ("eur/usd", "usd/jpy"). Mengembalikan label human-readable
    atau None jika tidak ada instrumen yang dikenali.
    """
    if not text:
        return None
    t = text.lower().strip()
    for pattern, label in _ALIAS_PATTERNS:
        if pattern.search(t):
            return label
    for pair, symbol in YAHOO_SYMBOLS.items():
        if pair in t:
            if symbol == "GC=F":
                return "XAU/USD (Gold)"
            if symbol == "SI=F":
                return "XAG/USD (Silver)"
            if symbol == "BTC-USD":
                return "BTC/USD (Bitcoin)"
            if symbol == "ETH-USD":
                return "ETH/USD (Ethereum)"
            if symbol == "DX-Y.NYB":
                return "DXY (Dollar Index)"
            return pair.upper()
    return None


def extract_trend_direction(text: Optional[str]) -> Optional[str]:
    """
    Deteksi arah tren dari teks (jawaban AI biasanya): bullish / bearish /
    sideways. Menggunakan penghitungan kemunculan kata kunci agar teks yang
    menyebut keduanya tidak salah deteksi. None jika tidak jelas.
    """
    if not text:
        return None
    t = text.lower()
    bull = sum(t.count(k) for k in _BULLISH_KEYWORDS)
    bear = sum(t.count(k) for k in _BEARISH_KEYWORDS)
    side = sum(t.count(k) for k in _SIDEWAYS_KEYWORDS)
    if side and side >= bull and side >= bear:
        return "sideways"
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return None


# ===================== KEY =====================

def _key(user_id: int) -> str:
    return f"conversation:{user_id}"


def _ctx_key(user_id: int) -> str:
    return f"conversation:ctx:{user_id}"


# ===================== RIWAYAT Q&A =====================

def get_history(user_id: int) -> List[Dict]:
    """
    Ambil riwayat percakapan user (terbaru di akhir list).
    Cek memori dulu; jika kosong, ambil dari Supabase (L2) dan isi ulang memori.
    """
    key = _key(user_id)
    data = cache.get(key)
    if data is not None:
        return data if isinstance(data, list) else []

    persisted = persistent.get(key)
    if isinstance(persisted, list):
        cache.set(key, persisted, MEMORY_TTL)
        return persisted
    return []


def add_exchange(user_id: int, question: str, answer: str):
    """Simpan satu pertanyaan-jawaban (memori + Supabase), potong agar hemat token.

    Sekaligus memperbarui konteks terstruktur (fokus aset dari pertanyaan,
    arah tren dari jawaban) agar follow-up ambigu tetap punya konteks.
    """
    history = get_history(user_id)
    history.append({
        "q": (question or "").strip()[:MAX_QUESTION_CHARS],
        "a": (answer or "").strip()[:MAX_ANSWER_CHARS],
    })
    # Hanya simpan MAX_ENTRIES terakhir
    history = history[-MAX_ENTRIES:]
    cache.set(_key(user_id), history, MEMORY_TTL)
    persistent.set(_key(user_id), history, MEMORY_TTL)

    asset = extract_asset_focus(question)
    direction = extract_trend_direction(answer)
    if asset or direction:
        set_context(user_id, asset_focus=asset, direction=direction)

    logger.debug(f"Conversation memory updated for user {user_id}: {len(history)} entries")


def clear(user_id: int):
    """Hapus riwayat percakapan + konteks user (memori + Supabase)."""
    cache.delete(_key(user_id))
    persistent.delete(_key(user_id))
    cache.delete(_ctx_key(user_id))
    persistent.delete(_ctx_key(user_id))


# ===================== KONTEKS TERSTRUKTUR (multi-turn) =====================

def get_context(user_id: int) -> Dict:
    """
    Ambil konteks terakhir user: {"asset_focus": str?, "direction": str?}.
    Cek memori dulu; jika kosong, ambil dari Supabase (L2) dan isi ulang memori.
    """
    data = cache.get(_ctx_key(user_id))
    if isinstance(data, dict):
        return data
    persisted = persistent.get(_ctx_key(user_id))
    if isinstance(persisted, dict):
        cache.set(_ctx_key(user_id), persisted, MEMORY_TTL)
        return persisted
    return {}


def set_context(user_id: int, asset_focus: Optional[str] = None, direction: Optional[str] = None):
    """
    Perbarui & simpan konteks user. Nilai None tidak mengubah field tersebut,
    sehingga konteks lama yang masih relevan (mis. fokus aset) tetap bertahan.
    """
    ctx = get_context(user_id)
    if asset_focus:
        ctx["asset_focus"] = asset_focus
    if direction:
        ctx["direction"] = direction
    ctx["updated_at"] = int(time.time())
    cache.set(_ctx_key(user_id), ctx, MEMORY_TTL)
    persistent.set(_ctx_key(user_id), ctx, MEMORY_TTL)


# ===================== FORMAT UNTUK PROMPT =====================

def format_history(user_id: int, max_exchanges: int = MAX_EXCHANGES_IN_CONTEXT) -> str:
    """
    Format riwayat + konteks terakhir menjadi teks untuk prompt LLM.

    Returns:
        String siap-suntik, atau "" jika tidak ada riwayat maupun konteks.
    """
    lines: List[str] = []

    history = get_history(user_id)[-max_exchanges:]
    if history:
        lines.append("Percakapan sebelumnya (User ↔ Bot):")
        for ex in history:
            q = ex.get("q", "")
            a = ex.get("a", "")
            if q:
                lines.append(f'User: "{q}"')
            if a:
                lines.append(f"Bot: {a}")

    # Konteks terstruktur membantu follow-up yang ambigu (mis. "support-nya berapa?")
    ctx = get_context(user_id)
    ctx_lines = []
    if ctx.get("asset_focus"):
        ctx_lines.append(f"• Fokus aset: {ctx['asset_focus']}")
    if ctx.get("direction"):
        ctx_lines.append(f"• Arah tren: {ctx['direction']}")
    if ctx_lines:
        lines.append("KONTEKS PERCAKAPAN TERAKHIR:")
        lines.extend(ctx_lines)

    return "\n".join(lines)
