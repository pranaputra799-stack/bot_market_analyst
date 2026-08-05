"""Unit tests untuk AIFallbackEngine — TANPA network.

Provider dipalsukan (stub _call_provider) sehingga tidak ada request keluar.
Fokus:
- Fallback & statistik
- System override dikirim per-request (regresi refactor penghapusan lock)
- Keamanan saat dipanggil paralel dari banyak thread
- Cache hit
"""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor

from ai.engine import AIFallbackEngine


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


class TestStats(unittest.TestCase):
    def test_get_stats_shape(self):
        eng = _make_engine()
        stats = eng.get_stats()
        self.assertIn("available_providers", stats)
        self.assertIn("provider_names", stats)
        self.assertIn("groq", stats["available_providers"])


if __name__ == "__main__":
    unittest.main()
