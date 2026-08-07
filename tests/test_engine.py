"""Unit tests untuk AIFallbackEngine — TANPA network.

Provider dipalsukan (stub _call_provider) sehingga tidak ada request keluar.
Fokus:
- Fallback & statistik
- System override dikirim per-request (regresi refactor penghapusan lock)
- Keamanan saat dipanggil paralel dari banyak thread
- Cache hit
"""

import asyncio
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from ai.engine import AIFallbackEngine
from config.providers import PROVIDER_CONFIGS


def _make_engine():
    """Engine dengan key groq palsu & _call_provider di-stub."""
    eng = AIFallbackEngine()
    eng.api_keys["groq"] = "test-key"

    def fake_call(provider, prompt, system, max_tokens):
        return f"RESP[{system}][{max_tokens}]:{prompt}"

    eng._call_provider = fake_call
    return eng


class TestGenerateBasics(unittest.TestCase):
    def test_success_uses_first_provider(self):
        eng = _make_engine()
        out = eng.generate("prompt-1", use_cache=False)
        self.assertIn("prompt-1", out)
        self.assertEqual(eng.stats["successful"], 1)
        self.assertEqual(eng.stats["provider_usage"]["groq"], 1)

    def test_no_key_returns_error_message(self):
        import logging

        eng = AIFallbackEngine()  # semua key kosong
        with self.assertLogs(level="WARNING"):
            out = eng.generate("prompt-2", use_cache=False)
        self.assertIn("semua AI provider sedang tidak tersedia", out)
        self.assertEqual(eng.stats["failed"], 1)

    def test_system_override_passthrough(self):
        eng = _make_engine()
        out = eng.generate("prompt-3", use_cache=False, system_override="SYS-BETA")
        self.assertTrue(out.startswith("RESP[SYS-BETA]"), out)

    def test_max_tokens_passthrough(self):
        eng = _make_engine()
        out = eng.generate("prompt-4", use_cache=False, max_tokens=2048)
        self.assertIn("[2048]", out)

    def test_via_prefix_only_without_override(self):
        eng = _make_engine()
        out = eng.generate("prompt-5", use_cache=False)
        self.assertIn("[via", out)
        out2 = eng.generate("prompt-6", use_cache=False, system_override="SYS-X")
        self.assertNotIn("[via", out2)


class TestParallelSafety(unittest.TestCase):
    def test_parallel_requests_keep_own_system(self):
        eng = _make_engine()
        systems = [f"SYS-{i}" for i in range(8)]
        prompts = [f"prompt-{i}" for i in range(8)]

        def call(i):
            return eng.generate(prompts[i], use_cache=False, system_override=systems[i])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(8)))

        for i, out in enumerate(results):
            self.assertTrue(
                out.startswith(f"RESP[{systems[i]}]"),
                f"System override tertukar untuk thread {i}: {out}",
            )


class TestCache(unittest.TestCase):
    def test_cached_response_skips_provider(self):
        eng = _make_engine()
        calls = {"n": 0}

        def fake_call(provider, prompt, system, max_tokens):
            calls["n"] += 1
            return f"RESP:{prompt}"

        eng._call_provider = fake_call

        prompt = "cache-unique-please-12345"
        out1 = eng.generate(prompt, use_cache=True, system_override="SYS-CACHE")
        out2 = eng.generate(prompt, use_cache=True, system_override="SYS-CACHE")
        self.assertEqual(out1, out2)
        self.assertEqual(calls["n"], 1)  # provider hanya dipanggil sekali

    def test_generate_async(self):
        eng = _make_engine()
        out = asyncio.run(
            eng.generate_async("prompt-async", use_cache=False, system_override="SYS-ASYNC")
        )
        self.assertTrue(out.startswith("RESP[SYS-ASYNC]"), out)


