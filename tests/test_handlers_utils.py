"""Unit tests untuk utilitas di bot/handlers.py (split_long_text, strip asterisks)."""

import unittest

from bot.handlers import (
    split_long_text,
    strip_markdown_asterisks,
    label_to_symbol,
    TG_MAX_MESSAGE_CHARS,
)


class TestSplitLongText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_long_text("Halo dunia"), ["Halo dunia"])

    def test_long_paragraph_split(self):
        text = "paragraf " + "x" * 5000
        chunks = split_long_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), TG_MAX_MESSAGE_CHARS)
        # Konten harus tetap utuh setelah digabung kembali
        self.assertEqual("".join(chunks), text)

    def test_empty_and_whitespace(self):
        self.assertEqual(split_long_text(""), [""])
        self.assertEqual([c for c in split_long_text("") if c], [])

    def test_multiple_paragraphs_preserved(self):
        text = "\n\n".join(["A" * 3000, "B" * 3000, "C" * 100])
        chunks = split_long_text(text)
        joined = "".join(chunks)
        self.assertIn("AAA", joined)
        self.assertIn("BBB", joined)
        self.assertIn("CCC", joined)
        for c in chunks:
            self.assertLessEqual(len(c), TG_MAX_MESSAGE_CHARS)


class TestSanitizeText(unittest.TestCase):
    """sanitize_text harus mengganti kontrol char dengan spasi, bukan menghapus
    (agar kata tidak menempel: 'gold\\nanalysis' → 'gold analysis')."""

    def test_newlines_replaced_not_concatenated(self):
        from utils.validators import sanitize_text

        self.assertEqual(
            sanitize_text("kenapa gold naik\napa penyebabnya?"),
            "kenapa gold naik apa penyebabnya?",
        )

    def test_tabs_replaced(self):
        from utils.validators import sanitize_text

        self.assertEqual(sanitize_text("harga\tgold"), "harga gold")

    def test_truncation(self):
        from utils.validators import sanitize_text

        out = sanitize_text("x" * 600)
        self.assertLessEqual(len(out), 500)


class TestLabelToSymbol(unittest.TestCase):
    """Label fokus aset (dari conversation memory) → simbol Yahoo Finance."""

    def test_pair_labels(self):
        self.assertEqual(label_to_symbol("EUR/USD"), "EURUSD=X")
        self.assertEqual(label_to_symbol("USD/JPY"), "USDJPY=X")
        self.assertEqual(label_to_symbol("USD/IDR"), "USDIDR=X")
        self.assertEqual(label_to_symbol("gbp/usd"), "GBPUSD=X")  # case-insensitive

    def test_special_labels(self):
        self.assertEqual(label_to_symbol("XAU/USD (Gold)"), "GC=F")
        self.assertEqual(label_to_symbol("XAG/USD (Silver)"), "SI=F")
        self.assertEqual(label_to_symbol("BTC/USD (Bitcoin)"), "BTC-USD")
        self.assertEqual(label_to_symbol("ETH/USD (Ethereum)"), "ETH-USD")
        self.assertEqual(label_to_symbol("DXY (Dollar Index)"), "DX-Y.NYB")
        self.assertEqual(label_to_symbol("S&P 500"), "^GSPC")
        self.assertEqual(label_to_symbol("NASDAQ"), "^IXIC")
        self.assertEqual(label_to_symbol("VIX"), "^VIX")

    def test_invalid_labels(self):
        self.assertIsNone(label_to_symbol(None))
        self.assertIsNone(label_to_symbol(""))
        self.assertIsNone(label_to_symbol("saham"))


class TestStripMarkdownAsterisks(unittest.TestCase):
    def test_double_asterisk_bold(self):
        self.assertEqual(strip_markdown_asterisks("**bold** teks"), "bold teks")

    def test_single_asterisk_italic(self):
        self.assertEqual(strip_markdown_asterisks("*miring*"), "miring")

    def test_numeric_multiplication_preserved(self):
        self.assertEqual(strip_markdown_asterisks("5*3=15"), "5*3=15")

    def test_none_input(self):
        self.assertIsNone(strip_markdown_asterisks(None))

    def test_empty_input(self):
        self.assertEqual(strip_markdown_asterisks(""), "")

    def test_mixed_content(self):
        out = strip_markdown_asterisks("**Judul** harga *naik* 2*3")
        self.assertNotIn("*", out.replace("2*3", ""))
        self.assertIn("2*3", out)


if __name__ == "__main__":
    unittest.main()
