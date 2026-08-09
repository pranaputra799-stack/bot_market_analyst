"""Unit tests untuk penghematan token (fitur baru).

Cover:
- utils/token_budget: estimate_tokens & truncate_to_budget
- conversation_memory.format_history: budget token (buang pertukaran terlama)
- engine: pelacakan pemakaian token dari response API (usage / usageMetadata)
- engine: single-flight / request coalescing (request identik tidak dobel)
"""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import ai.engine as engine_mod
from ai.engine import AIFallbackEngine
from config.providers import PROVIDER_CONFIGS
from data import conversation_memory as cm
from data.cache import set_cached_ai_response
from config.settings import MEMORY_MAX_TOKENS_IN_CONTEXT
from utils.token_budget import estimate_tokens, truncate_to_budget


def _make_engine():
    """Engine dengan key groq palsu & _call_provider di-stub (tanpa network)."""
    eng = AIFallbackEngine()
    eng.api_keys["groq"] = "test-key"
    eng.throttle_min_interval_override = 0.0  # matikan throttle agar test cepat
    return eng


class TestTokenBudget(unittest.TestCase):
    def test_estimate_tokens_min(self):
        self.assertGreaterEqual(estimate_tokens(""), 0)
        self.assertGreaterEqual(estimate_tokens("x"), 1)

    def test_truncate_keeps_short_text(self):
        t = truncate_to_budget("teks pendek", 1000)
        self.assertEqual(t, "teks pendek")

    def test_truncate_cuts_long_text_within_budget(self):
        long = "kalimat contoh " * 200  # ~3200 char → melebihi budget 100 token
        t = truncate_to_budget(long, 100, "context")
        self.assertIn("truncated", t)
        self.assertLessEqual(estimate_tokens(t), 100)

    def test_truncate_empty_and_zero(self):
        self.assertEqual(truncate_to_budget("", 100), "")
        self.assertEqual(truncate_to_budget("teks", 0), "")


class TestFormatHistoryTokenBudget(unittest.TestCase):
    def setUp(self):
        cm.clear(3000)

    def tearDown(self):
        cm.clear(3000)

    def test_history_drops_oldest_when_over_budget(self):
        # Jawaban maksimal (500 char) x 6 pertukaran → jauh melebihi budget 600
        # token → pertukaran PALING LAMA harus dibuang, yang TERBARU dipertahankan.
        for i in range(6):
            cm.add_exchange(3000, f"q{i}", "x" * 500)
        text = cm.format_history(3000)
        self.assertIn('User: "q5"', text, "pertukaran terbaru harus tetap ada")
        self.assertLessEqual(estimate_tokens(text), MEMORY_MAX_TOKENS_IN_CONTEXT)

    def test_history_short_stays_intact(self):
        cm.add_exchange(3000, "q1", "a1")
        cm.add_exchange(3000, "q2", "a2")
        text = cm.format_history(3000)
        self.assertIn('User: "q1"', text)
        self.assertIn('User: "q2"', text)
        self.assertIn("Bot: a2", text)

    def test_history_empty_returns_empty(self):
        self.assertEqual(cm.format_history(3000), "")


class TestEngineUsageTracking(unittest.TestCase):
    def test_usage_recorded_from_openai_response(self):
        eng = _make_engine()

        class FakeResp200:
            status_code = 200
            headers = {}

            def json(self):
                return {
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
                }

        original = engine_mod.requests.post
        try:
            engine_mod.requests.post = lambda *a, **k: FakeResp200()
            result = eng._call_openai_compatible(
                "groq", PROVIDER_CONFIGS["groq"], "test-key", "p", "s", 1024
            )
        finally:
            engine_mod.requests.post = original

        self.assertEqual(result, "OK")
        u = eng.stats["usage"]
        self.assertEqual(u["prompt_tokens"], 100)
        self.assertEqual(u["completion_tokens"], 25)
        self.assertEqual(u["total_tokens"], 125)
        self.assertEqual(u["by_provider"]["groq"]["total_tokens"], 125)

    def test_usage_recorded_from_gemini_metadata(self):
        eng = _make_engine()
        eng.api_keys["gemini"] = "test-key"

        class FakeResp200:
            status_code = 200
            headers = {}

            def json(self):
                return {
                    "candidates": [{"content": {"parts": [{"text": "halo"}]}}],
                    "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10, "totalTokenCount": 60},
                }

        original = engine_mod.requests.post
        try:
            engine_mod.requests.post = lambda *a, **k: FakeResp200()
            result = eng._call_gemini(
                "gemini", PROVIDER_CONFIGS["gemini"], "test-key", "p", "s", 1024
            )
        finally:
            engine_mod.requests.post = original

        self.assertEqual(result, "halo")
        u = eng.stats["usage"]["by_provider"]["gemini"]
        self.assertEqual(u["prompt_tokens"], 50)
        self.assertEqual(u["completion_tokens"], 10)
        self.assertEqual(u["total_tokens"], 60)

    def test_usage_tolerant_to_missing_or_invalid(self):
        eng = _make_engine()
        eng._record_usage("groq", None)
        eng._record_usage("groq", {})
        eng._record_usage("groq", {"prompt_tokens": "abc"})
        self.assertEqual(eng.stats["usage"]["total_tokens"], 0)


