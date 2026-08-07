"""
Unit tests untuk fitur notifikasi aftermath event high-impact (tanpa network).

Mencakup:
- _collect_aftermath_events: seleksi jendela waktu, filter high-impact, urutan
- _static_event_interpretation: arah DXY berbasis aturan (CPI, NFP, FOMC, non-AS)
- _format_event_numbers / _fmt_ev_value: format angka Actual/Forecast/Previous
- _aftermath_key: stabilitas kunci dedup
"""
import unittest
from datetime import datetime, timedelta, timezone

from bot.handlers import MarketBot

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _event(name="CPI / Inflasi AS (YoY)", hours_ago=2, impact="high",
           actual=2.9, estimate=3.0, prev=3.2, unit="%", country="US"):
    return {
        "event": name,
        "country": country,
        "country_emoji": "🇺🇸" if country == "US" else "🌍",
        "time": "07 Agu 2026 19:30 WIB",
        "_dt_utc": NOW - timedelta(hours=hours_ago),
        "impact": impact,
        "impact_label": "🔥 HIGH" if impact == "high" else "⚠️ MEDIUM",
        "actual": actual,
        "estimate": estimate,
        "prev": prev,
        "unit": unit,
        "source": "fred",
    }


class TestCollectAftermathEvents(unittest.TestCase):

    def test_selects_recent_high_impact_released(self):
        events = [
            _event(hours_ago=1),                          # dalam jendela
            _event(name="GDP AS (QoQ)", hours_ago=3),     # dalam jendela
            _event(name="CPI", hours_ago=10),             # di luar lookback
            _event(name="Initial Jobless Claims (US)", hours_ago=1, impact="medium"),
            _event(name="FOMC", hours_ago=-1),            # belum rilis (di masa depan)
        ]
        out = MarketBot._collect_aftermath_events(events, NOW, 6)
        names = [e["event"] for e in out]
        # Terurut dari paling baru (CPI 1 jam lalu, GDP 3 jam lalu)
        self.assertEqual(names, ["CPI / Inflasi AS (YoY)", "GDP AS (QoQ)"])

    def test_empty_input(self):
        self.assertEqual(MarketBot._collect_aftermath_events([], NOW, 6), [])
        self.assertEqual(MarketBot._collect_aftermath_events(None, NOW, 6), [])

    def test_skips_events_without_valid_dt(self):
        bad = dict(_event(hours_ago=1))
        bad["_dt_utc"] = None
        naive = dict(_event(hours_ago=1))
        naive["_dt_utc"] = datetime(2026, 8, 7, 11, 0)  # naive datetime (tanpa tzinfo)
        out = MarketBot._collect_aftermath_events([bad, naive], NOW, 6)
        self.assertEqual(out, [])

    def test_boundary_exact_cutoff(self):
        # Tepat di batas jendela (6 jam lalu) → tetap masuk
        e = _event(hours_ago=6)
        self.assertEqual(len(MarketBot._collect_aftermath_events([e], NOW, 6)), 1)


class TestStaticEventInterpretation(unittest.TestCase):

    def test_cpi_above_forecast_usd_bullish(self):
        out = MarketBot._static_event_interpretation(_event(actual=3.1, estimate=3.0))
        self.assertIn("DI ATAS ekspektasi", out)
        self.assertIn("NAIK", out)

    def test_cpi_below_forecast_usd_bearish(self):
        out = MarketBot._static_event_interpretation(_event(actual=2.8, estimate=3.0))
        self.assertIn("DI BAWAH ekspektasi", out)
        self.assertIn("TURUN", out)

    def test_exact_match_is_neutral_not_bearish(self):
        # Actual PERSIS sama dengan forecast — bukan "lebih rendah dari ekspektasi"
        out = MarketBot._static_event_interpretation(_event(actual=3.0, estimate=3.0))
        self.assertIn("sesuai ekspektasi", out)
        self.assertNotIn("lebih rendah", out)
        self.assertNotIn("TURUN", out)

    def test_nfp_strong_usd_bullish(self):
        e = _event(name="Non-Farm Payrolls (NFP) & Unemployment Rate",
                   actual=250, estimate=180, unit="K")
        out = MarketBot._static_event_interpretation(e)
        self.assertIn("NAIK", out)

    def test_unemployment_lower_is_bullish(self):
        # Pengangguran LEBIH RENDAH dari ekspektasi = kuat (inverse)
        e = _event(name="Unemployment Rate", actual=3.7, estimate=3.9, unit="%")
        out = MarketBot._static_event_interpretation(e)
        self.assertIn("NAIK", out)

    def test_fomc_hike_usd_bullish(self):
        e = _event(name="Fed Funds Rate Decision (FOMC)", actual=4.75, estimate=4.75, prev=4.50, unit="%")
        out = MarketBot._static_event_interpretation(e)
        self.assertIn("NAIK", out)

    def test_non_us_event_via_pair(self):
        e = _event(name="CPI Eurozone (YoY)", actual=3.0, estimate=2.8, unit="%", country="EU")
        out = MarketBot._static_event_interpretation(e)
        self.assertIn("TURUN", out)  # data EU kuat → EUR kuat → DXY turun

    def test_missing_actual(self):
        e = _event(actual=None)
        out = MarketBot._static_event_interpretation(e)
        self.assertIn("belum tersedia", out)


class TestFormatting(unittest.TestCase):

    def test_fmt_value(self):
        self.assertEqual(MarketBot._fmt_ev_value(None), "—")
        self.assertEqual(MarketBot._fmt_ev_value(""), "—")
        self.assertEqual(MarketBot._fmt_ev_value(2.9), "2.9")
        self.assertEqual(MarketBot._fmt_ev_value(250.0), "250")
        self.assertEqual(MarketBot._fmt_ev_value("2.9"), "2.9")

    def test_format_event_numbers(self):
        line = MarketBot._format_event_numbers(_event(actual=2.9, estimate=3.0, prev=3.2, unit="%"))
        self.assertIn("Actual", line)
        self.assertIn("2.9%", line)
        self.assertIn("3.0%", line)
        self.assertIn("3.2%", line)

    def test_format_event_numbers_nfp_large_number(self):
        line = MarketBot._format_event_numbers(
            _event(name="Non-Farm Payrolls (NFP)", actual=250.0, estimate=180.0, prev=160.0, unit="K")
        )
        self.assertIn("250K", line)
        self.assertNotIn("250.0", line)

    def test_aftermath_key_stable(self):
        e = _event()
        k1 = MarketBot._aftermath_key(e)
        k2 = MarketBot._aftermath_key(dict(e))  # salinan dengan nilai sama
        self.assertEqual(k1, k2)
        self.assertIn("2026-08-07", k1)


if __name__ == "__main__":
    unittest.main()
