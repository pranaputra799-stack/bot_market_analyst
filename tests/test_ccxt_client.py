"""Unit tests untuk ccxt client (harga crypto real-time) & integrasi market_data.

Semua exchange di-mock — tidak ada network. Menguji:
- Mapping simbol Yahoo → ccxt.
- Multi-exchange failover (exchange pertama yang sukses dipakai).
- Cache pendek: panggilan kedua tidak memanggil exchange lagi.
- Semua exchange gagal → None (caller fallback ke Yahoo).
- market_data: query harga spot (ohlcv_limit<=5) memakai ccxt; query OHLCV
  (ohlcv_limit=60, untuk chart/analisis) TIDAK di-intercept.
"""

import unittest
from unittest import mock

import data.market_data as md
from data.cache import cache
from data.ccxt_client import (
    CRYPTO_SYMBOLS,
    get_crypto_ticker,
    get_crypto_ohlcv,
    CCXT_PRICE_TTL,
)


class _FakeExchange:
    """Exchange ccxt tiruan — fetch_ticker & fetch_ohlcv, bisa di-set gagal."""

    def __init__(self, ticker=None, fail=False, ohlcv=None, fail_ohlcv=False,
                 fail_ohlcv_symbols=()):
        self.ticker = ticker or {
            "symbol": "BTC/USD", "last": 67000.5, "bid": 66999.5,
            "ask": 67001.5, "high": 67500.0, "low": 66000.0,
            "baseVolume": 12345.0, "percentage": 2.35, "timestamp": 0,
        }
        self.fail = fail
        self.ohlcv = ohlcv or [
            [1710000000000 + i * 86400000, 67000.0, 67100.0, 66900.0, 67050.0, 1000 + i]
            for i in range(5)
        ]
        self.fail_ohlcv = fail_ohlcv
        self.fail_ohlcv_symbols = set(fail_ohlcv_symbols)
        self.calls = []

    def fetch_ticker(self, symbol):
        self.calls.append(symbol)
        if self.fail:
            raise Exception("mock exchange down")
        return self.ticker

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=60):
        self.calls.append(("ohlcv", symbol, timeframe, limit))
        if self.fail_ohlcv or symbol in self.fail_ohlcv_symbols:
            raise Exception("mock ohlcv down")
        return self.ohlcv[:limit]


class TestCryptoSymbols(unittest.TestCase):
    def test_mapping(self):
        self.assertIn("BTC-USD", CRYPTO_SYMBOLS)
        self.assertIn("ETH-USD", CRYPTO_SYMBOLS)
        base, sym_usd, sym_usdt = CRYPTO_SYMBOLS["BTC-USD"]
        self.assertEqual(base, "BTC")
        self.assertEqual(sym_usd, "BTC/USD")
        self.assertEqual(sym_usdt, "BTC/USDT")


class TestGetCryptoTicker(unittest.TestCase):
    def setUp(self):
        cache.delete("ccxt:BTC-USD")

    def test_success_first_exchange(self):
        fake = _FakeExchange()
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            result = get_crypto_ticker("BTC-USD")
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "ccxt (binance)")
        self.assertEqual(result["symbol"], "BTC-USD")
        self.assertEqual(result["current_price"], 67000.5)
        self.assertEqual(result["change_pct"], 2.35)
        # previous_close = last / (1 + pct/100) = 67000.5 / 1.0235
        self.assertAlmostEqual(result["previous_close"], 67000.5 / 1.0235, places=4)
        self.assertIn("ohlcv", result)
        self.assertEqual(result["ohlcv"], [])  # OHLCV tidak disediakan ccxt

    def test_cached_second_call_skips_exchange(self):
        fake = _FakeExchange()
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            first = get_crypto_ticker("BTC-USD")
            second = get_crypto_ticker("BTC-USD")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["current_price"], second["current_price"])
        self.assertEqual(len(fake.calls), 1, "Panggilan kedua harus dari cache (TTL pendek)")

    def test_all_exchanges_fail_returns_none(self):
        fake = _FakeExchange(fail=True)
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            result = get_crypto_ticker("BTC-USD")
        self.assertIsNone(result)
        self.assertGreater(len(fake.calls), 0)  # semua symbol dicoba

    def test_failure_negative_cached(self):
        """Setelah semua exchange gagal, panggilan berikutnya TIDAK menghantam
        exchange lagi (negative cache pendek → anti-amplifikasi saat down)."""
        fake = _FakeExchange(fail=True)
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            first = get_crypto_ticker("BTC-USD")
            calls_after_first = len(fake.calls)
            second = get_crypto_ticker("BTC-USD")
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            len(fake.calls), calls_after_first,
            "Panggilan kedua harus pakai negative cache, bukan retry 12x",
        )

    def test_unsupported_symbol_returns_none(self):
        with mock.patch("data.ccxt_client._get_exchange") as m:
            result = get_crypto_ticker("EURUSD=X")
        self.assertIsNone(result)
        m.assert_not_called()

    def test_ticker_without_last_returns_none(self):
        fake = _FakeExchange(ticker={"symbol": "BTC/USD", "last": None})
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            result = get_crypto_ticker("BTC-USD")
        self.assertIsNone(result)


