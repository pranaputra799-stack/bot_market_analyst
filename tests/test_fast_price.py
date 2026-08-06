"""Unit tests untuk fast price path — deteksi pertanyaan harga sederhana
yang dijawab instan tanpa AI (tanpa network)."""

import unittest

from bot.handlers import detect_fast_price_query


class TestDetectFastPriceQuery(unittest.TestCase):
    """Tes deteksi pertanyaan harga sederhana → (symbol, display_name)."""

    def test_berapa_harga_eurusd(self):
        self.assertEqual(
            detect_fast_price_query("berapa harga eurusd?"),
            ("EURUSD=X", "EUR/USD"),
        )

    def test_harga_gold_sekarang(self):
        self.assertEqual(
            detect_fast_price_query("harga gold sekarang"),
            ("GC=F", "XAU/USD (Gold)"),
        )

    def test_rate_usd_jpy(self):
        self.assertEqual(
            detect_fast_price_query("rate usd/jpy berapa"),
            ("USDJPY=X", "USD/JPY"),
        )

    def test_harga_bitcoin(self):
        self.assertEqual(
            detect_fast_price_query("harga bitcoin sekarang?"),
            ("BTC-USD", "Bitcoin (BTC/USD)"),
        )

    def test_kurs_usdidr(self):
        # "dolar" dipetakan ke USD/IDR di keyword map chart
        self.assertEqual(
            detect_fast_price_query("kurs dolar berapa?"),
            ("USDIDR=X", "USD/IDR"),
        )

    def test_kurs_usdidr_slash(self):
        self.assertEqual(
            detect_fast_price_query("kurs usd/idr berapa?"),
            ("USDIDR=X", "USD/IDR"),
        )

    def test_analisis_dilewati(self):
        # Pertanyaan yang butuh analisis TIDAK boleh masuk fast path
        self.assertIsNone(detect_fast_price_query("analisis teknikal eurusd"))

    def test_kenapa_dilewati(self):
        # "kenapa" = butuh penjelasan, bukan cek harga instan
        self.assertIsNone(detect_fast_price_query("kenapa gold naik hari ini?"))

    def test_prediksi_dilewati(self):
        self.assertIsNone(detect_fast_price_query("berapa prediksi gold besok?"))

    def test_perbandingan_dilewati(self):
        self.assertIsNone(detect_fast_price_query("harga eurusd vs gbpusd?"))

    def test_tanpa_keyword_harga(self):
        self.assertIsNone(detect_fast_price_query("bagaimana cara baca chart?"))

    def test_tanpa_symbol(self):
        self.assertIsNone(detect_fast_price_query("berapa harga pasar?"))

    def test_teks_kosong(self):
        self.assertIsNone(detect_fast_price_query(""))
        self.assertIsNone(detect_fast_price_query(None))

    def test_chart_command_dilewati(self):
        # /chart ditangani command handler, bukan fast path pesan
        self.assertIsNone(detect_fast_price_query("chart eurusd"))


if __name__ == "__main__":
    unittest.main()
