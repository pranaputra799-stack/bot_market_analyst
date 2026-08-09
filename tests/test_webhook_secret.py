"""Regresi: WEBHOOK_SECRET harus hanya berisi karakter yang diizinkan Telegram.

Telegram setWebhook menolak secret yang mengandung karakter selain
A-Z, a-z, 0-9, _ dan - dengan error "Secret token contains unallowed
characters".

Bug sebelumnya: default WEBHOOK_SECRET = TELEGRAM_TOKEN[:64], padahal token
Telegram berformat "123456789:AAFAKE..." yang mengandung titik dua ':' →
bot gagal start di mode webhook (BadRequest). Sekarang default diturunkan
dari sha256(token) (hex, dijamin valid) dan env WEBHOOK_SECRET di-sanitasi.
"""

import hashlib
import os
import unittest

import config.settings as settings


class TestWebhookSecret(unittest.TestCase):
    def test_secret_only_contains_allowed_chars(self):
        """Invariant: secret webhook hanya karakter yang diizinkan Telegram."""
        self.assertRegex(settings.WEBHOOK_SECRET, r"^[A-Za-z0-9_-]{1,256}$")

    @unittest.skipIf(
        "WEBHOOK_SECRET" in os.environ, "WEBHOOK_SECRET di-set di environment"
    )
    def test_default_secret_derived_from_token_hash(self):
        """Tanpa env WEBHOOK_SECRET → sha256(token)[:64] (hex, deterministik)."""
        expected = hashlib.sha256(
            settings.TELEGRAM_TOKEN.encode("utf-8")
        ).hexdigest()[:64]
        self.assertEqual(settings.WEBHOOK_SECRET, expected)

    def test_env_secret_is_sanitized(self):
        """Karakter tak valid (:, !, spasi, $) dibuang dari env WEBHOOK_SECRET."""
        raw = "abc:def!ghi$ 123-456_789"
        self.assertEqual(settings._safe_webhook_secret(raw), "abcdefghi123-456_789")

    def test_sanitizer_drops_colon_like_bot_token(self):
        """Token Telegram (123456789:AAFAKE) harus bersih dari ':'."""
        token_like = "123456789:AAFAKE_TOKEN_EXAMPLE"
        cleaned = settings._safe_webhook_secret(token_like)
        self.assertNotIn(":", cleaned)
        self.assertRegex(cleaned, r"^[A-Za-z0-9_-]{1,256}$")


if __name__ == "__main__":
    unittest.main()
