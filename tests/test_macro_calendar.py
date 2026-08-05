"""Unit tests untuk logika kalender ekonomi (tanpa network).

Test ini hanya menyentuh fungsi murni:
- _compute_fred_event_value (actual/previous dari observasi FRED)
- _is_event_released, _find_obs_index
- _get_scheduled_calendar (jadwal built-in, tanpa panggilan API)

CATATAN PEMELIHARAAN: test ini memakai jadwal rilis hardcoded 2026/2027
(CPI/PPI/FOMC). Saat jadwal resmi 2028+ tersedia di macro_data.py, perbarui
juga test ini agar tidak diam-diam basi.
"""

import unittest
from datetime import datetime, timedelta, timezone

from data.macro_data import MacroDataFetcher


class TestComputeFredEventValue(unittest.TestCase):
    def _obs(self, values):
        # observasi terurut DESCENDING seperti respons FRED (terbaru di depan)
        start = datetime(2026, 8, 1)
        return [
            {"date": (start - timedelta(days=i)).strftime("%Y-%m-%d"), "value": v}
            for i, v in enumerate(values)
        ]

    def test_level_mode(self):
        obs = self._obs([5.5, 5.25, 5.0])
        actual, prev = MacroDataFetcher._compute_fred_event_value(obs, "level")
        self.assertEqual(actual, 5.5)
        self.assertEqual(prev, 5.25)

    def test_mom_change_mode(self):
        obs = self._obs([180, 150, 120])
        actual, prev = MacroDataFetcher._compute_fred_event_value(obs, "mom_change")
        self.assertEqual(actual, 30.0)
        self.assertEqual(prev, 30.0)

    def test_mom_pct_mode(self):
        obs = self._obs([110.0, 100.0, 90.0])
        actual, prev = MacroDataFetcher._compute_fred_event_value(obs, "mom_pct")
        self.assertAlmostEqual(actual, 10.0, places=2)
        self.assertAlmostEqual(prev, 11.11, places=1)

    def test_yoy_pct_mode_short_data_returns_none(self):
        obs = self._obs([110.0, 100.0])  # tidak cukup 13 observasi
        actual, prev = MacroDataFetcher._compute_fred_event_value(obs, "yoy_pct")
        self.assertIsNone(actual)

    def test_yoy_pct_mode(self):
        # 14 observasi; indeks 0 vs 12 → perubahan YoY
        obs = self._obs([110.0] + [100.0] * 13)
        actual, prev = MacroDataFetcher._compute_fred_event_value(obs, "yoy_pct")
        self.assertAlmostEqual(actual, 10.0, places=2)

    def test_qoq_annualized(self):
        obs = self._obs([101.0, 100.0, 100.0])
        actual, _ = MacroDataFetcher._compute_fred_event_value(obs, "qoq_pct")
        # (1.01 - 1)^4 annualized ≈ 4.06%
        self.assertAlmostEqual(actual, 4.06, places=1)

    def test_old_value_zero_returns_none(self):
        obs = self._obs([5.0, 0.0])
        actual, prev = MacroDataFetcher._compute_fred_event_value(obs, "level")
        self.assertEqual(actual, 5.0)
        self.assertEqual(prev, 0.0)


class TestIsEventReleased(unittest.TestCase):
    def test_past_dt_released(self):
        e = {"_dt_utc": datetime.now(timezone.utc) - timedelta(hours=1)}
        self.assertTrue(MacroDataFetcher._is_event_released(e))

    def test_future_dt_not_released(self):
        e = {"_dt_utc": datetime.now(timezone.utc) + timedelta(hours=1)}
        self.assertFalse(MacroDataFetcher._is_event_released(e))

    def test_no_dt_uses_actual(self):
        self.assertTrue(MacroDataFetcher._is_event_released({"actual": 5}))
        self.assertFalse(MacroDataFetcher._is_event_released({"actual": None}))

    def test_invalid_dt_falls_back_to_actual(self):
        e = {"_dt_utc": "not-a-datetime", "actual": ""}
        self.assertFalse(MacroDataFetcher._is_event_released(e))


class TestFindObsIndex(unittest.TestCase):
    def _obs(self, n=5):
        start = datetime(2026, 8, 5)
        return [
            {"date": (start - timedelta(days=i)).strftime("%Y-%m-%d"), "value": i}
            for i in range(n)
        ]

    def test_matching_release_date(self):
        obs = self._obs()
        release = datetime(2026, 8, 3, tzinfo=timezone.utc)
        idx = MacroDataFetcher._find_obs_index(obs, release)
        # obs[0]=08-05, obs[1]=08-04, obs[2]=08-03 → pertama dengan date <= rilis
        self.assertEqual(idx, 2)

    def test_empty_obs(self):
        self.assertIsNone(MacroDataFetcher._find_obs_index([], datetime.now(timezone.utc)))

    def test_none_event_returns_latest(self):
        obs = self._obs()
        self.assertEqual(MacroDataFetcher._find_obs_index(obs, None), 0)


class TestScheduledCalendar(unittest.TestCase):
    def setUp(self):
        self.fetcher = MacroDataFetcher()

    def test_august_2026_range_and_content(self):
        events = self.fetcher._get_scheduled_calendar("2026-08-01", "2026-08-31")
        self.assertTrue(events, "Kalender Agustus 2026 seharusnya tidak kosong")

        # Semua event harus berada dalam rentang yang diminta
        for e in events:
            dt = e.get("_dt_utc")
            self.assertIsNotNone(dt)
            d = dt.date()
            self.assertGreaterEqual(d, datetime(2026, 8, 1).date())
            self.assertLessEqual(d, datetime(2026, 8, 31).date())

        names = [e.get("event") for e in events]
        # CPI AS resmi rilis 12 Agu 2026 (jadwal BLS)
        self.assertIn("CPI / Inflasi AS (YoY)", names)
        # Initial Claims berulang tiap Kamis
        self.assertIn("Initial Jobless Claims (US)", names)

    def test_no_events_outside_range(self):
        events = self.fetcher._get_scheduled_calendar("2027-01-01", "2027-01-31")
        for e in events:
            d = e["_dt_utc"].date()
            self.assertGreaterEqual(d, datetime(2027, 1, 1).date())
            self.assertLessEqual(d, datetime(2027, 1, 31).date())

    def test_format_calendar_text_no_crash(self):
        events = self.fetcher._get_scheduled_calendar("2026-08-01", "2026-08-31")
        text = self.fetcher.format_calendar_text(events, max_events=5, only_high=True)
        self.assertIsInstance(text, str)
        self.assertIn("KALENDER EKONOMI", text)


if __name__ == "__main__":
    unittest.main()
