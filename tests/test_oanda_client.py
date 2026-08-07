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
        # Instrumen baru: cross pair, index, oil, crypto
        self.assertEqual(c.instrument_for("EURGBP=X"), "EUR_GBP")
        self.assertEqual(c.instrument_for("GBPJPY=X"), "GBP_JPY")
        self.assertEqual(c.instrument_for("^GSPC"), "SPX500_USD")
        self.assertEqual(c.instrument_for("^DJI"), "US30_USD")
        self.assertEqual(c.instrument_for("CL=F"), "WTICO_USD")
        self.assertEqual(c.instrument_for("BTC-USD"), "BTC_USD")
        self.assertEqual(c.instrument_for("ETH-USD"), "ETH_USD")
        # Instrumen non-OANDA tidak boleh di-routing
        self.assertIsNone(c.instrument_for("USDIDR=X"))
        self.assertIsNone(c.instrument_for("DX-Y.NYB"))
        self.assertIsNone(c.instrument_for("^VIX"))

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

    def test_is_forex(self):
        self.assertTrue(OandaClient.is_forex("EUR_USD"))
        self.assertTrue(OandaClient.is_forex("GBP_JPY"))
        self.assertTrue(OandaClient.is_forex("USD_IDR"))
        # Bukan pair forex: logam, index, oil, crypto
        self.assertFalse(OandaClient.is_forex("XAU_USD"))
        self.assertFalse(OandaClient.is_forex("SPX500_USD"))
        self.assertFalse(OandaClient.is_forex("WTICO_USD"))
        self.assertFalse(OandaClient.is_forex("BTC_USD"))
        self.assertFalse(OandaClient.is_forex(""))
        self.assertFalse(OandaClient.is_forex(None))


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
        self.assertEqual(result["spread"], round(1.0826 - 1.0824, 8))
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


class _FakeResp:
    """Respons requests stub (tanpa network)."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Session stub: positionBook / orderBook -> payload sesuai URL."""

    def __init__(self, position=None, order=None, fail=""):
        self._position = position or {
            "positionBook": {
                "time": "2026-08-07T00:00:00Z",
                "buckets": [
                    {"price": "1.0800", "longCount": 60, "shortCount": 40},
                    {"price": "1.0900", "longCount": 50, "shortCount": 50},
                ],
            }
        }
        self._order = order or {
            "orderBook": {
                "time": "2026-08-07T00:00:00Z",
                "price": "1.0825",
                "buckets": [{"price": "1.0800", "buyCount": 70, "sellCount": 30}],
            }
        }
        self._fail = fail

    def get(self, url, **kwargs):
        if self._fail in ("position", "both") and "positionBook" in url:
            raise RuntimeError("position down")
        if self._fail in ("order", "both") and "orderBook" in url:
            raise RuntimeError("order down")
        if "positionBook" in url:
            return _FakeResp(self._position)
        if "orderBook" in url:
            return _FakeResp(self._order)
        raise AssertionError(f"Unexpected URL: {url}")


class TestOandaRetailSentiment(unittest.TestCase):
    """Position/Order book OANDA — sentimen retail (semua stub, tanpa network)."""

    def _client(self, **kw):
        c = OandaClient(api_key="test", account_id="123")
        c._session = _FakeSession(**kw)
        return c

    def test_position_book_ratios(self):
        c = self._client()
        pb = c.get_position_book("EUR_USD")
        # long: 60+50=110, short: 40+50=90, total 200 -> long 55%
        self.assertEqual(pb["long_ratio"], 55.0)
        self.assertEqual(pb["short_ratio"], 45.0)
        self.assertIn("positions", pb)

    def test_order_book_ratios(self):
        c = self._client()
        ob = c.get_order_book("EUR_USD")
        self.assertEqual(ob["buy_ratio"], 70.0)
        self.assertEqual(ob["sell_ratio"], 30.0)
        self.assertEqual(ob["price"], "1.0825")

    def test_retail_sentiment_merges_both(self):
        c = self._client()
        sent = c.get_retail_sentiment("EUR_USD")
        self.assertEqual(sent["long_ratio"], 55.0)
        self.assertEqual(sent["buy_ratio"], 70.0)
        self.assertEqual(sent["instrument"], "EUR_USD")

    def test_retail_sentiment_degrades_when_order_book_fails(self):
        c = self._client(fail="order")
        sent = c.get_retail_sentiment("EUR_USD")
        self.assertEqual(sent["long_ratio"], 55.0)
        self.assertNotIn("buy_ratio", sent)

    def test_retail_sentiment_degrades_when_position_book_fails(self):
        c = self._client(fail="position")
        sent = c.get_retail_sentiment("EUR_USD")
        self.assertNotIn("long_ratio", sent)
        self.assertEqual(sent["buy_ratio"], 70.0)

    def test_retail_sentiment_raises_when_both_fail(self):
        c = self._client(fail="both")
        with self.assertRaises(RuntimeError):
            c.get_retail_sentiment("EUR_USD")


if __name__ == "__main__":
    unittest.main()
