"""Unit tests untuk server webhook aiohttp MILIK KITA (main.build_webhook_app).

Latar belakang: server webhook tornado bawaan PTB hanya menerima POST
(SUPPORTED_METHODS=("POST",)) → GET /health dari UptimeRobot / Render health
check selalu 405/404. Server kita menggantikannya: GET /health → 200 JSON
(keep-alive & healthCheckPath Render) + POST /<token> → process_update.

Tanpa network eksternal: semua probe ke 127.0.0.1 port ephemeral.
"""

import asyncio
import json
import socket
import unittest

import aiohttp
import telegram

import main as main_mod


def _ephemeral_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeApplication:
    """Tiruan Application PTB — hanya bot + process_update yang dibutuhkan."""

    def __init__(self):
        self.bot = telegram.Bot(token="123456789:AAFAKE")
        self.processed = []

    async def process_update(self, update):
        self.processed.append(update)


class TestWebhookAiohttp(unittest.TestCase):
    def setUp(self):
        self.token = "123456789:AAFAKE"
        self.secret = "rahasia-webhook-123"
        self.port = _ephemeral_port()
        self.fake = FakeApplication()

    async def _serve(self, app):
        runner = aiohttp.web.AppRunner(app, access_log=None)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        return runner

    def _valid_payload(self):
        return json.dumps({
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 1, "type": "private"},
                "text": "halo",
            },
        }).encode()

    def test_health_returns_200_and_webhook_routes(self):
        async def main():
            app = main_mod.build_webhook_app(self.fake, self.token, self.secret)
            runner = await self._serve(app)
            base = f"http://127.0.0.1:{self.port}"
            res = {}
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"{base}/health", timeout=5) as r:
                        res["health"] = r.status
                        res["health_json"] = await r.json()

                    good = {"Content-Type": "application/json",
                            "X-Telegram-Bot-Api-Secret-Token": self.secret}
                    payload = self._valid_payload()

                    async with s.post(f"{base}/{self.token}", data=payload,
                                      headers=good, timeout=5) as r:
                        res["valid"] = r.status
                    async with s.post(f"{base}/{self.token}", data=payload,
                                      headers={**good, "X-Telegram-Bot-Api-Secret-Token": "salah"},
                                      timeout=5) as r:
                        res["wrong_secret"] = r.status
                    async with s.post(f"{base}/{self.token}", data=b"bukan-json",
                                      headers=good, timeout=5) as r:
                        res["bad_json"] = r.status
                    async with s.get(f"{base}/path-tidak-dikenal", timeout=5) as r:
                        res["unknown_path"] = r.status
            finally:
                await runner.cleanup()
            return res

        res = asyncio.run(main())
        self.assertEqual(res["health"], 200, "GET /health harus 200 untuk keep-alive")
        self.assertEqual(res["health_json"].get("status"), "ok")
        self.assertEqual(res["valid"], 200, "POST valid + secret benar harus 200")
        self.assertEqual(res["wrong_secret"], 403)
        self.assertEqual(res["bad_json"], 400)
        self.assertEqual(res["unknown_path"], 404)
        self.assertEqual(len(self.fake.processed), 1, "Update valid harus diproses")
        self.assertEqual(self.fake.processed[0].update_id, 1)

    def test_slow_update_returns_200_immediately(self):
        """Regresi: webhook harus balas 200 CEPAT walau pipeline lambat (>20s).

        Kalau handler meng-await process_update, respons tertahan → Telegram
        timeout & retry → update diproses dobel. Fire-and-forget wajib.
        """

        class SlowApplication(FakeApplication):
            async def process_update(self, update):
                self.processed.append(update)
                await asyncio.sleep(5)  # simulasi pipeline AI lambat

        async def main():
            fake = SlowApplication()
            app = main_mod.build_webhook_app(fake, self.token, self.secret)
            runner = await self._serve(app)
            base = f"http://127.0.0.1:{self.port}"
            try:
                import time
                t0 = time.monotonic()
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{base}/{self.token}",
                        data=self._valid_payload(),
                        headers={"Content-Type": "application/json",
                                 "X-Telegram-Bot-Api-Secret-Token": self.secret},
                        timeout=3,  # lebih pendek dari simulasi pipeline
                    ) as r:
                        status = r.status
                        elapsed = time.monotonic() - t0
                await asyncio.sleep(5.5)  # biarkan task background selesai
                return status, elapsed, len(fake.processed)
            finally:
                await runner.cleanup()

        status, elapsed, processed = asyncio.run(main())
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 3, "Respons harus cepat walau pipeline lambat")
        self.assertEqual(processed, 1)

    def test_no_secret_configured_accepts_request(self):
        """Saat secret None (tidak dikonfigurasi), request tanpa header diterima."""

        async def main():
            app = main_mod.build_webhook_app(self.fake, self.token, None)
            runner = await self._serve(app)
            base = f"http://127.0.0.1:{self.port}"
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{base}/{self.token}",
                        data=self._valid_payload(),
                        headers={"Content-Type": "application/json"},
                        timeout=5,
                    ) as r:
                        return r.status
            finally:
                await runner.cleanup()

        self.assertEqual(asyncio.run(main()), 200)


if __name__ == "__main__":
    unittest.main()
