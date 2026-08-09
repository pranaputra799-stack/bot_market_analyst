"""Unit tests untuk health endpoint — payload JSON & lifecycle server (aiohttp).

Payload diuji murni (tanpa server). Lifecycle diuji dengan port ephemeral (0)
agar tidak bentrok; server dihentikan bersih lewat stop_health_server().
"""

import unittest

from utils.health_server import (
    build_health_payload,
    start_health_server,
    stop_health_server,
)


class TestHealthPayload(unittest.TestCase):
    def test_payload_shape(self):
        payload = build_health_payload()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "market-ai-bot")
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertIn("T", payload["timestamp"])

    def test_payload_has_subsections(self):
        payload = build_health_payload()
        self.assertIn("cache", payload)
        self.assertIn("database", payload)
        self.assertIn("ai", payload)
        self.assertIn("total_entries", payload["cache"])
        # metrics singleton selalu punya nilai default
        self.assertIn("total_analyses", payload["ai"])
        self.assertIn("cache_hits", payload["ai"])

    def test_payload_is_json_serializable(self):
        import json

        payload = build_health_payload()
        # Tidak boleh ada objek non-serializable (datetime, set, dll)
        json.dumps(payload)


class TestHealthServerLifecycle(unittest.TestCase):
    def test_disabled_returns_none(self):
        self.assertIsNone(start_health_server(enabled=False))

    def test_start_and_stop_cleanly(self):
        thread = start_health_server(port=0, enabled=True)  # port ephemeral
        self.assertIsNotNone(thread, "Server harus start di port ephemeral")
        self.assertTrue(thread.is_alive(), "Thread health server harus berjalan")
        stop_health_server(thread)
        self.assertFalse(thread.is_alive(), "Server harus berhenti bersih")

    def test_stop_on_unknown_thread_is_noop(self):
        # Tidak boleh crash bila dipanggil dengan thread non-health-server
        import threading

        dummy = threading.Thread(target=lambda: None, daemon=True)
        stop_health_server(dummy)  # tidak raise


if __name__ == "__main__":
    unittest.main()
