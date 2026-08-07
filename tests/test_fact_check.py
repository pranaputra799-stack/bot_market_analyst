"""Unit tests untuk analysis/fact_check.py — verifikasi deterministik angka jawaban AI."""

import unittest

from analysis.fact_check import (
    build_fact_check_note,
    extract_number_tokens,
    find_suspicious,
    strip_fact_check_note,
    _candidate_values,
)

# Data contoh: indikator hasil perhitungan lokal + data pasar (ground truth).
IND = (
    "• RSI(14): 58.3 (netral)\n"
    "• MACD: 0.00123 (positif)\n"
    "• Pivot: 1.0835 | R1 1.0870 S1 1.0800\n"
    "• Fib resistance: 1.0900, 1.0950"
)
MARKET = "📊 EUR/USD 1.0850 (+0.12%) | Gold 2.350 (-0.3%) | DXY 104.2 (+0.1%)"


class TestTokenParsing(unittest.TestCase):
    def test_simple_float(self):
        self.assertIn(1.085, _candidate_values("1.0850"))

    def test_integer(self):
        self.assertIn(2350, _candidate_values("2350"))

    def test_comma_thousands(self):
        self.assertIn(2350, _candidate_values("2,350"))

    def test_comma_decimal(self):
        self.assertIn(58.3, _candidate_values("58,3"))

    def test_both_separators_us_style(self):
        self.assertIn(1085.5, _candidate_values("1,085.50"))

    def test_both_separators_id_style(self):
        # "2.350,50" (titik ribuan, koma desimal gaya Indonesia) → 2350.5
        self.assertIn(2350.5, _candidate_values("2.350,50"))

    def test_leading_zero_is_decimal_only(self):
        self.assertEqual(_candidate_values("0.00123"), [0.00123])


class TestExtraction(unittest.TestCase):
    def test_excludes_percent(self):
        tokens = [t for t, _ in extract_number_tokens("probabilitas 40%, target 1.1000")]
        self.assertNotIn("40", tokens)
        self.assertIn("1.1000", tokens)

    def test_excludes_year_and_time(self):
        tokens = [t for t, _ in extract_number_tokens("rilis 13 Agu 2026 pukul 19:30 WIB")]
        self.assertEqual(tokens, [])

    def test_excludes_small_counts(self):
        tokens = [t for t, _ in extract_number_tokens("3 skenario, 2 jam ke depan")]
        self.assertEqual(tokens, [])

    def test_keeps_price_like(self):
        tokens = [t for t, _ in extract_number_tokens(
            "support 1.0800, resistance 1.0950, gold 2350.5"
        )]
        self.assertEqual(set(tokens), {"1.0800", "1.0950", "2350.5"})


class TestMatching(unittest.TestCase):
    def test_match_within_tolerance(self):
        self.assertEqual(find_suspicious("harga sekarang 1.0855", [MARKET]), [])

    def test_flag_mismatched(self):
        susp = find_suspicious("RSI 55 dan harga 1.1200", [IND, MARKET])
        self.assertIn("55", susp)      # RSI data 58.3 — selisih 5.7%
        self.assertIn("1.1200", susp)  # bukan level di data

    def test_flag_50_pips_off(self):
        # Error 50 pip (1.0900 vs data 1.0850 = 0.46%) harus terdeteksi
        susp = find_suspicious("harga sekarang 1.0900", [MARKET])
        self.assertIn("1.0900", susp)

    def test_gold_price_four_digit_not_year(self):
        # Harga emas 1990 tidak boleh dianggap tahun (tetap diperiksa)
        susp = find_suspicious("gold 1990", [MARKET])
        self.assertIn("1990", susp)

    def test_no_ground_truth_returns_empty(self):
        self.assertEqual(find_suspicious("RSI 58.3", []), [])

    def test_question_numbers_not_flagged(self):
        # Angka yang muncul di pertanyaan/riwayat user bukan halusinasi
        susp = find_suspicious(
            "kalau 1.0700 ditembus, target 1.1200",
            [IND, "User: support di 1.0700 berapa?"],
        )
        self.assertNotIn("1.0700", susp)
        self.assertIn("1.1200", susp)


class TestBuildNote(unittest.TestCase):
    def test_note_when_suspicious(self):
        note = build_fact_check_note("Bias bullish, target 1.1200, RSI 58.3", [IND])
        self.assertIn("1.1200", note)
        self.assertNotIn("58.3", note)  # cocok dengan data → tidak dilaporkan

    def test_no_note_when_clean(self):
        note = build_fact_check_note("RSI 58.3, support 1.0800, resistance 1.0950", [IND])
        self.assertEqual(note, "")

    def test_no_note_when_no_data(self):
        self.assertEqual(build_fact_check_note("apa pun 123", []), "")

    def test_no_note_when_no_answer(self):
        self.assertEqual(build_fact_check_note("", [IND]), "")

    def test_note_plain_text_no_markdown(self):
        note = build_fact_check_note("target 1.1200", [IND])
        self.assertNotIn("*", note)

    def test_strip_removes_note(self):
        answer = "Bias bullish.\n\n🔎 Verifikasi Data: angka 1.1200 tidak ditemukan. Mohon cek."
        self.assertEqual(strip_fact_check_note(answer), "Bias bullish.")

    def test_strip_no_note_unchanged(self):
        self.assertEqual(strip_fact_check_note("Bias bullish."), "Bias bullish.")
        self.assertEqual(strip_fact_check_note(""), "")


if __name__ == "__main__":
    unittest.main()
