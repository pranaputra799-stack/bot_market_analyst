"""Regresi: run_webhook TIDAK boleh memanggil Application.idle().

Application.idle() tidak ada di PTB 20.x maupun 22.x (AttributeError →
"Webhook mode gagal start" saat deploy). Penggantinya: blocking
loop.run_forever() yang berhenti via loop.stop() (signal handler /
stop_running()), lalu finally membersihkan aiohttp runner + application.

Test ini menjalankan run_webhook SUNGGAUHAN (semua dependensi dimock) dengan
application palsu yang TIDAK punya method idle — kalau kode masih memanggil
idle(), test ini gagal dengan AttributeError.
"""

import asyncio
import threading
import unittest
from unittest import mock

import main as main_mod


class _FakeBot:
    def __init__(self):
        self.set_webhook_calls = []

    async def set_webhook(self, url=None, secret_token=None):
        self.set_webhook_calls.append((url, secret_token))


class _FakeApplication:
    """Meniru Application PTB: method yang ada, TANPA idle()."""

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
    def test_setup_failure_idles_instead_of_crashing(self):
        """Regresi: set_webhook gagal (mis. Telegram API down) TIDAK boleh
        crash-loop. Server sudah bind — biarkan hidup (idle) untuk diagnosa,
        bukan melempar exception ke main() yang memicu restart berulang.
        """

        class FailingBot(_FakeBot):
            async def set_webhook(self, url=None, secret_token=None):
                raise RuntimeError("Telegram API down")

        class FailingApp(_FakeApplication):
            def __init__(self):
                super().__init__()
                self.bot = FailingBot()

        app = FailingApp()
        result = {}

        def _run():
            try:
                with mock.patch.object(main_mod, "build_application", return_value=app), \
                        mock.patch.object(main_mod, "WEBHOOK_URL", "http://example.com"), \
                        mock.patch.object(main_mod, "WEBHOOK_LISTEN", "127.0.0.1"), \
                        mock.patch.object(main_mod, "PORT", 0), \
                        mock.patch.object(main_mod, "WEBHOOK_SECRET", "sec-123456"), \
                        mock.patch.object(main_mod, "TELEGRAM_TOKEN", "123456789:AAFAKE"), \
                        mock.patch.object(main_mod, "SETUP_RETRY_ATTEMPTS", 2), \
                        mock.patch.object(main_mod, "SETUP_RETRY_BASE_DELAY", 0.01):
                    main_mod.run_webhook()
                result["ok"] = True
            except Exception as e:  # pragma: no cover - detail error
                result["error"] = repr(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=8)

        self.assertFalse(t.is_alive(), "set_webhook gagal harus idle (block), bukan crash")
        self.assertNotIn("error", result, result.get("error", ""))
        self.assertTrue(result.get("ok"), "run_webhook tidak boleh melempar ke main()")

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


class TestSetupWebhookRetry(unittest.IsolatedAsyncioTestCase):
    """Retry per-fase: error transient Telegram (500) tidak membuat bot idle."""

    class _QuietApp(_FakeApplication):
        """start() tidak menjadwalkan loop.stop (test murni retry, tanpa loop)."""

        async def start(self):
            self.started = True

    def _app(self, set_webhook_impl=None):
        app = self._QuietApp()
        if set_webhook_impl is not None:
            app.bot.set_webhook = set_webhook_impl
        return app

    async def test_transient_failure_then_success(self):
        """set_webhook gagal 1x (transient) lalu sukses → webhook terdaftar."""
        calls = {"n": 0}

        async def flaky(url=None, secret_token=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Telegram API 500 (transient)")
            return None

        app = self._app(flaky)
        with mock.patch.object(main_mod, "SETUP_RETRY_ATTEMPTS", 3), \
                mock.patch.object(main_mod, "SETUP_RETRY_BASE_DELAY", 0.01):
            exc = await main_mod._setup_webhook_with_retry(app)

        self.assertIsNone(exc, "retry harus berhasil setelah error transient")
        self.assertEqual(calls["n"], 2, "harus di-retry tepat sekali")
        self.assertTrue(app.started)

    async def test_persistent_failure_returns_last_error(self):
        """Semua percobaan gagal → return Exception (pemanggil idle, bukan crash)."""
        calls = {"n": 0}

        async def always_fail(url=None, secret_token=None):
            calls["n"] += 1
            raise RuntimeError("Telegram API down")

        app = self._app(always_fail)
        with mock.patch.object(main_mod, "SETUP_RETRY_ATTEMPTS", 2), \
                mock.patch.object(main_mod, "SETUP_RETRY_BASE_DELAY", 0.01):
            exc = await main_mod._setup_webhook_with_retry(app)

        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(calls["n"], 2, "harus mencoba sesuai SETUP_RETRY_ATTEMPTS")

    async def test_initialize_retried_too(self):
        """initialize() yang gagal transient juga di-retry (kasus di Render)."""
        init_calls = {"n": 0}
        app = self._app()

        async def flaky_initialize():
            init_calls["n"] += 1
            if init_calls["n"] == 1:
                raise RuntimeError("get_me: Internal Server Error (500)")

        app.initialize = flaky_initialize
        with mock.patch.object(main_mod, "SETUP_RETRY_ATTEMPTS", 3), \
                mock.patch.object(main_mod, "SETUP_RETRY_BASE_DELAY", 0.01):
            exc = await main_mod._setup_webhook_with_retry(app)

        self.assertIsNone(exc)
        self.assertEqual(init_calls["n"], 2)
        self.assertTrue(app.started)
        self.assertEqual(len(app.bot.set_webhook_calls), 1)


if __name__ == "__main__":
    unittest.main()
