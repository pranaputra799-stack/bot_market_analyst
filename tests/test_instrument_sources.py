"""
Unit tests: peta sumber data per instrumen untuk /status.

Fitur: /status menampilkan sumber data per instrumen (OANDA vs ccxt vs Yahoo)
agar beban yfinance mudah divalidasi:
- plan   = sumber yang AKAN dipakai berdasarkan konfigurasi (tanpa network)
- actual = sumber yang TERAKHIR benar-benar dipakai saat fetch

Semua test murni (tanpa network) — OANDA/cache di-stub.
"""

import time
import unittest

import bot.messages as msgs
from data.market_data import MarketDataAggregator
from data.oanda_client import OandaClient
from data.cache import cache


class _DailyCandle:
    """Satu candle harian OANDA (format sudah dinormalisasi)."""

    def __init__(self, close, day=5):
        self.data = {
            "date": f"2026-08-{day:02d} 00:00:00",
            "open": round(close - 0.001, 5),
            "high": round(close + 0.001, 5),
            "low": round(close - 0.002, 5),
            "close": close,
            "volume": 100,
            "complete": True,
        }

    def get(self, key, default=None):
        return self.data.get(key, default)


class _StubOanda(OandaClient):
    """OANDA stub tanpa network — is_configured=True, harga fixed."""

    def __init__(self):
        super().__init__(api_key="test-key", account_id="123")

    def get_candles(self, instrument, granularity="H1", count=120):
        if granularity == "D":
            return [_DailyCandle(1.0800, day=4).data, _DailyCandle(1.0790, day=5).data]
        return [_DailyCandle(1.0850, day=i).data for i in range(1, min(count, 5) + 1)]

    def get_mid_price(self, instrument):
        return {"mid": 1.0825, "bid": 1.0824, "ask": 1.0826, "time": ""}

    def get_previous_close(self, instrument):
        return 1.0800


class TestInstrumentSourceStatus(unittest.TestCase):
    """get_instrument_source_status: pemetaan plan per konfigurasi."""

    def setUp(self):
        cache.clear()

    def test_plan_map_with_oanda_configured(self):
        agg = MarketDataAggregator()
        agg.oanda = _StubOanda()
        rows = {r["symbol"]: r for r in agg.get_instrument_source_status()}

        # Forex & gold & S&P 500 → OANDA (terkonfigurasi & didukung)
        self.assertIn("OANDA", rows["EURUSD=X"]["plan"])
        self.assertIn("OANDA", rows["GC=F"]["plan"])
        self.assertIn("OANDA", rows["^GSPC"]["plan"])
        # IDR & DXY tidak didukung OANDA → Yahoo
        self.assertEqual(rows["USDIDR=X"]["plan"], "Yahoo Finance")
        self.assertEqual(rows["DX-Y.NYB"]["plan"], "Yahoo Finance")
        # Crypto: OANDA mendukung BTC/ETH di sebagian entity — plan OANDA dulu
        # (ccxt baru dipakai saat OANDA tidak terkonfigurasi/tidak didukung).
        self.assertIn("OANDA", rows["BTC-USD"]["plan"])
        self.assertIn("OANDA", rows["ETH-USD"]["plan"])

    def test_plan_map_without_oanda_all_yahoo_except_crypto(self):
        agg = MarketDataAggregator()
        agg.oanda = OandaClient(api_key="")  # tidak terkonfigurasi
        rows = {r["symbol"]: r for r in agg.get_instrument_source_status()}

        self.assertEqual(rows["EURUSD=X"]["plan"], "Yahoo Finance")
        self.assertEqual(rows["GC=F"]["plan"], "Yahoo Finance")
        self.assertIn("ccxt", rows["BTC-USD"]["plan"])

    def test_actual_recorded_after_oanda_fetch(self):
        agg = MarketDataAggregator()
        agg.oanda = _StubOanda()
        result = agg.get_yahoo_data("EURUSD=X", period="5d", interval="1h", ohlcv_limit=5)

        self.assertEqual(result["source"], "OANDA (Demo)")
        rows = {r["symbol"]: r for r in agg.get_instrument_source_status()}
        self.assertEqual(rows["EURUSD=X"]["actual"], "OANDA (Demo)")

    def test_actual_none_before_any_fetch(self):
        agg = MarketDataAggregator()
        agg.oanda = OandaClient(api_key="")
        rows = {r["symbol"]: r for r in agg.get_instrument_source_status()}
        self.assertIsNone(rows["EURUSD=X"]["actual"])

    def test_actual_error_recorded_on_total_failure(self):
        import data.market_data as md
        agg = MarketDataAggregator()
        agg.oanda = OandaClient(api_key="")  # non-OANDA → jalur Yahoo

        def boom(*a, **k):
            raise RuntimeError("network down")

        original = md._get_yf_context
        md._get_yf_context = boom
        try:
            agg.get_yahoo_data("EURUSD=X")
        finally:
            md._get_yf_context = original

        rows = {r["symbol"]: r for r in agg.get_instrument_source_status()}
        self.assertEqual(rows["EURUSD=X"]["actual"], "ERROR")


