"""Unit tests untuk kontrak session HTTP bersama (data/http_session.py).

Memastikan:
- get_aiohttp_session() mengembalikan INSTANCE SAMA dalam satu event loop
  (keep-alive benar-benar dipakai, bukan session baru per panggilan).
- Bila event loop berubah (restart / asyncio.run baru), session dibuat ulang
  (tidak memakai session milik loop yang sudah mati).
- get_requests_session() stabil dalam satu thread, berbeda antar thread.
"""

import asyncio
import threading
import unittest

from data.http_session import get_aiohttp_session, get_requests_session


class TestSharedAiohttpSession(unittest.TestCase):
    """Kontrak ClientSession aiohttp bersama."""

    def _with_loop(self, fn):
        """Jalankan coroutine di event loop baru (ditutup rapi)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(fn())
            # Tutup session di loop yang sama agar tidak ada warning "unclosed"
            try:
                loop.run_until_complete(get_aiohttp_session().close())
            except Exception:
                pass
            return result
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def test_reuses_same_instance_within_loop(self):
        async def main():
            s1 = get_aiohttp_session()
            s2 = get_aiohttp_session()
            s3 = get_aiohttp_session()
            return s1, s2, s3, s1.closed

        s1, s2, s3, closed = self._with_loop(main)
        self.assertIs(s1, s2, "Session aiohttp harus di-reuse dalam satu loop")
        self.assertIs(s1, s3)
        self.assertFalse(closed, "Session tidak boleh tertutup (bukan async with)")

    def test_recreated_when_loop_changes(self):
        async def get():
            return get_aiohttp_session()

        s1 = self._with_loop(get)
        s2 = self._with_loop(get)  # event loop baru → instance baru
        self.assertIsNot(s1, s2, "Session aiohttp tidak boleh dipakai lintas loop")


class TestSharedRequestsSession(unittest.TestCase):
    """Kontrak Session requests per-thread."""

    def test_same_thread_same_instance(self):
        self.assertIs(
            get_requests_session(),
            get_requests_session(),
            "Session requests harus stabil dalam satu thread",
        )

    def test_other_thread_different_instance(self):
        main_s = get_requests_session()
        other = {}

        def worker():
            other["s"] = get_requests_session()

        th = threading.Thread(target=worker)
        th.start()
        th.join()
        self.assertIsNot(other["s"], main_s, "Tiap thread punya session sendiri")


if __name__ == "__main__":
    unittest.main()