class TestEngineSingleFlight(unittest.TestCase):
    def test_waiter_gets_cached_result_after_release(self):
        eng = _make_engine()
        key = "coalesce-mechanism-key"
        registered, _ = eng._wait_or_register_inflight(key)
        self.assertTrue(registered, "generator pertama harus terdaftar")

        captured = []
        about_to_wait = threading.Event()

        def waiter():
            about_to_wait.set()  # tandai SEBELUM masuk event.wait (deterministik)
            r, c = eng._wait_or_register_inflight(key, timeout=5)
            captured.append((r, c))

        t = threading.Thread(target=waiter)
        t.start()
        # Deterministik: tunggu waiter menandai siap, bukan sleep acak.
        self.assertTrue(about_to_wait.wait(timeout=5), "waiter harus mulai menunggu")
        time.sleep(0.05)  # celah mikro: flag → event.wait()
        set_cached_ai_response(key, "JAWABAN-CACHE")
        eng._release_inflight(key)
        t.join(timeout=5)

        self.assertFalse(t.is_alive(), "waiter harus selesai setelah release")
        self.assertEqual(captured, [(False, "JAWABAN-CACHE")])
        # Registrasi harus sudah bersih setelah release
        self.assertNotIn(key, eng._inflight)

    def test_parallel_identical_requests_call_provider_once(self):
        eng = _make_engine()
        calls = {"n": 0}
        gate = threading.Event()
        arrived = threading.Event()

        def fake_call(provider, prompt, system, max_tokens):
            calls["n"] += 1
            arrived.set()
            gate.wait(timeout=5)
            return "JAWABAN-SAMA"

        eng._call_provider = fake_call
        prompt = "coalesce-identical-request-98765"

        with ThreadPoolExecutor(max_workers=3) as pool:
            # Fase 1: GENERATOR berangkat duluan & terdaftar (blok di gerbang).
            f_gen = pool.submit(eng.generate, prompt, 1, True)
            self.assertTrue(arrived.wait(timeout=5), "generator harus masuk fake_call")

            # Fase 2: barulah waiter berangkat — PASTI melihat event yang sudah
            # terdaftar, sehingga tidak mungkin jadi generator kedua.
            f_waiters = [pool.submit(eng.generate, prompt, 1, True) for _ in range(2)]
            deadline = time.time() + 5
            while time.time() < deadline and not all(f.running() for f in f_waiters):
                time.sleep(0.02)
            time.sleep(0.3)  # beri waktu waiter masuk event.wait
            gate.set()       # lepaskan generator
            results = [f_gen.result(timeout=10)] + [f.result(timeout=10) for f in f_waiters]

        self.assertEqual(calls["n"], 1, "request identik paralel hanya 1 panggilan API")
        self.assertEqual(len(set(results)), 1)

    def test_inflight_released_on_failure_path(self):
        eng = _make_engine()
        eng.api_keys["groq"] = "test-key"

        def fake_call(provider, prompt, system, max_tokens):
            return None  # gagal — jalur error

        eng._call_provider = fake_call
        out = eng.generate("single-flight-failure-111", use_cache=True, max_retries=1, max_total_wait=5)
        self.assertIn("semua AI provider sedang tidak tersedia", out)
        # Registrasi harus bersih walau gagal (finally di generate()).
        self.assertEqual(eng._inflight, {})

    def test_failure_cached_short_ttl_so_no_retry_herd(self):
        """Pesan error total di-cache singkat agar request identik berikutnya
        tidak ikut me-retry pipeline yang sedang down (anti-amplifikasi)."""
        eng = _make_engine()
        calls = {"n": 0}

        def fake_call(provider, prompt, system, max_tokens):
            calls["n"] += 1
            return None  # gagal

        eng._call_provider = fake_call
        prompt = "coalesce-failure-444"
        out1 = eng.generate(prompt, use_cache=True, max_retries=1, max_total_wait=5)
        out2 = eng.generate(prompt, use_cache=True, max_retries=1, max_total_wait=5)
        self.assertIn("semua AI provider sedang tidak tersedia", out1)
        self.assertEqual(out1, out2)
        self.assertEqual(calls["n"], 1, "error di-cache → request kedua tidak retry")


if __name__ == "__main__":
    unittest.main()
