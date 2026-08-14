"""Unit tests untuk sesi market (utils/sessions.py)."""

import unittest
from datetime import datetime, timezone

from utils.sessions import sessions_just_opened, SESSIONS, format_session_text


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class TestSessionsJustOpened(unittest.TestCase):
    def test_tokyo_opens_at_midnight(self):
        opened = sessions_just_opened(_utc(2026, 8, 14, 0, 10))
        self.assertEqual([s.key for s in opened], ["TOKYO"])

    def test_sydney_opens_at_22_utc(self):
        opened = sessions_just_opened(_utc(2026, 8, 14, 22, 10))
        self.assertEqual([s.key for s in opened], ["SYDNEY"])

    def test_london_opens_at_08_utc(self):
        opened = sessions_just_opened(_utc(2026, 8, 14, 8, 15))
        self.assertEqual([s.key for s in opened], ["LONDON"])

    def test_new_york_opens_at_13_utc(self):
        opened = sessions_just_opened(_utc(2026, 8, 14, 13, 20))
        self.assertEqual([s.key for s in opened], ["NEW_YORK"])

    def test_none_outside_window(self):
        self.assertEqual(sessions_just_opened(_utc(2026, 8, 14, 12, 0)), [])
        # 45 menit setelah buka → bukan "baru buka" lagi
        self.assertEqual(sessions_just_opened(_utc(2026, 8, 14, 8, 45)), [])

    def test_window_edges(self):
        # Tepat di jam buka → masuk jendela
        self.assertIn("LONDON", [s.key for s in sessions_just_opened(_utc(2026, 8, 14, 8, 0))])
        # 30 menit setelah buka → masih dalam jendela (buka + <30mnt)
        self.assertIn("LONDON", [s.key for s in sessions_just_opened(_utc(2026, 8, 14, 8, 29))])

    def test_all_sessions_defined(self):
        self.assertEqual(len(SESSIONS), 4)
        self.assertEqual({s.key for s in SESSIONS}, {"SYDNEY", "TOKYO", "LONDON", "NEW_YORK"})


class TestFormatSessionText(unittest.TestCase):
    def test_contains_name_and_disclaimer(self):
        sydney = next(s for s in SESSIONS if s.key == "SYDNEY")
        text = format_session_text(sydney)
        self.assertIn("SYDNEY", text.upper())
        self.assertIn("bukan saran trading", text)


if __name__ == "__main__":
    unittest.main()
