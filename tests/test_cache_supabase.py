"""Unit tests untuk cache hybrid (L1 memori terbatas + L2 Supabase mock).

TANPA network — SupabaseCache di-mock dengan FakePersistent yang meniru
kontrak get/set/delete/enabled.
"""

import hashlib
import time
import unittest
from contextlib import contextmanager

import data.cache as cache_mod
import data.conversation_memory as cm


class FakePersistent:
    """Stub SupabaseCache (L2) — kontrak sama, tanpa network."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.storage = {}
        self.set_calls = []
        self.delete_calls = []

    def get(self, key):
        return self.storage.get(key) if self.enabled else None

    def set(self, key, value, ttl):
        if not self.enabled:
            return
        self.set_calls.append((key, value, ttl))
        self.storage[key] = value

    def delete(self, key):
        if not self.enabled:
            return
        self.delete_calls.append(key)
        self.storage.pop(key, None)

    def cleanup_expired(self):
        pass


class FakeSession:
    """Session requests tiruan untuk patch data.cache._session (tanpa network)."""

    def __init__(self, get_resp=None):
        self.get_resp = get_resp
        self.post_resp = None
        self.captured = {}

    def get(self, *a, **k):
        return self.get_resp

    def post(self, *a, **k):
        self.captured["json"] = k.get("json")
        return self.post_resp

    def delete(self, *a, **k):
        return self.get_resp


@contextmanager
def _patch_session(fake):
    """Alihkan data.cache._session ke FakeSession untuk durasi blok."""
    original = cache_mod._session
    cache_mod._session = lambda: fake
    try:
        yield fake
    finally:
        cache_mod._session = original


class TestMemoryCacheBound(unittest.TestCase):
    def test_fifo_eviction_when_full(self):
        mc = cache_mod.MemoryCache(max_entries=3)
        mc.set("k1", 1)
        mc.set("k2", 2)
        mc.set("k3", 3)
        mc.set("k4", 4)  # k1 (terlama) harus ter-evict
        self.assertIsNone(mc.get("k1"))
        self.assertEqual(mc.get("k2"), 2)
        self.assertEqual(mc.get("k4"), 4)
        self.assertLessEqual(len(mc._cache), 3)

    def test_no_eviction_without_cap(self):
        mc = cache_mod.MemoryCache(max_entries=0)  # 0 = tanpa batas
        for i in range(100):
            mc.set(f"k{i}", i)
        self.assertEqual(len(mc._cache), 100)

    def test_update_existing_key_does_not_evict(self):
        mc = cache_mod.MemoryCache(max_entries=2)
        mc.set("a", 1)
        mc.set("b", 2)
        mc.set("a", 10)  # update key yang sudah ada — tidak menambah entri, tanpa evict
        self.assertEqual(mc.get("a"), 10)
        self.assertEqual(mc.get("b"), 2)
        self.assertEqual(len(mc._cache), 2)

    def test_cleanup_expired_removes_only_expired(self):
        mc = cache_mod.MemoryCache(max_entries=0)
        mc.set("expired", "x", ttl=-1)  # langsung kedaluwarsa
        mc.set("fresh", "y", ttl=300)
        removed = mc.cleanup_expired()
        self.assertEqual(removed, 1)
        self.assertIsNone(mc.get("expired"))
        self.assertEqual(mc.get("fresh"), "y")


class TestSupabaseCacheParsing(unittest.TestCase):
    """Mimic bentuk respons PostgREST: kolom jsonb SUDAH ter-parse oleh
    resp.json() (bukan string JSON). Regresi untuk bug double json.loads.
    """

    def _sc(self):
        return cache_mod.SupabaseCache(url="https://x.supabase.co", key="k", enabled=True)

    def _fake_resp(self, value, expires_at=None):
        from datetime import datetime, timedelta, timezone

        exp = expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        class FakeResp:
            status_code = 200

            def __init__(self, rows):
                self._rows = rows

            def json(self):
                return self._rows

        return FakeResp([{"value": value, "expires_at": exp}])

    def test_get_returns_parsed_string(self):
        sc = self._sc()
        with _patch_session(FakeSession(self._fake_resp("JAWABAN"))):
            self.assertEqual(sc.get("ai:abc"), "JAWABAN")

    def test_get_returns_parsed_list(self):
        sc = self._sc()
        rows = [{"q": "pertanyaan", "a": "jawaban"}]
        with _patch_session(FakeSession(self._fake_resp(rows))):
            self.assertEqual(sc.get("conversation:1"), rows)

    def test_get_expired_returns_none(self):
        from datetime import datetime, timedelta, timezone

        sc = self._sc()
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with _patch_session(FakeSession(self._fake_resp("LAMA", expired))):
            self.assertIsNone(sc.get("ai:old"))

    def test_get_http_error_returns_none(self):
        sc = self._sc()

        class ErrResp:
            status_code = 404

            def json(self):
                return []

        with _patch_session(FakeSession(ErrResp())):
            self.assertIsNone(sc.get("ai:missing"))


class TestSupabaseCacheWritePayload(unittest.TestCase):
    """Regresi: payload HTTP ke PostgREST harus membawa nilai MENTAH (tanpa
    json.dumps). json.dumps double-encode di Supabase asli — string terbaca
    dengan kutip literal dan list berubah menjadi string (ditemukan lewat
    live E2E terhadap Supabase sungguhan).
    """

    def _sc(self):
        return cache_mod.SupabaseCache(url="https://x.supabase.co", key="k", enabled=True)

    def _capture_post(self):
        fake = FakeSession()

        class FakeResp:
            status_code = 201

            def json(self):
                return []

        fake.post_resp = FakeResp()
        return fake

    def _wait_for(self, captured, timeout=3.0):
        """Tunggu deterministik sampai background thread mengirim payload."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "json" in captured:
                return
            time.sleep(0.05)
        raise AssertionError(f"background thread tidak mengirim payload dalam {timeout}s")

    def test_set_sends_raw_string_value(self):
        sc = self._sc()
        fake = self._capture_post()
        with _patch_session(fake):
            sc.set("ai:abc", "TEXT_POLOS", 300)
            self._wait_for(fake.captured)
            payload = fake.captured["json"]
            self.assertEqual(payload["key"], "ai:abc")
            self.assertEqual(payload["value"], "TEXT_POLOS")
            self.assertIn("expires_at", payload)

    def test_set_sends_raw_list_value(self):
        sc = self._sc()
        fake = self._capture_post()
        with _patch_session(fake):
            rows = [{"q": "tanya", "a": "jawab"}]
            sc.set("conversation:1", rows, 900)
            self._wait_for(fake.captured)
            # Nilai list dikirim mentah (bukan string hasil json.dumps)
            self.assertEqual(fake.captured["json"]["value"], rows)

    def test_set_disabled_sends_nothing(self):
        sc = cache_mod.SupabaseCache(url="https://x.supabase.co", key="k", enabled=False)
        fake = FakeSession()
        with _patch_session(fake):
            sc.set("ai:x", "y", 300)
            time.sleep(0.4)
            # enabled=False → worker tidak pernah dipanggil
            self.assertEqual(fake.captured, {})


