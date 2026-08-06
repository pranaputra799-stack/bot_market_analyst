"""
Technical Indicators — dihitung LOKAL dari data OHLCV (tanpa API/network).

Mengapa penting: sebelumnya signal engine bergantung pada `technical_indicators`
yang TIDAK PERNAH dikirim dari pipeline (selalu None), sehingga RSI/MACD/Bollinger
tidak pernah benar-benar dievaluasi. Modul ini menghitung indikator murni dari
candle data (yfinance) yang sudah di-cache, sehingga:

- RSI, MACD, Bollinger, ATR, Stochastic dihitung dengan formula standar
- Level support/resistance dari PIVOT POINT klasik + Fibonacci retracement
  (bukan ditebak LLM — anti-halusinasi)
- Semua murni matematika: deterministik, cepat (<1ms), tanpa biaya AI token

Semua fungsi aman untuk data pendek: jika data tidak cukup, mengembalikan None
(bukan exception) — caller cukup mengabaikan nilai None.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ===================== DASAR =====================

def _sma(values: List[float], period: int) -> Optional[float]:
    """Simple Moving Average dari N nilai terakhir."""
    if not values or len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window) / period


def _ema(values: List[float], period: int) -> Optional[float]:
    """Exponential Moving Average (Wilder-style seeding dengan SMA)."""
    if not values or len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # seed = SMA pertama
    for price in values[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Relative Strength Index (Wilder smoothing)."""
    if not closes or len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    # Wilder smoothing
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        # Semua bar naik → RSI 100. Pasar datar total (gain & loss 0) → 50 (netral),
        # bukan 100 — RSI 100 pada pasar sideways adalah sinyal palsu overbought.
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """EMA untuk SELURUH deret (nilai None untuk bar yang belum cukup)."""
    if not values or period <= 0:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def _macd(closes: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Optional[Dict]:
    """MACD: macd line, signal line, histogram. Butuh ~34 bar.

    Signal line dihitung sebagai EMA(9) dari SELURUH deret MACD line
    (bukan pendekatan satu titik), sehingga posisi relatif macd vs signal
    akurat secara matematis.
    """
    if not closes or len(closes) < slow + signal_period:
        return None

    ema_fast_series = _ema_series(closes, fast)
    ema_slow_series = _ema_series(closes, slow)

    # Deret MACD line (hanya bar yang kedua EMA-nya tersedia)
    macd_series = []
    for f, s in zip(ema_fast_series, ema_slow_series):
        if f is None or s is None:
            macd_series.append(None)
        else:
            macd_series.append(f - s)

    macd_values = [m for m in macd_series if m is not None]
    if not macd_values:
        return None

    signal_series = _ema_series(macd_values, signal_period)
    signal_values = [s for s in signal_series if s is not None]
    if not signal_values:
        return None

    macd_line = macd_values[-1]
    signal_line = signal_values[-1]
    return {
        "macd": round(macd_line, 6),
        "macd_signal": round(signal_line, 6),
        "macd_hist": round(macd_line - signal_line, 6),
    }


def _bollinger(closes: List[float], period: int = 20, num_std: float = 2.0) -> Optional[Dict]:
    """Bollinger Bands: middle (SMA), upper, lower."""
    if not closes or len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    return {
        "upper": mid + num_std * std,
        "middle": mid,
        "lower": mid - num_std * std,
        "bandwidth_pct": (2 * num_std * std / mid * 100) if mid else 0.0,
    }


def _atr(ohlcv: List[Dict], period: int = 14) -> Optional[float]:
    """Average True Range (Wilder). Butuh period+1 bar."""
    if not ohlcv or len(ohlcv) < period + 1:
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    trs = []
    for i in range(1, len(ohlcv)):
        high = _f(ohlcv[i].get("high"))
        low = _f(ohlcv[i].get("low"))
        prev_close = _f(ohlcv[i - 1].get("close"))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _stochastic(ohlcv: List[Dict], period: int = 14) -> Optional[float]:
    """Stochastic %K: posisi close dalam rentang period terakhir."""
    if not ohlcv or len(ohlcv) < period:
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    window = ohlcv[-period:]
    highs = [_f(c.get("high")) for c in window]
    lows = [_f(c.get("low")) for c in window]
    close = _f(window[-1].get("close"))

    highest = max(highs)
    lowest = min(lows)
    if highest == lowest:
        return 50.0
    return (close - lowest) / (highest - lowest) * 100.0


def _pivot_points(ohlcv: List[Dict]) -> Optional[Dict]:
    """
    Pivot Point klasik dari candle terakhir (H/L/C).
    Rumus: P = (H+L+C)/3; R1 = 2P−L; S1 = 2P−H; R2 = P+(H−L); S2 = P−(H−L).
    """
    if not ohlcv:
        return None
    last = ohlcv[-1]

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    high = _f(last.get("high"))
    low = _f(last.get("low"))
    close = _f(last.get("close"))
    if high is None or low is None or close is None or high == low:
        return None

    pivot = (high + low + close) / 3.0
    return {
        "pivot": pivot,
        "r1": 2 * pivot - low,
        "s1": 2 * pivot - high,
        "r2": pivot + (high - low),
        "s2": pivot - (high - low),
        "r3": high + 2 * (pivot - low),
        "s3": low - 2 * (high - pivot),
    }


def _fibonacci_levels(ohlcv: List[Dict], lookback: int = 40) -> Optional[Dict]:
    """
    Level Fibonacci retracement dari swing high/low dalam lookback bar.
    Mengembalikan level resistance (0.0–0.382) & support (0.618–1.0) relatif.
    """
    if not ohlcv or len(ohlcv) < 10:
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    window = ohlcv[-lookback:]
    highs = [_f(c.get("high")) for c in window if c.get("high") is not None]
    lows = [_f(c.get("low")) for c in window if c.get("low") is not None]
    if not highs or not lows:
        return None

    swing_high = max(highs)
    swing_low = min(lows)
    if swing_high == swing_low:
        return None

    diff = swing_high - swing_low
    ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "levels": {
            f"fib_{int(r * 1000)}": swing_low + diff * r for r in ratios
        },
    }


