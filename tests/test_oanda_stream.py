"""
Unit tests untuk OANDA price stream (tanpa network - parse & store saja).

Mencakup:
- parse_price_message: pesan PRICE -> harga bersih, pesan lain -> None
- OandaPriceStream: penyimpanan harga live + get_price / is_configured
"""
import unittest

from data.oanda_stream import OandaPriceStream, parse_price_message


def _price_msg(instrument="EUR_USD", bid="1.0824", ask="1.0826"):
    return {
        "type": "PRICE",
        "instrument": instrument,
        "time": "2026-08-07T00:00:00.000000000Z",
        "bids": [{"price": bid, "liquidity": 1000000}],
        "asks": [{"price": ask, "liquidity": 1000000}],
    }


class TestParsePriceMessage(unittest.TestCase):

    def test_parses_price_message(self):
        parsed = parse_price_message(_price_msg())
        self.assertEqual(parsed["instrument"], "EUR_USD")
        self.assertEqual(parsed["bid"], 1.0824)
        self.assertEqual(parsed["ask"], 1.0826)
        self.assertEqual(parsed["mid"], 1.0825)
        self.assertTrue(parsed["time"])

    def test_parses_gold(self):
        parsed = parse_price_message(_price_msg(instrument="XAU_USD", bid="2350.10", ask="2350.40"))
        self.assertEqual(parsed["instrument"], "XAU_USD")
        self.assertAlmostEqual(parsed["mid"], 2350.25)

    def test_heartbeat_returns_none(self):
        self.assertIsNone(parse_price_message({"type": "HEARTBEAT", "time": "..."}))

    def test_invalid_messages_return_none(self):
        self.assertIsNone(parse_price_message(None))
        self.assertIsNone(parse_price_message({}))
        self.assertIsNone(parse_price_message({"type": "PRICE", "instrument": "EUR_USD"}))
        self.assertIsNone(parse_price_message({"type": "PRICE", "instrument": "EUR_USD",
                                               "bids": [{"price": "x"}], "asks": []}))


class TestOandaPriceStream(unittest.TestCase):

    def test_not_configured_without_key(self):
        s = OandaPriceStream(api_key="", account_id="", env="practice")
        self.assertFalse(s.is_configured)

    def test_configured_with_key(self):
        s = OandaPriceStream(api_key="abc", account_id="123", env="practice")
        self.assertTrue(s.is_configured)
        self.assertFalse(s.is_running)  # belum start

    def test_get_price_returns_copy(self):
        s = OandaPriceStream(api_key="abc")
        with s._lock:
            s._prices["EUR_USD"] = {"instrument": "EUR_USD", "mid": 1.0825}
        p = s.get_price("EUR_USD")
        self.assertEqual(p["mid"], 1.0825)
        # Mutasi hasil tidak boleh mengubah store internal
        p["mid"] = 999.0
        self.assertEqual(s.get_price("EUR_USD")["mid"], 1.0825)

    def test_get_price_unknown_instrument(self):
        s = OandaPriceStream(api_key="abc")
        self.assertIsNone(s.get_price("USD_IDR"))

    def test_live_instruments_from_config(self):
        s = OandaPriceStream(api_key="abc")
        insts = s.live_instruments
        self.assertIn("EUR_USD", insts)
        self.assertIn("XAU_USD", insts)
        self.assertNotIn("USD_IDR", insts)

    def test_full_list_used_by_default(self):
        s = OandaPriceStream(api_key="abc")
        self.assertFalse(s._use_core_only)
        self.assertIn("SPX500_USD", s.live_instruments)
        self.assertIn("BTC_USD", s.live_instruments)

    def test_core_only_after_server_rejects_full_list(self):
        s = OandaPriceStream(api_key="abc")
        s._use_core_only = True
        insts = s.live_instruments
        # Inti: forex + logam — tanpa index/oil/crypto
        self.assertIn("EUR_USD", insts)
        self.assertIn("XAU_USD", insts)
        self.assertNotIn("SPX500_USD", insts)
        self.assertNotIn("BTC_USD", insts)


if __name__ == "__main__":
    unittest.main()
