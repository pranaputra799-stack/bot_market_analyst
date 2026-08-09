"""
ccxt Client - Harga crypto REAL-TIME dari exchange publik (tanpa API key).

Yahoo Finance untuk crypto (BTC-USD / ETH-USD) delayed 15-20 menit. ccxt
mengambil data langsung dari REST API publik exchange (Binance, Coinbase,
Kraken, OKX, Bybit, KuCoin) — gratis, tanpa kunci, rate-limit longgar, dan
latensi detik-an. Data di-cache pendek agar tidak membebani exchange.

Menyediakan:
- get_crypto_ticker  — harga spot real-time (current_price, change 24 jam).
- get_crypto_ohlcv   — candle OHLCV real-time (chart BTC/ETH & analisis
  teknikal ikut real-time, bukan Yahoo yang delayed).

Arsitektur:
- Multi-exchange: exchange pertama yang berhasil dipakai (failover otomatis).
- Simbol USD dulu (BTC/USD), fallback USDT (BTC/USDT) bila pair USD tidak ada.
- Negative cache pendek saat semua exchange gagal (anti request beruntun).
- Aman bila ccxt tidak terpasang: semua fungsi return None/[] (data layer
  fallback ke Yahoo) — dependency tetap opsional di requirements.txt.
"""

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

# ccxt di-load LAZY (hanya saat harga/OHLCV crypto diminta) — library ini
# ~40MB RAM + ~1 detik import. Tidak perlu membebani startup bot di container
# kecil (free tier yang OOM-restart bila RSS melewati limit memori).
_ccxt = None
_ccxt_lock = threading.Lock()


def _get_ccxt():
    """Muat modul ccxt sekali (lazy, thread-safe). None bila tidak terpasang."""
    global _ccxt
    if _ccxt is None:
        with _ccxt_lock:
            if _ccxt is None:
                try:
                    import ccxt as _ccxt_mod  # type: ignore
                    _ccxt = _ccxt_mod
                except ImportError:  # pragma: no cover - jalur fallback
                    _ccxt = None
    return _ccxt

from data.cache import cache

logger = logging.getLogger(__name__)

# TTL cache harga crypto (detik). Harga crypto bergerak cepat — 30 dtk cukup
# segar tanpa membebani exchange. Bisa di-override via env CCXT_PRICE_TTL.
CCXT_PRICE_TTL = int(os.getenv("CCXT_PRICE_TTL", "30"))
# TTL cache candle OHLCV crypto (detik). Candle harian berubah lebih lambat
# dari harga spot — 60 dtk seimbang antara kesegaran & beban exchange.
# Bisa di-override via env CCXT_OHLCV_TTL.
CCXT_OHLCV_TTL = int(os.getenv("CCXT_OHLCV_TTL", "60"))
# Negative cache saat SEMUA exchange gagal (detik). Mencegah request beruntun
# (6 exchange × 2 simbol = 12 panggilan) setiap query spot ketika exchange
# sedang down — caller cukup fallback ke Yahoo dengan murah. Mirip pola
# negative cache 429 Yahoo.
CCXT_FAIL_TTL = 15

# Simbol Yahoo Finance → (base currency, symbol USD, symbol USDT fallback).
# Hanya aset yang didukung penuh public REST ccxt tanpa API key.
CRYPTO_SYMBOLS = {
    "BTC-USD": ("BTC", "BTC/USD", "BTC/USDT"),
    "ETH-USD": ("ETH", "ETH/USD", "ETH/USDT"),
}

# Urutan exchange yang dicoba. Binance = likuiditas terbaik & paling stabil;
# yang lain cadangan. Semua public REST — tidak butuh API key.
_EXCHANGE_IDS = ("binance", "coinbase", "kraken", "okx", "bybit", "kucoin")

# Map interval yfinance → timeframe ccxt (format ccxt: 1m/5m/15m/1h/1d/1w/1M).
# Interval yang TIDAK ada di map (mis. 2m/2h/90m/3mo) dipakai apa adanya —
# exchange yang tidak mendukungnya melempar error dan ditangani (coba exchange
# berikutnya → negative cache → fallback Yahoo). Caller saat ini hanya memakai
# 1d & 1h untuk crypto.
YF_TO_CCXT_TIMEFRAME = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "60m": "1h",
    "1d": "1d", "1wk": "1w", "1mo": "1M",
}

