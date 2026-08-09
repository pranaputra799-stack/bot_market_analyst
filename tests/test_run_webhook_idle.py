"""Regresi: run_webhook TIDAK boleh memanggil Application.idle().

PTB 20.7 menghapus method idle() dari Application (AttributeError →
"Webhook mode gagal start" saat deploy). Penggantinya: blocking
loop.run_forever() yang berhenti via loop.stop() (signal handler /
stop_running()), lalu finally membersihkan aiohttp runner + application.

Test ini menjalankan run_webhook SUNGGAUHAN (semua dependensi dimock) dengan
application palsu yang TIDAK punya method idle — kalau kode masih memanggil
idle(), test ini gagal dengan AttributeError.
"""

import asyncio
import threading
import time
import unittest
from unittest import mock

import main as main_mod


class _FakeBot:
    def __init__(self):
        self.set_webhook_calls = []

    async def set_webhook(self, url=None, secret_token=None):
        self.set_webhook_calls.append((url, secret_token))


class _FakeApplication:
    """Meniru Application PTB 20.7: method yang ada, TANPA idle()."""

    def __init__(self):
        self.bot = _FakeBot()
        self.running = True
        self.started = False
        self.stopped = False
        self.shutdown_called = False

    async def initialize(self):
        pass

    async def start(self):
        self.started = True
        # Hentikan loop 0,5 detik setelah start — simulasi sinyal stop
        # (mekanisme yang sama dengan stop_running() → loop.stop()).
        asyncio.get_running_loop().call_later(0.5, asyncio.get_running_loop().stop)

    async def stop(self):
        self.stopped = True

    async def shutdown(self):
        self.shutdown_called = True

    async def process_update(self, update):
        pass


class TestRunWebhookIdle(unittest.TestCase):
    def test_run_webhook_blocks_and_cleans_up_without_idle(self):
        app = _FakeApplication()
        result = {}

        def _run():
            try:
                with mock.patch.object(main_mod, "build_application", return_value=app), \
                        mock.patch.object(main_mod, "WEBHOOK_URL", "http://example.com"), \
                        mock.patch.object(main_mod, "WEBHOOK_LISTEN", "127.0.0.1"), \
                        mock.patch.object(main_mod, "PORT", 0), \
                        mock.patch.object(main_mod, "WEBHOOK_SECRET", "sec-123456"), \
                        mock.patch.object(main_mod, "TELEGRAM_TOKEN", "123456789:AAFAKE"):
                    main_mod.run_webhook()
                result["ok"] = True
            except Exception as e:  # pragma: no cover - detail error
                result["error"] = repr(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=8)

        self.assertFalse(t.is_alive(), "run_webhook masih berjalan (loop tidak berhenti)")
        self.assertNotIn("error", result, result.get("error", ""))
        self.assertTrue(result.get("ok"))
        self.assertTrue(app.started, "application.start() tidak dipanggil")
        self.assertTrue(app.stopped, "application.stop() tidak dipanggil (cleanup)")
        self.assertTrue(app.shutdown_called, "application.shutdown() tidak dipanggil")
        self.assertEqual(
            app.bot.set_webhook_calls[0][0],
            "http://example.com/123456789:AAFAKE",
            "URL webhook tidak sesuai",
        )


if __name__ == "__main__":
    unittest.main()
