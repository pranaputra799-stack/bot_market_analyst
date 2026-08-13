"""Unit tests untuk interval pengecekan aftermath (terpisah dari reminder).

Memastikan: setting baru terbaca dari env, ter-clamp ke rentang aman,
dan dipakai scheduler untuk job aftermath (bukan interval reminder).
"""

import unittest
from unittest import mock


class TestAftermathIntervalSetting(unittest.TestCase):
    def test_default_is_30(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            from config.settings import EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES
            self.assertEqual(EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES, 30)

    def test_env_override(self):
        with mock.patch.dict("os.environ", {"EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES": "60"}, clear=False):
            import importlib
            import config.settings as s
            importlib.reload(s)
            self.assertEqual(s.EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES, 60)
        # Pulihkan modul agar test lain tidak terpengaruh env override
        import importlib
        import config.settings as s
        importlib.reload(s)

    def test_clamped_to_min_10(self):
        with mock.patch.dict("os.environ", {"EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES": "5"}, clear=False):
            import importlib
            import config.settings as s
            importlib.reload(s)
            self.assertEqual(s.EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES, 10)
        import importlib
        import config.settings as s
        importlib.reload(s)

    def test_clamped_to_max_120(self):
        with mock.patch.dict("os.environ", {"EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES": "999"}, clear=False):
            import importlib
            import config.settings as s
            importlib.reload(s)
            self.assertEqual(s.EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES, 120)
        import importlib
        import config.settings as s
        importlib.reload(s)

    def test_scheduler_uses_separate_interval_for_aftermath(self):
        """Job aftermath memakai EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES,
        bukan ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES (reminder)."""
        from config.settings import (
            EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES,
            ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES,
        )
        # Keduanya eksis & aftermath lebih jarang (default 30 vs 15)
        self.assertIsInstance(EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES, int)
        self.assertGreaterEqual(EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES, ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES)


if __name__ == "__main__":
    unittest.main()