# Instance exchange di-cache (inisialisasi ccxt butuh setup kecil — jangan
# buat ulang per request). Dibuat lazy saat pertama dipakai; lock membuat init
# deterministik walau dipanggil dari banyak thread (asyncio.to_thread).
_exchanges: Dict[str, object] = {}
_exchanges_lock = threading.Lock()


def _get_exchange(exchange_id: str) -> object:
    """Dapatkan instance exchange (lazy init + cache, thread-safe)."""
    ccxt = _get_ccxt()
    if ccxt is None:
        return None
    with _exchanges_lock:
        if exchange_id not in _exchanges:
            exchange_cls = getattr(ccxt, exchange_id)
            _exchanges[exchange_id] = exchange_cls({
                # ccxt men-jeda request otomatis sesuai rate limit exchange
                "enableRateLimit": True,
                "timeout": 10000,  # 10 dtk per request — fallback cepat saat lambat
            })
        return _exchanges[exchange_id]


def _build_result(exchange_id: str, yahoo_symbol: str, ticker: Dict) -> Optional[Dict]:
    """
    Bentuk dict hasil dengan shape kompatibel get_yahoo_data (subset yang
    dibaca caller: current_price, change_pct, previous_close, volume, dll).

    Ticker ccxt berisi: last, bid, ask, high, low, baseVolume, percentage
    (perubahan 24 jam dalam %), timestamp.
    """
    last = ticker.get("last")
    if not last or last <= 0:
        return None
    pct = ticker.get("percentage")
    change_pct = round(float(pct), 2) if pct is not None else None
    previous_close = None
    if change_pct is not None and change_pct != -100:
        try:
            previous_close = round(float(last) / (1 + change_pct / 100.0), 8)
        except (ZeroDivisionError, TypeError, ValueError):
            previous_close = None
    return {
        "source": f"ccxt ({exchange_id})",
        "symbol": yahoo_symbol,
        "current_price": float(last),
        "previous_close": previous_close,
        "change_pct": change_pct,
        "high_52w": None,
        "low_52w": None,
        # ccxt baseVolume = volume koin (bukan dollar) — display-only
        "volume": int(ticker.get("baseVolume") or 0),
        "market_cap": None,
        # OHLCV TIDAK disediakan di sini — caller yang butuh candle (chart /
        # analisis teknikal) tetap memakai Yahoo via get_ohlcv_history.
        "ohlcv": [],
        "timestamp": datetime.now().isoformat(),
    }


def get_crypto_ticker(yahoo_symbol: str) -> Optional[Dict]:
    """
    Ambil harga real-time crypto dari exchange publik (multi-exchange failover).

    Args:
        yahoo_symbol: Simbol Yahoo ("BTC-USD", "ETH-USD").

    Returns:
        Dict shape kompatibel get_yahoo_data (source "ccxt (...)" dan ohlcv=[]),
        atau None bila ccxt tidak terpasang / semua exchange gagal (caller
        fallback ke Yahoo).
    """
    if _get_ccxt() is None:
        return None
    info = CRYPTO_SYMBOLS.get(yahoo_symbol)
    if not info:
        return None

    cache_key = f"ccxt:{yahoo_symbol}"
    cached = cache.get(cache_key)
    if cached:
        # Nilai negative-cache (semua exchange gagal) → langsung None agar
        # caller cepat fallback ke Yahoo tanpa 12 panggilan ulang ke exchange.
        if "error" in cached:
            return None
        return cached

    _, sym_usd, sym_usdt = info
    errors = []
    for exchange_id in _EXCHANGE_IDS:
        try:
            exchange = _get_exchange(exchange_id)
            for symbol in (sym_usd, sym_usdt):
                try:
                    ticker = exchange.fetch_ticker(symbol)
                except Exception as e:
                    errors.append(f"{exchange_id}/{symbol}: {e}")
                    continue
                result = _build_result(exchange_id, yahoo_symbol, ticker)
                if result is not None:
                    cache.set(cache_key, result, CCXT_PRICE_TTL)
                    logger.debug(f"Crypto {yahoo_symbol} via {exchange_id}/{symbol}: {result['current_price']}")
                    return result
        except Exception as e:
            errors.append(f"{exchange_id}: {e}")
            continue

    # Semua exchange gagal — negative cache pendek: jangan retry 12 panggilan
    # beruntun tiap query selama exchange down (mirip pola 429 Yahoo). Caller
    # (get_yahoo_data) cukup fallback ke Yahoo yang punya negative cache sendiri.
    try:
        cache.set(cache_key, {"error": "all exchanges failed"}, CCXT_FAIL_TTL)
    except Exception:
        pass
    logger.warning(f"Semua exchange gagal untuk {yahoo_symbol}: {errors[-3:]}")
    return None


