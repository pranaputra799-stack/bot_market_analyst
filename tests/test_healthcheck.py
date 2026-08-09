"""Unit tests untuk Docker healthcheck (utils/healthcheck.py).

Menguji:
- health_ok(): cek HTTP 200 ke /health (dengan mock urlopen).
- Normalisasi host: bind 0.0.0.0 → konek ke 127.0.0.1.
- bot_process_alive(): deteksi proses "main.py" lewat /proc/<pid>/cmdline
  (diuji dengan direktori temp, bukan /proc asli).
- check()/main(): prioritas /health → fallback proses → unhealthy.
- E2E: server /health nyata (port ephemeral) → health_ok() True, berhenti bersih.
"""

import os
import socket
import tempfile
import unittest
from unittest import mock

import utils.healthcheck as hc
from utils.health_server import start_health_server, stop_health_server


class _FakeResp:
    """Objek respons urlopen minimal (context manager + .status)."""

    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestHealthOk(unittest.TestCase):
    def test_success(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(200)):
            self.assertTrue(hc.health_ok("127.0.0.1", 8090))

    def test_non_200(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(500)):
            self.assertFalse(hc.health_ok("127.0.0.1", 8090))

    def test_connection_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("conn refused")):
            self.assertFalse(hc.health_ok("127.0.0.1", 8090))

    def test_bind_0000_normalized_to_localhost(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(200)) as m:
            self.assertTrue(hc.health_ok("0.0.0.0", 8090))
        self.assertIn("127.0.0.1:8090/health", m.call_args.args[0])

    def test_check_host(self):
        self.assertEqual(hc._check_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(hc._check_host("::"), "127.0.0.1")
        self.assertEqual(hc._check_host("127.0.0.1"), "127.0.0.1")


class TestBotProcessAlive(unittest.TestCase):
    def test_finds_main_py_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_dir = os.path.join(tmp, "1234")
            os.makedirs(pid_dir)
            with open(os.path.join(pid_dir, "cmdline"), "wb") as f:
                f.write(b"python\x00main.py\x00")
            self.assertTrue(hc.bot_process_alive(proc_root=tmp))

    def test_no_main_py_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_dir = os.path.join(tmp, "9999")
            os.makedirs(pid_dir)
            with open(os.path.join(pid_dir, "cmdline"), "wb") as f:
                f.write(b"sleep\x00100\x00")
            self.assertFalse(hc.bot_process_alive(proc_root=tmp))

    def test_empty_root_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(hc.bot_process_alive(proc_root=tmp))


class TestCheckAndMain(unittest.TestCase):
    def test_health_ok_short_circuits(self):
        with mock.patch.object(hc, "health_ok", return_value=True) as h, \
                mock.patch.object(hc, "bot_process_alive") as p:
            self.assertTrue(hc.check())
        h.assert_called_once()
        p.assert_not_called()

    def test_health_down_process_alive_fallback(self):
        with mock.patch.object(hc, "health_ok", return_value=False), \
                mock.patch.object(hc, "bot_process_alive", return_value=True):
            self.assertTrue(hc.check())

    def test_both_down_unhealthy(self):
        with mock.patch.object(hc, "health_ok", return_value=False), \
                mock.patch.object(hc, "bot_process_alive", return_value=False):
            self.assertFalse(hc.check())

    def test_main_exit_codes(self):
        with mock.patch.object(hc, "health_ok", return_value=True):
            self.assertEqual(hc.main(), 0)
        with mock.patch.object(hc, "health_ok", return_value=False), \
                mock.patch.object(hc, "bot_process_alive", return_value=False):
            self.assertEqual(hc.main(), 1)


class TestEndToEndWithRealServer(unittest.TestCase):
    """health_ok() terhadap server /health NYATA (port ephemeral)."""

    def test_real_server_responds_ok(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        thread = start_health_server(port=port, enabled=True)
        self.assertIsNotNone(thread)
        try:
            self.assertTrue(hc.health_ok("127.0.0.1", port, timeout=5))
        finally:
            stop_health_server(thread)


if __name__ == "__main__":
    unittest.main()