class TestDataSourcesStatusRendering(unittest.TestCase):
    """get_data_sources_status menampilkan blok 'Sumber per Instrumen'."""

    def setUp(self):
        msgs._DATA_STATUS_CACHE["ts"] = 0.0
        msgs._DATA_STATUS_CACHE["value"] = ""

    def _fake_macro(self):
        class FakeMacro:
            fred_key = ""
        return FakeMacro()

    def test_block_rendered_with_rows(self):
        class FakeMarket:
            oanda = type("O", (), {"is_configured": True, "env_name": "Demo"})()
            alpha_key = ""
            finnhub_key = ""

            @staticmethod
            def get_yahoo_data(*a, **k):
                return {"current_price": 1.0825, "source": "OANDA (Demo)"}

            @staticmethod
            def get_instrument_source_status():
                return [
                    {"symbol": "EURUSD=X", "display": "EUR/USD", "plan": "OANDA (Demo)", "actual": "OANDA (Demo)"},
                    {"symbol": "USDIDR=X", "display": "USD/IDR", "plan": "Yahoo Finance", "actual": "Yahoo Finance"},
                    {"symbol": "BTC-USD", "display": "BTC/USD", "plan": "ccxt (real-time)", "actual": None},
                ]

        out = msgs.get_data_sources_status(FakeMarket(), self._fake_macro(), None)
        self.assertIn("Sumber per Instrumen", out)
        self.assertIn("EUR/USD", out)
        self.assertIn("USD/IDR", out)
        self.assertIn("BTC/USD", out)

    def test_fallback_warning_when_actual_yahoo_but_plan_oanda(self):
        class FakeMarket:
            oanda = type("O", (), {"is_configured": True, "env_name": "Demo"})()
            alpha_key = ""
            finnhub_key = ""

            @staticmethod
            def get_yahoo_data(*a, **k):
                return {"current_price": 1.0825, "source": "Yahoo Finance"}

            @staticmethod
            def get_instrument_source_status():
                return [
                    {"symbol": "EURUSD=X", "display": "EUR/USD", "plan": "OANDA (Demo)", "actual": "Yahoo Finance"},
                ]

        out = msgs.get_data_sources_status(FakeMarket(), self._fake_macro(), None)
        # Plan OANDA tapi aktual Yahoo → ikon peringatan, bukan hijau
        self.assertIn("⚠️", out)
        self.assertIn("aktual: Yahoo Finance", out)

    def test_legacy_market_without_method_does_not_crash(self):
        class FakeMarket:
            oanda = type("O", (), {"is_configured": False, "env_name": "practice"})()
            alpha_key = ""
            finnhub_key = ""

            @staticmethod
            def get_yahoo_data(*a, **k):
                return {"current_price": 1.0825, "source": "yahoo"}

        out = msgs.get_data_sources_status(FakeMarket(), self._fake_macro(), None)
        self.assertIn("Yahoo", out)  # blok lama tetap jalan
        # Blok baru dilewati tanpa crash (getattr defensif)


if __name__ == "__main__":
    unittest.main()