class TestRateLimitHandling(unittest.TestCase):
    """429 harus dicatat sebagai hint + cooldown TANPA tidur di _call_provider
    (backoff dijalankan satu kali di generate(), hindari double-wait)."""

    def test_429_records_hint_and_cooldown(self):
        eng = _make_engine()

        class FakeResp:
            status_code = 429
            headers = {"Retry-After": "5"}

            def json(self):
                return {}

        import ai.engine as engine_mod
        original = engine_mod.requests.post
        try:
            engine_mod.requests.post = lambda *a, **k: FakeResp()
            result = eng._call_openai_compatible(
                "groq", PROVIDER_CONFIGS["groq"], "test-key", "p", "s", 1024
            )
        finally:
            engine_mod.requests.post = original

        self.assertIsNone(result)
        self.assertAlmostEqual(eng._rate_limit_hints.get("groq"), 5.0)
        self.assertGreater(eng._provider_cooldown.get("groq", 0), time.time())

    def test_ordered_providers_puts_cooldown_last(self):
        eng = _make_engine()
        eng.fallback_order = ["openrouter", "groq", "gemini"]
        eng._order_index = {p: i for i, p in enumerate(eng.fallback_order)}
        eng._provider_cooldown["groq"] = time.time() + 30
        order = eng._ordered_providers()
        self.assertEqual(order[0], "openrouter")
        self.assertEqual(order[-1], "groq")

    def test_backoff_wait_uses_retry_after_hint(self):
        eng = _make_engine()
        eng._rate_limit_hints["groq"] = 4.0
        deadline = time.time() + 60
        wait = eng._backoff_wait("groq", 0, deadline)
        self.assertAlmostEqual(wait, 4.0)
        # Hint tetap tersimpan agar thread lain ikut menghormati Retry-After
        self.assertEqual(eng._rate_limit_hints.get("groq"), 4.0)

    def test_backoff_wait_skips_when_hint_exceeds_budget(self):
        eng = _make_engine()
        eng._rate_limit_hints["groq"] = 10.0
        deadline = time.time() + 3  # sisa budget 3 detik < hint 10 detik
        wait = eng._backoff_wait("groq", 0, deadline)
        self.assertEqual(wait, 0.0)

    def test_success_clears_rate_limit_state(self):
        eng = _make_engine()
        eng._rate_limit_hints["groq"] = 5.0
        eng._provider_cooldown["groq"] = time.time() + 5
        out = eng.generate("prompt-clear-hint", use_cache=False)
        self.assertIn("prompt-clear-hint", out)
        self.assertNotIn("groq", eng._rate_limit_hints)
        self.assertNotIn("groq", eng._provider_cooldown)


class TestDeadModelBlacklist(unittest.TestCase):
    """Model yang 404 (hilang/tidak gratis/diblokir guardrail) di-blacklist
    sementara agar tidak di-retry berulang di setiap generate()."""

    def test_404_model_skipped_on_next_call(self):
        import ai.engine as engine_mod

        eng = _make_engine()

        class FakeResp404:
            status_code = 404
            headers = {}
            text = '{"error":{"message":"No endpoints available matching your guardrail restrictions"}}'

            def json(self):
                return {}

        original = engine_mod.requests.post
        calls = {"n": 0}
        try:
            def fake_post(*a, **k):
                calls["n"] += 1
                return FakeResp404()

            engine_mod.requests.post = fake_post

            # Panggilan 1: semua model 404 -> ter-blacklist
            result = eng._call_openai_compatible(
                "groq", PROVIDER_CONFIGS["groq"], "test-key", "p", "s", 1024
            )
            self.assertIsNone(result)
            self.assertGreaterEqual(len(eng._dead_models), 1)
            first_calls = calls["n"]

            # Panggilan 2: model mati di-skip -> TANPA request baru
            result2 = eng._call_openai_compatible(
                "groq", PROVIDER_CONFIGS["groq"], "test-key", "p", "s", 1024
            )
            self.assertIsNone(result2)
            self.assertEqual(calls["n"], first_calls)
        finally:
            engine_mod.requests.post = original

    def test_429_not_blacklisted(self):
        """429 adalah kuota, bukan model mati — TIDAK boleh masuk blacklist."""
        import ai.engine as engine_mod

        eng = _make_engine()

        class FakeResp429:
            status_code = 429
            headers = {"Retry-After": "3"}
            text = "rate limited"

            def json(self):
                return {}

        original = engine_mod.requests.post
        try:
            engine_mod.requests.post = lambda *a, **k: FakeResp429()
            eng._call_openai_compatible("groq", PROVIDER_CONFIGS["groq"], "test-key", "p", "s", 1024)
        finally:
            engine_mod.requests.post = original

        self.assertEqual(eng._dead_models, {})


class TestStats(unittest.TestCase):
    def test_get_stats_shape(self):
        eng = _make_engine()
        stats = eng.get_stats()
        self.assertIn("available_providers", stats)
        self.assertIn("provider_names", stats)
        self.assertIn("groq", stats["available_providers"])


if __name__ == "__main__":
    unittest.main()