class TestLayeredAICache(unittest.TestCase):
    def setUp(self):
        self._orig = cache_mod.persistent
        self.fake = FakePersistent(enabled=True)
        cache_mod.persistent = self.fake
        self.key = f"ai:{hashlib.md5(b'layered-test-prompt').hexdigest()}"
        cache_mod.cache.delete(self.key)

    def tearDown(self):
        cache_mod.persistent = self._orig
        cache_mod.cache.delete(self.key)
        self.fake.storage.pop(self.key, None)

    def test_set_writes_both_layers(self):
        cache_mod.set_cached_ai_response("layered-test-prompt", "JAWABAN")
        self.assertEqual(cache_mod.cache.get(self.key), "JAWABAN")
        self.assertEqual(self.fake.storage.get(self.key), "JAWABAN")

    def test_get_falls_back_to_l2_when_memory_empty(self):
        self.fake.storage[self.key] = "DARI-SUPABASE"
        got = cache_mod.get_cached_ai_response("layered-test-prompt")
        self.assertEqual(got, "DARI-SUPABASE")
        # Harus mengisi ulang memori (L1)
        self.assertEqual(cache_mod.cache.get(self.key), "DARI-SUPABASE")

    def test_get_memory_hit_skips_l2(self):
        cache_mod.cache.set(self.key, "DARI-MEMORI", ttl=300)
        got = cache_mod.get_cached_ai_response("layered-test-prompt")
        self.assertEqual(got, "DARI-MEMORI")

    def test_l2_disabled_no_write(self):
        fake_off = FakePersistent(enabled=False)
        cache_mod.persistent = fake_off
        cache_mod.set_cached_ai_response("layered-test-prompt", "x")
        self.assertEqual(fake_off.storage, {})
        self.assertEqual(fake_off.set_calls, [])
        # Baca dari memori tetap jalan
        self.assertEqual(cache_mod.get_cached_ai_response("layered-test-prompt"), "x")


class TestConversationMemoryPersistence(unittest.TestCase):
    def setUp(self):
        self._orig = cm.persistent
        self.fake = FakePersistent(enabled=True)
        cm.persistent = self.fake
        cm.clear(777)
        cm.clear(778)

    def tearDown(self):
        cm.persistent = self._orig
        cm.clear(777)
        cm.clear(778)

    def test_add_exchange_persists_to_l2(self):
        cm.add_exchange(777, "q1", "a1")
        key = cm._key(777)
        self.assertIn(key, self.fake.storage)
        self.assertEqual(self.fake.storage[key][0]["q"], "q1")

    def test_get_history_reads_from_l2_after_memory_clear(self):
        cm.add_exchange(777, "q1", "a1")
        cm.cache.delete(cm._key(777))  # simulasi restart / memory kosong
        history = cm.get_history(777)
        self.assertEqual(history[0]["q"], "q1")

    def test_clear_removes_from_both_layers(self):
        cm.add_exchange(777, "q1", "a1")
        cm.clear(777)
        self.assertEqual(cm.get_history(777), [])
        self.assertNotIn(cm._key(777), self.fake.storage)

    def test_l2_disabled_still_works_in_memory(self):
        cm.persistent = FakePersistent(enabled=False)
        cm.add_exchange(777, "q1", "a1")
        self.assertEqual(cm.get_history(777)[0]["q"], "q1")
        cm.clear(777)


if __name__ == "__main__":
    unittest.main()
