"""
Unit tests untuk integrasi OANDA (tanpa network - semua stub).

Mencakup:
- Mapping simbol Yahoo ke instrumen OANDA
- Mapping interval ke granularity + estimasi count candle
- Normalisasi tanggal RFC3339 OANDA
- Logika previous_close (candle harian complete/incomplete)
- Shape data & graceful degrade di MarketDataAggregator._get_oanda_data
"""
import unittest

from data.oanda_client import OandaClient
from data.market_data import MarketDataAggregator


def _daily_candle(close, complete=True, day=5):
    """Satu candle harian OANDA (format sudah dinormalisasi)."""
    return {
        "date": f"2026-08-{day:02d} 00:00:00",
        "open": round(close - 0.001, 5),
        "high": round(close + 0.001, 5),
        "low": round(close - 0.002, 5),
        "close": close,
        "volume": 100,
        "complete": complete,
    }


class _StubClient(OandaClient):
    """Subclass tanpa network - get_candles / get_mid_price di-stub."""

    def __init__(self, daily_candles=None, mid=1.0825, bid=1.0824, ask=1.0826):
        super().__init__(api_key="test-key", account_id="123")
        self._daily = daily_candles or []
        self._mid, self._bid, self._ask = mid, bid, ask

    def get_candles(self, instrument, granularity="H1", count=120):
        if granularity == "D":
            return list(self._daily)
        return [_daily_candle(1.0850, day=i) for i in range(1, min(count, 5) + 1)]

    def get_mid_price(self, instrument):
        return {"mid": self._mid, "bid": self._bid, "ask": self._ask, "time": ""}


class TestOandaMapping(unittest.TestCase):

    def test_instrument_for(self):
        c = OandaClient(api_key="")
        self.assertEqual(c.instrument_for("EURUSD=X"), "EUR_USD")
        self.assertEqual(c.instrument_for("GBPUSD=X"), "GBP_USD")
        self.assertEqual(c.instrument_for("USDJPY=X"), "USD_JPY")
        self.assertEqual(c.instrument_for("GC=F"), "XAU_USD")
        self.assertEqual(c.instrument_for("SI=F"), "XAG_USD")
        # Instrumen non-OANDA tidak boleh di-routing
        self.assertIsNone(c.instrument_for("USDIDR=X"))
        self.assertIsNone(c.instrument_for("DX-Y.NYB"))
        self.assertIsNone(c.instrument_for("BTC-USD"))

    def test_granularity_for(self):
        c = OandaClient(api_key="")
        self.assertEqual(c.granularity_for("1m"), "M1")
        self.assertEqual(c.granularity_for("5m"), "M5")
        self.assertEqual(c.granularity_for("30m"), "M30")
        self.assertEqual(c.granularity_for("1h"), "H1")
        self.assertEqual(c.granularity_for("60m"), "H1")
        self.assertEqual(c.granularity_for("1d"), "D")
        self.assertEqual(c.granularity_for("1wk"), "W")
        self.assertEqual(c.granularity_for("1mo"), "M")
        self.assertEqual(c.granularity_for("tidak-kenal"), "H1")

    def test_count_for(self):
        c = OandaClient(api_key="")
        self.assertEqual(c.count_for("5d", "1h", 60), 120)
        self.assertEqual(c.count_for("3mo", "1d", 60), 90)
        self.assertEqual(c.count_for("1mo", "1d", 60), 60)
        self.assertGreaterEqual(c.count_for("1d", "1h", 5), 24)
        self.assertLessEqual(c.count_for("1mo", "1h", 5), 5000)

    def test_normalize_date(self):
        c = OandaClient(api_key="")
        self.assertEqual(
            c._normalize_date("2026-08-05T13:00:00.000000000Z"),
            "2026-08-05 13:00:00",
        )
        self.assertEqual(c._normalize_date("2026-08-05T00:00:00Z"), "2026-08-05 00:00:00")
        self.assertEqual(c._normalize_date(""), "")

    def test_is_configured(self):
        self.assertFalse(OandaClient(api_key="").is_configured)
        self.assertTrue(OandaClient(api_key="abc").is_configured)
        self.assertEqual(OandaClient(api_key="abc", env="practice").env_name, "Demo")
        self.assertEqual(OandaClient(api_key="abc", env="live").env_name, "Live")


