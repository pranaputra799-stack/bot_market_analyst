"""
Unit tests untuk TTL cache get_data_sources_status (performa /status):
Panggilan kedua dalam jendela TTL tidak boleh memanggil data provider lagi
(Yahoo/OANDA) — /status berulang harus instan.
"""
import time
import unittest

import bot.messages as msgs


def _fakes():
    """Fake market/macro dengan counter panggilan get_yahoo_data."""
    calls = {"n": 0}

    class FakeMarket:
        oanda = type("O", (), {"is_configured": False, "env_name": "practice"})()
        alpha_key = ""
        finnhub_key = ""

        @staticmethod
        def get_yahoo_data(*a, **k):
            calls["n"] += 1
            return {"current_price": 1.0850, "source": "yahoo"}

    class FakeMacro:
        fred_key = ""

    return FakeMarket(), FakeMacro(), calls


class TestDataSourcesStatusCache(unittest.TestCase):

    def setUp(self):
        # reset cache agar tiap test deterministik
        msgs._DATA_STATUS_CACHE["ts"] = 0.0
        msgs._DATA_STATUS_CACHE["value"] = ""

    def test_second_call_within_ttl_skips_network(self):
        market, macro, calls = _fakes()
        out1 = msgs.get_data_sources_status(market, macro, None)
        first = calls["n"]
        self.assertGreaterEqual(first, 1)  # panggilan pertama benar-benar cek
        self.assertIn("Yahoo", out1)

        out2 = msgs.get_data_sources_status(market, macro, None)
        self.assertEqual(calls["n"], first)  # cache → tidak ada network baru
        self.assertEqual(out1, out2)

    def test_cache_expires_after_ttl(self):
        market, macro, calls = _fakes()
        msgs.get_data_sources_status(market, macro, None)
        first = calls["n"]

        msgs._DATA_STATUS_CACHE["ts"] = time.time() - msgs._DATA_STATUS_TTL - 1
        msgs.get_data_sources_status(market, macro, None)
        self.assertGreater(calls["n"], first)  # TTL lewat → cek ulang

    def test_output_stable_across_calls(self):
        market, macro, calls = _fakes()
        out1 = msgs.get_data_sources_status(market, macro, None)
        out2 = msgs.get_data_sources_status(market, macro, None)
        self.assertEqual(out1, out2)


if __name__ == "__main__":
    unittest.main()