class TestGetCryptoOhlcv(unittest.TestCase):
    """Candle OHLCV real-time dari ccxt (chart BTC/ETH & analisis teknikal)."""

    def setUp(self):
        cache.delete("ccxtohlcv:BTC-USD:1d:60")
        cache.delete("ccxtohlcv:BTC-USD:1d:5")

    def test_success_converts_bars(self):
        fake = _FakeExchange()
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            result = get_crypto_ohlcv("BTC-USD", interval="1d", limit=60)
        self.assertEqual(len(result), 5)
        bar = result[0]
        self.assertIn("date", bar)
        self.assertRegex(bar["date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(bar["open"], 67000.0)
        self.assertEqual(bar["high"], 67100.0)
        self.assertEqual(bar["low"], 66900.0)
        self.assertEqual(bar["close"], 67050.0)
        self.assertEqual(bar["volume"], 1000)
        # Kronologis naik (ts bertambah)
        dates = [b["date"] for b in result]
        self.assertEqual(dates, sorted(dates))

    def test_timeframe_mapping(self):
        fake = _FakeExchange()
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            get_crypto_ohlcv("BTC-USD", interval="1mo", limit=60)
            get_crypto_ohlcv("BTC-USD", interval="1h", limit=60)
        timeframes = [c[2] for c in fake.calls if c[0] == "ohlcv"]
        self.assertEqual(timeframes, ["1M", "1h"])  # yfinance → ccxt

    def test_limit_passed_through(self):
        fake = _FakeExchange()
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            get_crypto_ohlcv("BTC-USD", interval="1d", limit=30)
        self.assertEqual(fake.calls[0], ("ohlcv", "BTC/USD", "1d", 30))

    def test_fallback_usd_to_usdt(self):
        """BTC/USD gagal di exchange → coba BTC/USDT."""
        fake = _FakeExchange(fail_ohlcv_symbols=("BTC/USD",))
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            result = get_crypto_ohlcv("BTC-USD", interval="1d", limit=60)
        self.assertEqual(len(result), 5)
        symbols = [c[1] for c in fake.calls if c[0] == "ohlcv"]
        self.assertIn("BTC/USDT", symbols)

    def test_cached_second_call_skips_exchange(self):
        fake = _FakeExchange()
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            first = get_crypto_ohlcv("BTC-USD", interval="1d", limit=60)
            second = get_crypto_ohlcv("BTC-USD", interval="1d", limit=60)
        self.assertEqual(len(first), 5)
        self.assertEqual(second, first)
        ohlcv_calls = [c for c in fake.calls if c[0] == "ohlcv"]
        self.assertEqual(len(ohlcv_calls), 1, "Panggilan kedua harus dari cache")

    def test_all_exchanges_fail_returns_empty_and_negative_cached(self):
        fake = _FakeExchange(fail_ohlcv=True)
        with mock.patch("data.ccxt_client._get_exchange", return_value=fake):
            first = get_crypto_ohlcv("BTC-USD", interval="1d", limit=60)
            calls_after_first = len(fake.calls)
            second = get_crypto_ohlcv("BTC-USD", interval="1d", limit=60)
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(
            len(fake.calls), calls_after_first,
            "Panggilan kedua harus pakai negative cache ([]), bukan retry beruntun",
        )

    def test_unsupported_symbol_returns_empty(self):
        with mock.patch("data.ccxt_client._get_exchange") as m:
            result = get_crypto_ohlcv("EURUSD=X", interval="1d", limit=60)
        self.assertEqual(result, [])
        m.assert_not_called()


class TestMarketDataIntegration(unittest.TestCase):
    """get_yahoo_data harus memakai ccxt untuk harga spot crypto, tapi TIDAK
    untuk query OHLCV (chart / analisis teknikal tetap via Yahoo)."""

    def setUp(self):
        cache.delete("ccxt:BTC-USD")
        cache.delete("yahoo:BTC-USD:1d:1h:n5")
        cache.delete("yahoo:BTC-USD:1d:1h:n60")

    def test_spot_price_uses_ccxt(self):
        fake_ticker = {
            "source": "ccxt (mock)",
            "symbol": "BTC-USD",
            "current_price": 67000.5,
            "change_pct": 2.35,
            "ohlcv": [],
        }
        agg = md.MarketDataAggregator()
        with mock.patch.object(md, "get_crypto_ticker", return_value=fake_ticker) as m, \
                mock.patch.object(md, "OANDA_SYMBOLS", {}):
            result = agg.get_yahoo_data("BTC-USD", period="1d", interval="1h")
        m.assert_called_once_with("BTC-USD")
        self.assertEqual(result["source"], "ccxt (mock)")
        self.assertEqual(result["current_price"], 67000.5)

    def test_ohlcv_query_uses_ccxt(self):
        """ohlcv_limit=60 (chart/analisis teknikal) → candle ccxt real-time."""
        candles = [
            {"date": "2026-07-01 00:00", "open": 66000.0, "high": 67000.0,
             "low": 65500.0, "close": 66800.0, "volume": 1000},
            {"date": "2026-07-02 00:00", "open": 66800.0, "high": 67500.0,
             "low": 66500.0, "close": 67000.0, "volume": 1200},
        ]
        agg = md.MarketDataAggregator()
        with mock.patch.object(md, "get_crypto_ohlcv", return_value=candles) as m, \
                mock.patch.object(md, "OANDA_SYMBOLS", {}):
            result = agg.get_yahoo_data("BTC-USD", period="1mo", interval="1d", ohlcv_limit=60)
        m.assert_called_once_with("BTC-USD", interval="1d", limit=60)
        self.assertIn("ccxt", result["source"])
        self.assertEqual(result["ohlcv"], candles)
        self.assertEqual(result["current_price"], 67000.0)  # close terakhir
        self.assertEqual(result["change_pct"], round((67000.0 - 66800.0) / 66800.0 * 100, 2))

    def test_ohlcv_ccxt_failure_falls_back_to_yahoo(self):
        """ccxt OHLCV gagal → tetap fallback ke Yahoo (negative cache Yahoo)."""
        agg = md.MarketDataAggregator()
        with mock.patch.object(md, "get_crypto_ohlcv", return_value=[]) as m, \
                mock.patch.object(md, "OANDA_SYMBOLS", {}):
            cache.set("yahoo:BTC-USD:1d:1d:n60", {"error": "mock cached"}, 60)
            result = agg.get_yahoo_data("BTC-USD", period="1d", interval="1d", ohlcv_limit=60)
        m.assert_called_once_with("BTC-USD", interval="1d", limit=60)
        self.assertEqual(result.get("error"), "mock cached")

    def test_ccxt_failure_falls_back_to_yahoo(self):
        agg = md.MarketDataAggregator()
        with mock.patch.object(md, "get_crypto_ticker", return_value=None) as m, \
                mock.patch.object(md, "OANDA_SYMBOLS", {}):
            cache.set("yahoo:BTC-USD:1d:1h:n5", {"error": "mock yahoo down"}, 60)
            result = agg.get_yahoo_data("BTC-USD", period="1d", interval="1h")
        m.assert_called_once_with("BTC-USD")
        self.assertEqual(result.get("error"), "mock yahoo down")

    def test_price_ttl_is_short(self):
        self.assertLessEqual(CCXT_PRICE_TTL, 60)


if __name__ == "__main__":
    unittest.main()
