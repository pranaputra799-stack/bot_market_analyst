"""Unit tests untuk modul indikator teknikal lokal (tanpa network)."""

import unittest

from analysis.indicators import (
    compute_indicators,
    format_indicators_for_prompt,
    format_key_levels,
    _rsi,
    _macd,
    _sma,
    _ema,
    _ema_series,
    _atr,
    _stochastic,
    _pivot_points,
)


def _make_bars(count=60, start=1.0, step=0.01):
    """Synthetic OHLCV dengan tren naik konsisten."""
    bars = []
    for i in range(count):
        close = start + i * step
        bars.append({
            "date": f"2026-08-{(i % 28) + 1:02d}",
            "open": close - step,
            "high": close + step * 0.5,
            "low": close - step * 1.5,
            "close": close,
            "volume": 1000 + i * 10,
        })
    return bars


def _make_flat_bars(count=40, price=1.1):
    """Harga datar (untuk test RSI = 100 / ATR kecil)."""
    return [{
        "date": f"2026-08-{(i % 28) + 1:02d}",
        "open": price, "high": price, "low": price, "close": price, "volume": 1000,
    } for i in range(count)]


class TestBasicStats(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(_sma([1, 2, 3, 4, 5], 5), 3.0)
        self.assertIsNone(_sma([1, 2], 5))

    def test_ema_short(self):
        ema = _ema([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3)
        self.assertIsNotNone(ema)
        self.assertAlmostEqual(ema, 9.0, places=4)  # EMA konvergen ke harga terakhir


class TestRSI(unittest.TestCase):
    def test_rsi_all_gains_is_100(self):
        closes = list(range(1, 30))
        self.assertEqual(_rsi(closes, 14), 100.0)

    def test_rsi_insufficient_data(self):
        self.assertIsNone(_rsi([1, 2, 3], 14))

    def test_rsi_in_range(self):
        closes = [1.0 + (i % 5) * 0.01 for i in range(30)]
        rsi = _rsi(closes, 14)
        self.assertIsNotNone(rsi)
        self.assertGreaterEqual(rsi, 0.0)
        self.assertLessEqual(rsi, 100.0)

    def test_rsi_flat_market_is_neutral(self):
        # Pasar datar total harus netral (~50), BUKAN 100 (overbought palsu)
        self.assertEqual(_rsi([1.0] * 30, 14), 50.0)


class TestMACD(unittest.TestCase):
    def test_macd_uptrend(self):
        # Uptrend EKSPONENSIAL (mengakselerasi): macd positif & di atas signal
        # (tren linear membuat macd ≈ signal — itu perilaku matematis yang benar)
        closes = [1.0 * (1.02 ** i) for i in range(40)]
        macd = _macd(closes)
        self.assertIsNotNone(macd)
        self.assertIn("macd", macd)
        self.assertIn("macd_signal", macd)
        self.assertIn("macd_hist", macd)
        self.assertGreater(macd["macd"], 0)
        self.assertGreater(macd["macd"], macd["macd_signal"])

    def test_macd_downtrend(self):
        closes = [1.0 * (0.98 ** i) for i in range(40)]
        macd = _macd(closes)
        self.assertIsNotNone(macd)
        self.assertLess(macd["macd"], 0)

    def test_macd_insufficient(self):
        self.assertIsNone(_macd([1.0] * 10))


class TestEMASeries(unittest.TestCase):
    def test_ema_series_last_value(self):
        series = _ema_series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3)
        self.assertEqual(len(series), 10)
        self.assertAlmostEqual(series[-1], 9.0, places=4)
        # Bar sebelum period cukup bernilai None
        self.assertIsNone(series[1])
        self.assertIsNotNone(series[2])


class TestATRAndStochastic(unittest.TestCase):
    def test_atr_flat_is_zero(self):
        atr = _atr(_make_flat_bars())
        self.assertIsNotNone(atr)
        self.assertAlmostEqual(atr, 0.0, places=6)

    def test_atr_trending_positive(self):
        atr = _atr(_make_bars())
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0.0)

    def test_stochastic_in_range(self):
        stoch = _stochastic(_make_bars())
        self.assertIsNotNone(stoch)
        self.assertGreaterEqual(stoch, 0.0)
        self.assertLessEqual(stoch, 100.0)


class TestPivots(unittest.TestCase):
    def test_pivot_formula(self):
        bars = [{"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 0}]
        piv = _pivot_points(bars)
        self.assertIsNotNone(piv)
        self.assertAlmostEqual(piv["pivot"], (1.1 + 0.9 + 1.05) / 3, places=6)
        self.assertAlmostEqual(piv["r1"], 2 * piv["pivot"] - 0.9, places=6)
        self.assertAlmostEqual(piv["s1"], 2 * piv["pivot"] - 1.1, places=6)


class TestComputeIndicators(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(compute_indicators([]), {})

    def test_short_input_no_crash(self):
        ind = compute_indicators(_make_bars(3))
        self.assertIsInstance(ind, dict)

    def test_full_indicators_present(self):
        ind = compute_indicators(_make_bars(60))
        self.assertIsNotNone(ind.get("rsi"))
        self.assertIsNotNone(ind.get("macd"))
        self.assertIsNotNone(ind.get("bollinger"))
        self.assertIsNotNone(ind.get("atr"))
        self.assertIsNotNone(ind.get("stochastic"))
        self.assertIsNotNone(ind.get("ema_20"))
        self.assertIsNotNone(ind.get("sma_20"))
        self.assertIsNotNone(ind.get("pivot_points"))
        self.assertIsNotNone(ind.get("fibonacci"))
        self.assertIsNotNone(ind.get("current_price"))

    def test_format_prompt_returns_text(self):
        ind = compute_indicators(_make_bars(60))
        text = format_indicators_for_prompt(ind, "EUR/USD")
        self.assertIn("EUR/USD", text)
        self.assertIn("RSI", text)

    def test_format_empty(self):
        self.assertEqual(format_indicators_for_prompt({}), "")
        self.assertEqual(format_key_levels({}), "")

    def test_key_levels_format(self):
        ind = compute_indicators(_make_bars(60))
        levels = format_key_levels(ind)
        self.assertIsInstance(levels, str)


if __name__ == "__main__":
    unittest.main()