def _convert_ohlcv(raw: list) -> list:
    """
    Konversi bar ccxt ([ts_ms, open, high, low, close, volume] per bar) ke
    format OHLCV proyek: {date, open, high, low, close, volume} — identik
    dengan output yfinance sehingga chart & indikator teknikal bisa langsung
    memakainya tanpa perubahan.

    Bar yang tidak valid (None / bukan angka) dibuang.
    """
    rows = []
    for bar in raw or []:
        if not bar or len(bar) < 6:
            continue
        ts, o, h, l, c, v = bar[:6]
        try:
            rows.append({
                # UTC (deterministik, format sama dengan yfinance intraday)
                "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "open": round(float(o), 5),
                "high": round(float(h), 5),
                "low": round(float(l), 5),
                "close": round(float(c), 5),
                "volume": int(float(v)),
            })
        except (TypeError, ValueError):
            continue
    return rows


def get_crypto_ohlcv(yahoo_symbol: str, interval: str = "1d", limit: int = 60) -> list:
    """
    Ambil candle OHLCV real-time crypto dari exchange publik (failover).

    Args:
        yahoo_symbol: Simbol Yahoo ("BTC-USD", "ETH-USD").
        interval: Interval gaya yfinance ("1d", "1h", "15m", "1mo", dll) —
            dipetakan ke timeframe ccxt; nilai tak dikenal dipakai apa adanya.
        limit: Jumlah candle terakhir yang diminta (mis. 60 = ~3 bulan harian).

    Returns:
        List {date, open, high, low, close, volume} (kronologis naik), atau []
        bila ccxt tidak terpasang / semua exchange gagal (caller fallback Yahoo).
    """
    if _get_ccxt() is None:
        return []
    info = CRYPTO_SYMBOLS.get(yahoo_symbol)
    if not info:
        return []

    timeframe = YF_TO_CCXT_TIMEFRAME.get(str(interval), str(interval))
    cache_key = f"ccxtohlcv:{yahoo_symbol}:{timeframe}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # [] (negative cache) atau list candle

    _, sym_usd, sym_usdt = info
    errors = []
    for exchange_id in _EXCHANGE_IDS:
        try:
            exchange = _get_exchange(exchange_id)
            for symbol in (sym_usd, sym_usdt):
                try:
                    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                except Exception as e:
                    errors.append(f"{exchange_id}/{symbol}: {e}")
                    continue
                candles = _convert_ohlcv(raw)
                if len(candles) >= 2:
                    cache.set(cache_key, candles, CCXT_OHLCV_TTL)
                    logger.debug(f"OHLCV {yahoo_symbol} ({timeframe}, n={len(candles)}) via {exchange_id}/{symbol}")
                    return candles
        except Exception as e:
            errors.append(f"{exchange_id}: {e}")
            continue

    # Negative cache pendek: saat semua exchange down, jangan retry beruntun.
    try:
        cache.set(cache_key, [], CCXT_FAIL_TTL)
    except Exception:
        pass
    logger.warning(f"OHLCV gagal untuk {yahoo_symbol} ({timeframe}): {errors[-3:]}")
    return []