# ===================== API UTAMA =====================

def compute_indicators(ohlcv: List[Dict]) -> Dict:
    """
    Hitung semua indikator teknikal dari data OHLCV.

    Args:
        ohlcv: List of {date, open, high, low, close, volume}

    Returns:
        Dict dengan indikator (nilai None jika data tidak cukup).
        Selalu mengembalikan dict (tidak pernah raise).
    """
    if not ohlcv:
        return {}

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    closes = [_f(c.get("close")) for c in ohlcv if c.get("close") is not None]

    indicators: Dict = {}
    indicators["rsi"] = _rsi(closes)
    indicators["macd"] = _macd(closes)
    indicators["bollinger"] = _bollinger(closes)
    indicators["atr"] = _atr(ohlcv)
    indicators["stochastic"] = _stochastic(ohlcv)
    indicators["ema_20"] = _ema(closes, 20)
    indicators["ema_50"] = _ema(closes, 50)
    indicators["sma_20"] = _sma(closes, 20)
    indicators["sma_50"] = _sma(closes, 50)
    indicators["pivot_points"] = _pivot_points(ohlcv)
    indicators["fibonacci"] = _fibonacci_levels(ohlcv)

    # Statistik harga tambahan
    if closes:
        indicators["current_price"] = closes[-1]
        indicators["price_5d_change"] = (
            (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) > 6 else None
        )
        indicators["price_20d_change"] = (
            (closes[-1] - closes[-21]) / closes[-21] * 100 if len(closes) > 21 else None
        )
        indicators["period_high"] = max(closes)
        indicators["period_low"] = min(closes)

    if closes:
        indicators["bar_count"] = len(closes)

    return indicators


