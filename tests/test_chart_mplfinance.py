"""Unit tests untuk chart generator — mplfinance (primary) + fallback manual.

Tanpa network & tanpa display (backend Agg sudah di-set di chart_generator).
Hanya memverifikasi kontrak API: build_candlestick_chart(ohlcv, symbol) → path
PNG yang valid, dan fallback manual tetap berfungsi bila mplfinance dinonaktifkan.
"""

import os
import unittest
from datetime import datetime, timedelta

from utils import chart_generator
from utils.chart_generator import ChartGenerator


def _sample_ohlcv(n: int = 40, start: str = "2026-06-01") -> list:
    """n bar OHLCV harian sintetis (kronologis naik)."""
    base = datetime.strptime(start, "%Y-%m-%d")
    rows = []
    price = 1.0800
    for i in range(n):
        price += 0.0002 if i % 2 == 0 else -0.0001
        rows.append({
            "date": (base + timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": round(price - 0.0001, 5),
            "high": round(price + 0.0003, 5),
            "low": round(price - 0.0003, 5),
            "close": round(price, 5),
            "volume": 1000 + i * 10,
        })
    return rows


class TestCandlestickMplfinance(unittest.TestCase):
    """Jalur utama: mplfinance terpasang → chart PNG valid."""

    def test_returns_png_path(self):
        chart = ChartGenerator()
        path = chart.build_candlestick_chart(_sample_ohlcv(), "EURUSD=X")
        self.assertIsNotNone(path, "Chart harus menghasilkan file PNG")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(".png"))
        self.assertGreater(os.path.getsize(path), 0, "PNG tidak boleh kosong")
        os.remove(path)

    def test_crypto_symbol(self):
        chart = ChartGenerator()
        path = chart.build_candlestick_chart(_sample_ohlcv(), "BTC-USD")
        self.assertIsNotNone(path)
        os.remove(path)

    def test_intraday_dates_with_time(self):
        """Format tanggal yfinance intraday ('2026-08-05 13:00') harus diparse."""
        rows = []
        base = datetime(2026, 8, 5, 8, 0)
        price = 1.10
        for i in range(30):
            price += 0.0001 if i % 2 == 0 else -0.0001
            rows.append({
                "date": (base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M"),
                "open": price, "high": price + 0.0002,
                "low": price - 0.0002, "close": price + 0.0001,
                "volume": 500,
            })
        chart = ChartGenerator()
        path = chart.build_candlestick_chart(rows, "EURUSD=X")
        self.assertIsNotNone(path)
        os.remove(path)

    def test_insufficient_data_returns_none(self):
        chart = ChartGenerator()
        self.assertIsNone(chart.build_candlestick_chart([], "EURUSD=X"))
        self.assertIsNone(chart.build_candlestick_chart(_sample_ohlcv(1), "EURUSD=X"))

    def test_duplicate_dates_deduplicated(self):
        """Index duplikat (mis. dua bar tanggal sama) tidak boleh menggagalkan mplfinance."""
        rows = _sample_ohlcv(25)
        rows.insert(5, dict(rows[4]))  # duplikat tanggal
        chart = ChartGenerator()
        path = chart.build_candlestick_chart(rows, "EURUSD=X")
        self.assertIsNotNone(path)
        os.remove(path)


class TestCandlestickManualFallback(unittest.TestCase):
    """Jalur fallback: mplfinance tidak tersedia → penggambaran manual lama."""

    def test_fallback_when_mpf_missing(self):
        original = chart_generator.mpf
        chart_generator.mpf = None  # simulasikan mplfinance tidak terpasang
        try:
            chart = ChartGenerator()
            path = chart.build_candlestick_chart(_sample_ohlcv(), "GC=F")
            self.assertIsNotNone(path, "Fallback manual harus tetap menghasilkan PNG")
            self.assertTrue(os.path.exists(path))
            os.remove(path)
        finally:
            chart_generator.mpf = original

    def test_manual_fallback_insufficient_data(self):
        original = chart_generator.mpf
        chart_generator.mpf = None
        try:
            chart = ChartGenerator()
            self.assertIsNone(chart.build_candlestick_chart(_sample_ohlcv(1), "GC=F"))
        finally:
            chart_generator.mpf = original


class TestLineChart(unittest.TestCase):
    def test_line_chart_returns_png(self):
        chart = ChartGenerator()
        prices = [1.08 + i * 0.001 for i in range(20)]
        labels = [f"d{i}" for i in range(20)]
        path = chart.build_line_chart(prices, labels, "EURUSD=X")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        os.remove(path)

    def test_line_chart_insufficient_data(self):
        chart = ChartGenerator()
        self.assertIsNone(chart.build_line_chart([1.0], ["a"], "EURUSD=X"))


class TestParseCandles(unittest.TestCase):
    def test_invalid_rows_skipped(self):
        rows = _sample_ohlcv(5)
        rows[2] = {"date": "invalid-date", "open": "nan", "high": None,
                   "low": "x", "close": "y", "volume": None}
        candles = ChartGenerator._parse_candles(rows, max_points=10)
        self.assertEqual(len(candles), 4, "Baris rusak harus dibuang, bukan membatalkan semuanya")

    def test_max_points_slicing(self):
        candles = ChartGenerator._parse_candles(_sample_ohlcv(20), max_points=5)
        self.assertEqual(len(candles), 5)


if __name__ == "__main__":
    unittest.main()
