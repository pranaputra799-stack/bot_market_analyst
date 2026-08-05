"""Unit tests untuk SentimentAnalyzer (logika murni, tanpa network)."""

import math
import time
import unittest

from analysis.sentiment import SentimentAnalyzer


class TestSafeFloat(unittest.TestCase):
    def test_none_returns_default(self):
        self.assertEqual(SentimentAnalyzer._safe_float(None), 0.0)

    def test_invalid_string_returns_default(self):
        self.assertEqual(SentimentAnalyzer._safe_float("abc"), 0.0)

    def test_nan_returns_default(self):
        self.assertEqual(SentimentAnalyzer._safe_float(float("nan")), 0.0)

    def test_infinity_returns_default(self):
        self.assertEqual(SentimentAnalyzer._safe_float(float("inf")), 0.0)

    def test_valid_number(self):
        self.assertEqual(SentimentAnalyzer._safe_float("3.5"), 3.5)
        self.assertEqual(SentimentAnalyzer._safe_float(2), 2.0)

    def test_custom_default(self):
        self.assertEqual(SentimentAnalyzer._safe_float(None, default=0.5), 0.5)


class TestLexiconScore(unittest.TestCase):
    def test_bullish_words_positive(self):
        self.assertGreater(SentimentAnalyzer._lexicon_score("Gold naik dan menguat hari ini"), 0)

    def test_bearish_words_negative(self):
        self.assertLess(SentimentAnalyzer._lexicon_score("Dolar turun dan melemah"), 0)

    def test_neutral_text_zero(self):
        self.assertEqual(SentimentAnalyzer._lexicon_score("Tidak ada berita signifikan"), 0.0)

    def test_equal_bull_and_bear_neutral(self):
        self.assertEqual(SentimentAnalyzer._lexicon_score("naik turun"), 0.0)


class TestCombineScores(unittest.TestCase):
    def test_finnhub_zero_uses_lexicon(self):
        self.assertEqual(SentimentAnalyzer._combine_scores(0.0, 0.8), 0.8)

    def test_blend_clamped_upper(self):
        self.assertLessEqual(SentimentAnalyzer._combine_scores(0.9, 0.9), 1.0)

    def test_blend_clamped_lower(self):
        self.assertGreaterEqual(SentimentAnalyzer._combine_scores(-0.9, -0.9), -1.0)


class TestRecencyWeight(unittest.TestCase):
    def test_recent_article_high_weight(self):
        art = {"datetime_ts": int(time.time())}
        self.assertAlmostEqual(SentimentAnalyzer._recency_weight(art, 0), 1.0, places=1)

    def test_old_article_floor_weight(self):
        # 100 hari lalu → bobot harus ter-floor di 0.3
        art = {"datetime_ts": int(time.time()) - 100 * 86400}
        self.assertEqual(SentimentAnalyzer._recency_weight(art, 0), 0.3)

    def test_no_timestamp_index_based(self):
        self.assertEqual(SentimentAnalyzer._recency_weight({}, 0), 1.0)
        self.assertAlmostEqual(SentimentAnalyzer._recency_weight({}, 5), 0.7)

    def test_invalid_timestamp_fallback(self):
        art = {"datetime_ts": "not-a-number"}
        w = SentimentAnalyzer._recency_weight(art, 3)
        self.assertGreaterEqual(w, 0.3)
        self.assertLessEqual(w, 1.0)

    def test_all_weights_in_range(self):
        for i in range(0, 20):
            w = SentimentAnalyzer._recency_weight({}, i)
            self.assertGreaterEqual(w, 0.3)
            self.assertLessEqual(w, 1.0)


class TestScoreToLabel(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(SentimentAnalyzer._score_to_label(0.6), "sangat_bullish")
        self.assertEqual(SentimentAnalyzer._score_to_label(0.2), "bullish")
        self.assertEqual(SentimentAnalyzer._score_to_label(0.0), "netral")
        self.assertEqual(SentimentAnalyzer._score_to_label(-0.3), "bearish")
        self.assertEqual(SentimentAnalyzer._score_to_label(-0.7), "sangat_bearish")


class TestFormatReport(unittest.TestCase):
    # format_report/format_short adalah instance method — panggil via instance
    analyzer = SentimentAnalyzer()

    def test_error_dict_short_circuits(self):
        result = {"error": "no_news", "message": "Berita tidak tersedia", "symbol": "FOREX"}
        text = self.analyzer.format_report(result, "Pasar Forex")
        self.assertIn("Sentimen Pasar", text)
        self.assertIn("tidak tersedia", text.lower())

    def test_normal_report_contains_sections(self):
        result = {
            "symbol": "FOREX",
            "score": 0.5,
            "label": "bullish",
            "confidence": 0.7,
            "count": 3,
            "articles": [
                {"score": 0.5, "headline": "H1", "source": "S1"},
            ],
            "bull_drivers": ["Driver 1"],
            "bear_drivers": [],
            "assessment": "Ringkasan",
            "method": "finnhub+lexicon+llm",
        }
        text = self.analyzer.format_report(result, "Pasar Forex")
        self.assertIn("SENTIMEN PASAR", text)
        self.assertIn("Driver Bullish", text)
        self.assertIn("Ringkasan", text)

    def test_format_short_error(self):
        self.assertEqual(
            self.analyzer.format_short({"error": "x"}, "FOREX"),
            "Sentimen pasar tidak tersedia.",
        )


class TestAsList(unittest.TestCase):
    def test_list_of_mixed(self):
        self.assertEqual(SentimentAnalyzer._as_list([1, "a", None, "", "b"]), ["1", "a", "b"])

    def test_string(self):
        self.assertEqual(SentimentAnalyzer._as_list("satu"), ["satu"])
        self.assertEqual(SentimentAnalyzer._as_list("  "), [])

    def test_none(self):
        self.assertEqual(SentimentAnalyzer._as_list(None), [])


if __name__ == "__main__":
    unittest.main()
