"""Unit tests untuk utilitas di bot/handlers.py (split_long_text, strip asterisks)."""

import unittest

from bot.handlers import split_long_text, strip_markdown_asterisks, TG_MAX_MESSAGE_CHARS


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
