"""Unit tests untuk cache AnalysisDirector dua lapis (L1 memori + L2 Supabase).

TANPA network — persistent di-mock dengan FakePersistent (kontrak sama dengan
data.cache.SupabaseCache). Verifikasi:
- _cache_result menulis ke L1 (memori) DAN L2 (app_cache) saat enabled
- _check_cache membaca dari L2 saat L1 kosong — simulasi bot restart/deploy,
  analisis follow-up yang sama tetap dikembalikan
- bila L2 disabled / tidak ada data, perilaku fallback normal (None)
"""

import unittest
from unittest import mock

import analysis.director as director_mod
from analysis.director import AnalysisDirector, AnalysisResult
from data.cache import MemoryCache, safe_hash


class FakePersistent:
    """Stub SupabaseCache (L2) — kontrak sama, tanpa network."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.storage = {}
        self.set_calls = []

    def get(self, key):
        return self.storage.get(key) if self.enabled else None

    def set(self, key, value, ttl):
        if not self.enabled:
            return
        self.set_calls.append((key, value, ttl))
        self.storage[key] = value

    def delete(self, key):
        self.storage.pop(key, None)


def _result(**kw):
    defaults = dict(
        question="harga gold sekarang?",
        intent="price",
        final_response="Gold 2350 — bias bullish.",
        agents_executed=["research", "signals"],
        conversation_history="User: gold\nBot: 2350",
    )
    defaults.update(kw)
    return AnalysisResult(**defaults)


class TestDirectorCacheTwoLayers(unittest.TestCase):
    def _director(self):
        return AnalysisDirector.__new__(AnalysisDirector)

    def _expected_key(self, question, history):
        return f"analysis:{safe_hash(question + history[:200])}"

    def test_cache_result_writes_both_layers(self):
        director = self._director()
        result = _result()
        l1 = MemoryCache()
        l2 = FakePersistent(enabled=True)
        with mock.patch.object(director_mod, "cache", l1), \
             mock.patch.object(director_mod, "persistent", l2):
            director._cache_result(result.question, result)

        key = self._expected_key(result.question, result.conversation_history)
        self.assertIsNotNone(l1.get(key), "L1 (memori) harus terisi")
        self.assertIn(key, l2.storage, "L2 (app_cache) harus terisi")
        self.assertEqual(l2.storage[key]["final_response"], "Gold 2350 — bias bullish.")
        self.assertEqual(len(l2.set_calls), 1)

    def test_check_cache_reads_l2_after_restart(self):
        """Simulasi restart: L1 kosong, L2 masih punya analisis lama."""
        director = self._director()
        result = _result()
        key = self._expected_key(result.question, result.conversation_history)

        l1 = MemoryCache()  # kosong = proses baru
        l2 = FakePersistent(enabled=True)
        l2.storage[key] = {
            "intent": result.intent,
            "final_response": result.final_response,
            "agents_executed": result.agents_executed,
            "confidence": None,
        }
        with mock.patch.object(director_mod, "cache", l1), \
             mock.patch.object(director_mod, "persistent", l2):
            got = director._check_cache(result.question, result.conversation_history)

        self.assertIsNotNone(got)
        self.assertEqual(got.final_response, result.final_response)
        self.assertEqual(got.intent, "price")
        # Setelah hit L2, L1 diisi ulang agar akses berikutnya cepat
        self.assertIsNotNone(l1.get(key))

    def test_check_cache_none_when_empty(self):
        director = self._director()
        l1 = MemoryCache()
        l2 = FakePersistent(enabled=True)  # kosong
        with mock.patch.object(director_mod, "cache", l1), \
             mock.patch.object(director_mod, "persistent", l2):
            got = director._check_cache("ada pertanyaan", "riwayat")
        self.assertIsNone(got)

    def test_l2_disabled_no_persistent_write(self):
        """L2 mati (Supabase tidak dikonfigurasi) → hanya L1, tidak crash."""
        director = self._director()
        result = _result()
        l1 = MemoryCache()
        l2 = FakePersistent(enabled=False)
        with mock.patch.object(director_mod, "cache", l1), \
             mock.patch.object(director_mod, "persistent", l2):
            director._cache_result(result.question, result)
        self.assertEqual(l2.set_calls, [])
        self.assertIsNotNone(l1.get(self._expected_key(
            result.question, result.conversation_history)))

    def test_cache_key_differs_by_history(self):
        """Riwayat berbeda → key berbeda (follow-up tidak memakai cache jawaban lain)."""
        q = "support-nya di mana?"
        k1 = self._expected_key(q, "gold")
        k2 = self._expected_key(q, "eurusd")
        self.assertNotEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