class TestOandaPreviousClose(unittest.TestCase):
    """get_previous_close: candle harian OANDA (complete/incomplete)."""

    def test_last_incomplete_uses_last_complete(self):
        # Hari ini (5) masih berjalan -> previous close = kemarin (4) = 1.0800
        candles = [
            _daily_candle(1.0790, day=3),
            _daily_candle(1.0800, day=4),
            _daily_candle(1.0810, complete=False, day=5),
        ]
        c = _StubClient(daily_candles=candles)
        self.assertEqual(c.get_previous_close("EUR_USD"), 1.0800)

    def test_last_complete_uses_second_to_last(self):
        # Hari ini (6) sudah complete (pasar tutup) -> previous close = kemarin (5)
        candles = [
            _daily_candle(1.0790, day=4),
            _daily_candle(1.0800, day=5),
            _daily_candle(1.0810, day=6),
        ]
        c = _StubClient(daily_candles=candles)
        self.assertEqual(c.get_previous_close("EUR_USD"), 1.0800)

    def test_single_complete_candle(self):
        c = _StubClient(daily_candles=[_daily_candle(1.0800, day=5)])
        self.assertEqual(c.get_previous_close("EUR_USD"), 1.0800)

    def test_no_complete_candles_returns_none(self):
        c = _StubClient(daily_candles=[_daily_candle(1.0810, complete=False, day=5)])
        self.assertIsNone(c.get_previous_close("EUR_USD"))

    def test_empty_candles_returns_none(self):
        c = _StubClient(daily_candles=[])
        self.assertIsNone(c.get_previous_close("EUR_USD"))


class TestOandaAggregator(unittest.TestCase):
    """Shape data & degrade behavior di MarketDataAggregator._get_oanda_data."""

    def test_get_oanda_data_shape(self):
        agg = MarketDataAggregator()
        # Skenario: candle terakhir (hari ini) masih berjalan (incomplete),
        # jadi previous_close = close complete terakhir = 1.0790 (kemarin).
        agg.oanda = _StubClient(
            daily_candles=[
                _daily_candle(1.0800, day=4),
                _daily_candle(1.0790, day=5),
                _daily_candle(1.0780, complete=False, day=6),
            ],
            mid=1.0825, bid=1.0824, ask=1.0826,
        )
        result = agg._get_oanda_data("EURUSD=X", "5d", "1h", 5)

        self.assertEqual(result["source"], "OANDA (Demo)")
        self.assertEqual(result["symbol"], "EURUSD=X")
        self.assertEqual(result["instrument"], "EUR_USD")
        self.assertEqual(result["current_price"], 1.0825)
        self.assertEqual(result["bid"], 1.0824)
        self.assertEqual(result["ask"], 1.0826)
        self.assertEqual(result["previous_close"], 1.0790)
        self.assertAlmostEqual(result["change_pct"], round((1.0825 - 1.0790) / 1.0790 * 100, 2))
        self.assertEqual(len(result["ohlcv"]), 5)
        self.assertIn("date", result["ohlcv"][0])
        self.assertIn("volume", result["ohlcv"][0])
        self.assertIsNone(result["high_52w"])
        self.assertIsNone(result["low_52w"])
        self.assertIsNone(result["market_cap"])

    def test_get_oanda_data_degrades_when_pricing_fails(self):
        class _NoPrice(_StubClient):
            def get_mid_price(self, instrument):
                raise RuntimeError("pricing down")

        agg = MarketDataAggregator()
        agg.oanda = _NoPrice(daily_candles=[_daily_candle(1.0800, day=5)])
        result = agg._get_oanda_data("EURUSD=X", "5d", "1h", 5)

        # Tanpa pricing -> fallback ke close candle terakhir (1.0850 dari stub)
        self.assertEqual(result["current_price"], 1.0850)
        self.assertIsNone(result["bid"])

    def test_get_oanda_data_degrades_when_daily_close_fails(self):
        class _NoDaily(_StubClient):
            def get_previous_close(self, instrument):
                raise RuntimeError("daily down")

        agg = MarketDataAggregator()
        agg.oanda = _NoDaily(daily_candles=[_daily_candle(1.0800, day=5)], mid=1.0825)
        result = agg._get_oanda_data("EURUSD=X", "5d", "1h", 5)

        # Harga real-time tetap ada walau previous_close gagal
        self.assertEqual(result["current_price"], 1.0825)
        self.assertIsNone(result["previous_close"])
        self.assertIsNone(result["change_pct"])

    def test_get_yahoo_data_routes_to_oanda_when_configured(self):
        from data.cache import cache
        cache.clear()
        agg = MarketDataAggregator()
        agg.oanda = _StubClient(
            daily_candles=[_daily_candle(1.0800, day=4), _daily_candle(1.0790, day=5)],
            mid=1.0825,
        )
        result = agg.get_yahoo_data("EURUSD=X", period="5d", interval="1h", ohlcv_limit=5)
        self.assertEqual(result["source"], "OANDA (Demo)")
        self.assertEqual(result["current_price"], 1.0825)

    def test_get_yahoo_data_skips_oanda_when_not_configured(self):
        agg = MarketDataAggregator()
        agg.oanda = OandaClient(api_key="")  # tidak terkonfigurasi -> jalan Yahoo
        self.assertFalse(agg.oanda.is_configured)


if __name__ == "__main__":
    unittest.main()
