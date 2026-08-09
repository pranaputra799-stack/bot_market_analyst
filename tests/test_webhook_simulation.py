"""Regression test: mode webhook PTB 20.7 benar-benar jalan dengan tornado.

Latar belakang: sebelumnya `tornado` (HTTP server internal webhook PTB) TIDAK
ada di requirements — webhook mode crash saat start di production. Test ini
menyalakan server webhook NYATA (infrastruktur PTB 20.7 + tornado) secara
lokal dan memverifikasi perilaku HTTP, tanpa menghubungi api.telegram.org
(get_me & set_webhook di-stub).

Catatan: memakai atribut privat PTB (`_bot_user`, `updater.start_webhook`) —
karena requirements PIN python-telegram-bot==20.7, struktur ini stabil. Jika
PTB dinaikkan versi dan test mulai gagal, itu sinyal untuk meninjau ulang.

Tidak ada network: aiohttp probe ke 127.0.0.1 port ephemeral.
"""

import asyncio
import json
import socket
import unittest
from unittest import mock

import aiohttp
import telegram
from telegram.ext import Application

TOKEN = "123456789:AAFAKE"  # token palsu — API di-stub
SECRET = "rahasia-webhook-123"
PATH = TOKEN


def _ephemeral_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _run_simulation() -> dict:
    app = Application.builder().token(TOKEN).build()

    async def _fake_get_me(self, *args, **kwargs):
        user = telegram.User(id=123456789, is_bot=True, first_name="FakeBot", username="fakebot")
        self._bot_user = user  # get_me asli menyimpan hasil ke self._bot_user
        return user

    async def _fake_set_webhook(self, *args, **kwargs):
        return True

    port = _ephemeral_port()
    bot_cls = type(app.bot)  # ExtBot — instance attr dilarang, patch class
    with mock.patch.object(bot_cls, "get_me", new=_fake_get_me), \
            mock.patch.object(bot_cls, "set_webhook", new=_fake_set_webhook):
        # Urutan sama seperti Application.run_webhook: initialize → start → start_webhook
        await app.initialize()
        await app.start()
        await app.updater.start_webhook(
            listen="127.0.0.1",
            port=port,
            url_path=PATH,
            webhook_url=f"http://127.0.0.1:{port}/{PATH}",
            secret_token=SECRET,
        )

        valid = json.dumps({
            "update_id": 1,
            "message": {
                "message_id": 1, "date": 0,
                "chat": {"id": 1, "type": "private"},
                "text": None,  # tidak memicu pipeline AI
            },
        }).encode()

        async def post(path: str, body: bytes, secret=None):
            headers = {"Content-Type": "application/json"}
            if secret is not None:
                headers["X-Telegram-Bot-Api-Secret-Token"] = secret
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/{path}", data=body,
                    headers=headers, timeout=5,
                ) as resp:
                    return resp.status

        async def get(path: str):
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/{path}", timeout=5,
                ) as resp:
                    return resp.status

        results = {
            "valid_ok": await post(PATH, valid, SECRET),
            "wrong_secret": await post(PATH, valid, "salah"),
            "no_secret": await post(PATH, valid),
            "wrong_path": await post("salah", valid, SECRET),
            "get_method": await get(PATH),
        }
        # Urutan teardown SAMA PERSIS dengan Application.__run (finally block):
        # updater.stop() → app.stop() → shutdown(). Tanpa updater.stop() dulu,
        # shutdown() raise "This Updater is still running!".
        if app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
    return results


class TestWebhookSimulation(unittest.TestCase):
    """Server webhook tornado PTB 20.7 berfungsi penuh secara lokal."""

    def test_webhook_serves_and_validates(self):
        # Guard timeout: server webhook tak boleh menggantung suite selamanya
        # bila PTB berubah perilaku shutdown.
        async def _bounded():
            return await asyncio.wait_for(_run_simulation(), timeout=30)

        results = asyncio.run(_bounded())
        self.assertEqual(results["valid_ok"], 200, "Update valid + secret benar harus 200")
        self.assertEqual(results["wrong_secret"], 403, "Secret salah harus ditolak")
        self.assertEqual(results["no_secret"], 403, "Tanpa secret harus ditolak")
        self.assertEqual(results["wrong_path"], 404, "Path salah harus 404")
        self.assertEqual(results["get_method"], 405, "GET bukan POST harus ditolak")


if __name__ == "__main__":
    unittest.main()
