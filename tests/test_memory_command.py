"""Unit tests untuk command /memory — lihat & hapus riwayat percakapan.

TANPA network — conversation_memory memakai cache L1 lokal (L2 no-op tanpa
konfigurasi Supabase di lingkungan test).
"""

import asyncio
import unittest

from bot.handlers import MarketBot, MEMORY_USAGE
from data import conversation_memory as cm
from tests.test_handlers_utils import _FakeUpdate, _FakeContext


class TestMemoryCommand(unittest.TestCase):
    USER = 9999

    def setUp(self):
        cm.clear(self.USER)

    def tearDown(self):
        cm.clear(self.USER)

    def _run(self, text, user_id=None):
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate(text, user_id=user_id or self.USER)
        asyncio.run(bot.memory_command(upd, _FakeContext()))
        return upd

    def test_empty_shows_usage(self):
        upd = self._run("/memory")
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("RIWAYAT PERCAKAPAN", joined.upper())
        self.assertIn("/memory clear", joined)

    def test_shows_history_and_context(self):
        cm.add_exchange(self.USER, "harga gold sekarang?", "Gold 2350, bias bullish.")
        upd = self._run("/memory")
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("harga gold sekarang?", joined)
        self.assertIn("Gold 2350", joined)
        # Konteks terstruktur: aset terdeteksi dari pertanyaan
        self.assertIn("XAU/USD (Gold)", joined)

    def test_shows_newest_first(self):
        cm.add_exchange(self.USER, "pertanyaan satu", "jawaban satu")
        cm.add_exchange(self.USER, "pertanyaan dua", "jawaban dua")
        upd = self._run("/memory")
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertLess(joined.index("pertanyaan dua"), joined.index("pertanyaan satu"))

    def test_clear_removes_history(self):
        cm.add_exchange(self.USER, "pertanyaan", "jawaban")
        upd = self._run("/memory clear")
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("dihapus", joined.lower())
        self.assertEqual(cm.get_history(self.USER), [])
        # Setelah clear, /memory kembali menampilkan usage (kosong)
        upd2 = self._run("/memory")
        joined2 = "\n".join(t for t, _ in upd2.message.replies)
        self.assertIn("RIWAYAT PERCAKAPAN", joined2.upper())

    def test_markdown_in_answer_is_sanitized(self):
        """Jawaban AI berisi **bold**_md yang tidak seimbang → tetap tampil aman."""
        cm.add_exchange(self.USER, "analisis eurusd", "Harga **1.0850** _kemungkinan_ *naik* 5*3=15")
        upd = self._run("/memory")
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("1.0850", joined)
        self.assertIn("5*3=15", joined)  # perkalian angka dipertahankan
        self.assertNotIn("**1.0850**", joined)  # asterisks markdown sudah distrip

    def test_case_insensitive_clear(self):
        cm.add_exchange(self.USER, "pertanyaan", "jawaban")
        upd = self._run("/MEMORY CLEAR")
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("dihapus", joined.lower())
        self.assertEqual(cm.get_history(self.USER), [])

    def test_usage_constant(self):
        self.assertIn("/memory", MEMORY_USAGE)
        self.assertIn("24 jam", MEMORY_USAGE)


if __name__ == "__main__":
    unittest.main()
