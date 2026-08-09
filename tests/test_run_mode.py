"""Unit tests untuk resolve_run_mode — pemilihan mode webhook/polling.

Konteks: JustRunMy tidak meng-inject env var platform otomatis (beda dengan
Railway/Render), jadi deteksi JustRunMy dilakukan lewat WEBHOOK_URL terisi
atau BOT_RUN_MODE eksplisit.
"""

import unittest

from main import resolve_run_mode


class TestResolveRunMode(unittest.TestCase):
    def test_explicit_webhook(self):
        self.assertEqual(resolve_run_mode("webhook", False, ""), "webhook")

    def test_explicit_polling_overrides_cloud(self):
        # BOT_RUN_MODE=polling harus menang walau IS_CLOUD=True
        self.assertEqual(resolve_run_mode("polling", True, "https://x"), "polling")

    def test_explicit_case_insensitive(self):
        self.assertEqual(resolve_run_mode("WEBHOOK", False, ""), "webhook")

    def test_auto_cloud_detected(self):
        self.assertEqual(resolve_run_mode("auto", True, ""), "webhook")

    def test_auto_webhook_url_set(self):
        # Deteksi JustRunMy: WEBHOOK_URL terisi manual di panel
        self.assertEqual(resolve_run_mode("auto", False, "https://app.justrunmy.app"), "webhook")

    def test_auto_no_cloud_no_url_polling(self):
        # JustRunMy tanpa setup webhook → polling (jalan tanpa port publik)
        self.assertEqual(resolve_run_mode("auto", False, ""), "polling")

    def test_empty_mode_defaults_auto(self):
        self.assertEqual(resolve_run_mode("", False, ""), "polling")
        self.assertEqual(resolve_run_mode(None, False, ""), "polling")

    def test_unknown_mode_falls_to_auto(self):
        # Nilai typo (mis. "webhoook") tidak boleh crash — ikuti aturan auto
        self.assertEqual(resolve_run_mode("webhoook", False, ""), "polling")


if __name__ == "__main__":
    unittest.main()
