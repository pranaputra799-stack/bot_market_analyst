"""Unit tests untuk _token_valid_async (main.py) — pemetaan exception PTB 20.x.

Regresi: PTB 20.x melempar telegram.error.InvalidToken (bukan Unauthorized
yang ada di PTB <20) saat token ditolak. Pastikan:
- token valid → True
- InvalidToken (token ditolak) → False
- NetworkError / TimedOut (sementara) → True (bot tetap mencoba jalan)
"""

import asyncio
import unittest
from unittest import mock

import telegram

import main as main_mod


class TestTokenValidation(unittest.TestCase):
    def _check(self, get_me_side_effect=None, get_me_return=None):
        bot_cls = telegram.Bot
        get_me = mock.AsyncMock()
        if get_me_side_effect is not None:
            get_me.side_effect = get_me_side_effect
        else:
            get_me.return_value = get_me_return
        close = mock.AsyncMock()
        with mock.patch.object(bot_cls, "get_me", get_me), \
                mock.patch.object(bot_cls, "close", close):
            return asyncio.run(main_mod._token_valid_async("123456789:AAFAKE"))

    def test_valid_token_returns_true(self):
        user = telegram.User(id=123, is_bot=True, first_name="Bot", username="mybot")
        self.assertTrue(self._check(get_me_return=user))

    def test_invalid_token_returns_false(self):
        """InvalidToken (token ditolak) harus FATAL → False (idle, bukan crash)."""
        self.assertFalse(self._check(get_me_side_effect=telegram.error.InvalidToken("401")))

    def test_network_error_is_transient_returns_true(self):
        """NetworkError/TimedOut bersifat sementara → True agar bot tetap coba jalan."""
        self.assertTrue(self._check(get_me_side_effect=telegram.error.NetworkError("timeout")))
        self.assertTrue(self._check(get_me_side_effect=telegram.error.TimedOut()))

    def test_other_exceptions_are_transient(self):
        self.assertTrue(self._check(get_me_side_effect=RuntimeError("bukan API error")))


if __name__ == "__main__":
    unittest.main()