def format_indicators_for_prompt(indicators: Dict, display_name: str = "") -> str:
    """
    Format indikator menjadi teks ringkas untuk prompt LLM.
    Hanya menyertakan nilai yang tersedia (None di-skip) — hemat token.
    """
    if not indicators:
        return ""

    lines = []
    if display_name:
        lines.append(f"📐 INDIKATOR TEKNIKAL {display_name.upper()}:")

    rsi = indicators.get("rsi")
    if rsi is not None:
        zone = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "netral"
        lines.append(f"• RSI(14): {rsi:.1f} ({zone})")

    macd = indicators.get("macd")
    if macd and macd.get("macd") is not None:
        pos = "positif" if macd["macd"] > 0 else "negatif"
        above = "di atas" if macd["macd"] > macd.get("macd_signal", 0) else "di bawah"
        lines.append(f"• MACD: {macd['macd']:.5f} ({pos}, {above} signal line)")

    stoch = indicators.get("stochastic")
    if stoch is not None:
        lines.append(f"• Stochastic(14): {stoch:.1f}")

    bb = indicators.get("bollinger")
    if bb and bb.get("middle") is not None:
        price = indicators.get("current_price")
        band_pos = ""
        if price is not None:
            if price >= bb.get("upper", float("inf")):
                band_pos = " (menyentuh upper — overextended)"
            elif price <= bb.get("lower", -float("inf")):
                band_pos = " (menyentuh lower — oversold ekstrem)"
        lines.append(
            f"• Bollinger(20,2): {bb.get('lower', 0):.4f} / {bb.get('middle', 0):.4f} / "
            f"{bb.get('upper', 0):.4f}{band_pos}"
        )

    ema20 = indicators.get("ema_20")
    ema50 = indicators.get("ema_50")
    if ema20 and ema50:
        trend = "bullish" if ema20 > ema50 else "bearish"
        lines.append(f"• EMA20 {ema20:.4f} vs EMA50 {ema50:.4f} ({trend})")
    elif ema20:
        lines.append(f"• EMA20: {ema20:.4f}")

    atr = indicators.get("atr")
    price = indicators.get("current_price")
    if atr and price:
        lines.append(f"• ATR(14): {atr:.4f} ({atr / price * 100:.2f}% harga)")

    c5 = indicators.get("price_5d_change")
    c20 = indicators.get("price_20d_change")
    if c5 is not None or c20 is not None:
        parts = []
        if c5 is not None:
            parts.append(f"5d {c5:+.2f}%")
        if c20 is not None:
            parts.append(f"20d {c20:+.2f}%")
        lines.append(f"• Perubahan harga: {', '.join(parts)}")

    # Level kunci: pivot + fibonacci
    levels_str = format_key_levels(indicators)
    if levels_str:
        lines.append(levels_str)

    return "\n".join(lines)


def format_key_levels(indicators: Dict) -> str:
    """Format level support/resistance (pivot + fibonacci) untuk prompt/user."""
    if not indicators:
        return ""

    out = []

    piv = indicators.get("pivot_points")
    if piv and piv.get("pivot") is not None:
        out.append(
            f"• Pivot: {piv['pivot']:.4f} | R1 {piv['r1']:.4f} S1 {piv['s1']:.4f} | "
            f"R2 {piv['r2']:.4f} S2 {piv['s2']:.4f}"
        )

    fib = indicators.get("fibonacci")
    if fib and fib.get("levels"):
        levels = fib["levels"]
        # Resistance = level retracement di atas harga; support = di bawah
        price = indicators.get("current_price")
        if price is not None:
            res = [v for v in levels.values() if v >= price]
            sup = [v for v in levels.values() if v < price]
            if res:
                out.append(f"• Fib resistance: {', '.join(f'{v:.4f}' for v in sorted(res))}")
            if sup:
                out.append(f"• Fib support: {', '.join(f'{v:.4f}' for v in sorted(sup, reverse=True))}")

    return "\n".join(out)



