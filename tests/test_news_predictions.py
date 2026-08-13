"""
Unit tests untuk fitur PREDIKSI NEWS (XAU/USD):
- NewsPredictionStore (memori): add/get/pending/settle/stats/recent
- _rule_based_gold_direction (fallback aturan saat AI tidak tersedia)
- _parse_ai_direction / _parse_ai_verdict (format satu kata dari AI)
- _compute_rule_result (evaluasi harga berbasis aturan)
- check_news_predictions (job T-5 menit → kirim prediksi ke subscriber)
- settle_news_predictions (job pasca rilis → AI menilai benar/salah/flat)
- prediksi_command (/prediksi → win rate & riwayat)
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from bot.handlers import MarketBot
from data.news_predictions import (
    NewsPredictionStore,
    STATUS_PENDING,
    STATUS_SETTLED,
)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _event(name="Non-Farm Payrolls (NFP)", minutes_to=4, impact="high",
           estimate=250.0, prev=240.0, unit="K", country="US"):
    # Handler memakai waktu NYATA (datetime.now) — event dibuat relatif ke sekarang
    return {
        "event": name,
        "country": country,
        "country_emoji": "🇺🇸",
        "time": "07 Agu 2026 19:30 WIB",
        "_dt_utc": datetime.now(timezone.utc) + timedelta(minutes=minutes_to),
        "impact": impact,
        "impact_label": "🔥 HIGH" if impact == "high" else "⚠️ MEDIUM",
        "actual": None,
        "estimate": estimate,
        "prev": prev,
        "unit": unit,
        "source": "fred",
    }


class FakeAI:
    def __init__(self, text):
        self.text = text

    def generate(self, *a, **k):
        return self.text


class FakeMarket:
    @staticmethod
    def get_yahoo_data(*args, **kwargs):
        return {"current_price": 2400.0, "change_pct": 0.1, "ohlcv": []}


class FakeMacro:
    def __init__(self, events):
        self.events = events

    async def get_economic_calendar(self, *a, **k):
        return self.events


class FakeNews:
    async def get_news_summary(self, symbol):
        return "Gold bergerak setelah rilis data."


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class FakeApplication:
    def __init__(self, subscribers=None):
        self.bot_data = {"event_alert_subscribers": set(subscribers or [])}
        self.bot = FakeBot()


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeUpdate:
    def __init__(self, text):
        self.message = FakeMessage(text)


class FakeContext:
    pass


class TestNewsPredictionStore(unittest.TestCase):

    def _store_with(self, records):
        store = NewsPredictionStore()
        store._loaded = True  # hermetic — tanpa akses Supabase
        for r in records:
            store._records[r["event_key"]] = r
        return store

    def test_add_and_get(self):
        store = NewsPredictionStore()
        store._loaded = True
        rec = store.add_prediction(
            event_key="CPI|2026-08-07T12:30:00+00:00",
            event_name="CPI / Inflasi AS (YoY)",
            event_time="07 Agu 2026 19:30 WIB",
            event_dt_utc=NOW + timedelta(minutes=5),
            direction="naik",
            price_at_prediction=2400.0,
            reasoning="Alasan.",
        )
        self.assertEqual(rec["status"], STATUS_PENDING)
        self.assertEqual(rec["direction"], "naik")
        self.assertIsNone(store.get_prediction("tidak-ada"))
        self.assertEqual(store.get_prediction(rec["event_key"])["event_name"], "CPI / Inflasi AS (YoY)")

    def test_add_invalid_direction_normalized(self):
        store = NewsPredictionStore()
        store._loaded = True
        rec = store.add_prediction(event_key="k1", event_name="E", direction="sideways")
        self.assertEqual(rec["direction"], "naik")

    def test_get_pending_window(self):
        store = NewsPredictionStore()
        store._loaded = True
        store.add_prediction(
            event_key="old", event_name="E1", direction="naik",
            event_dt_utc=NOW - timedelta(hours=1),
        )
        store.add_prediction(
            event_key="recent", event_name="E2", direction="turun",
            event_dt_utc=NOW - timedelta(minutes=5),
        )
        store.add_prediction(
            event_key="future", event_name="E3", direction="naik",
            event_dt_utc=NOW + timedelta(hours=1),
        )
        pending = store.get_pending(now_utc=NOW, settle_minutes=15)
        keys = {p["event_key"] for p in pending}
        self.assertIn("old", keys)
        self.assertNotIn("recent", keys)  # 5 menit < settle 15
        self.assertNotIn("future", keys)

    def test_settle_idempotent_and_updates(self):
        store = NewsPredictionStore()
        store._loaded = True
        store.add_prediction(
            event_key="k1", event_name="E1", direction="naik",
            price_at_prediction=2400.0,
        )
        updated = store.settle(
            event_key="k1", result="benar", actual_direction="naik",
            price_after=2410.0, move_pct=0.42, reasoning="sesuai",
        )
        self.assertEqual(updated["status"], STATUS_SETTLED)
        self.assertEqual(updated["result"], "benar")
        store.settle(
            event_key="k1", result="salah", actual_direction="turun",
            price_after=2390.0, move_pct=-0.5, reasoning="diubah",
        )
        # idempotent — hasil pertama tidak tertimpa
        self.assertEqual(store.get_prediction("k1")["result"], "benar")
        self.assertIsNone(store.settle(event_key="none", result="benar"))

    def test_stats_and_win_rate(self):
        store = NewsPredictionStore()
        store._loaded = True
        store.add_prediction(event_key="a", event_name="A", direction="naik")
        store.add_prediction(event_key="b", event_name="B", direction="turun")
        store.add_prediction(event_key="c", event_name="C", direction="naik")
        store.settle(event_key="a", result="benar")
        store.settle(event_key="b", result="salah")
        store.settle(event_key="c", result="flat")
        stats = store.get_stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["settled"], 3)
        self.assertEqual(stats["benar"], 1)
        self.assertEqual(stats["salah"], 1)
        self.assertEqual(stats["flat"], 1)
        self.assertEqual(stats["win_rate"], 50.0)

    def test_stats_no_decided_win_rate_none(self):
        store = NewsPredictionStore()
        store._loaded = True
        store.add_prediction(event_key="c", event_name="C", direction="naik")
        store.settle(event_key="c", result="flat")
        self.assertIsNone(store.get_stats()["win_rate"])

    def test_get_recent_ordering(self):
        store = NewsPredictionStore()
        store._loaded = True
        store._records["a"] = {"event_key": "a", "predicted_at": "2026-08-07T10:00:00+00:00"}
        store._records["b"] = {"event_key": "b", "predicted_at": "2026-08-07T11:00:00+00:00"}
        recent = store.get_recent(10)
        self.assertEqual(recent[0]["event_key"], "b")  # terbaru dulu

    def test_ensure_loaded_without_supabase(self):
        store = NewsPredictionStore()
        store.ensure_loaded()  # tidak boleh raise / menggantung tanpa Supabase
        self.assertTrue(store._loaded)


class TestRuleBasedDirection(unittest.TestCase):

    def test_cpi_up_means_gold_down(self):
        direction, _ = MarketBot._rule_based_gold_direction(_event("CPI / Inflasi AS (YoY)", estimate=3.2, prev=3.0))
        self.assertEqual(direction, "turun")

    def test_cpi_down_means_gold_up(self):
        direction, _ = MarketBot._rule_based_gold_direction(_event("CPI / Inflasi AS (YoY)", estimate=2.8, prev=3.0))
        self.assertEqual(direction, "naik")

    def test_nfp_strong_means_gold_down(self):
        direction, _ = MarketBot._rule_based_gold_direction(_event("Non-Farm Payrolls (NFP)", estimate=300.0, prev=240.0))
        self.assertEqual(direction, "turun")

    def test_gdp_weak_means_gold_up(self):
        direction, _ = MarketBot._rule_based_gold_direction(_event("GDP AS (QoQ)", estimate=1.5, prev=2.8))
        self.assertEqual(direction, "naik")

    def test_unemployment_high_means_gold_up(self):
        direction, _ = MarketBot._rule_based_gold_direction(_event("Unemployment Rate", estimate=4.5, prev=4.0))
        self.assertEqual(direction, "naik")

    def test_fomc_naik_default(self):
        direction, reason = MarketBot._rule_based_gold_direction(_event("Fed Funds Rate Decision (FOMC)", estimate=4.75, prev=4.75))
        self.assertEqual(direction, "naik")
        self.assertIn("Fed", reason)

    def test_missing_numbers_default_naik(self):
        direction, _ = MarketBot._rule_based_gold_direction(_event("Consumer Confidence", estimate=None, prev=None))
        self.assertEqual(direction, "naik")

    def test_default_up_when_forecast_below_prev(self):
        direction, _ = MarketBot._rule_based_gold_direction(_event("Industrial Production", estimate=0.1, prev=0.5))
        self.assertEqual(direction, "naik")


class TestParseAI(unittest.TestCase):

    def test_parse_direction(self):
        self.assertEqual(MarketBot._parse_ai_direction("naik\nkarena data lemah"), "naik")
        self.assertEqual(MarketBot._parse_ai_direction("turun."), "turun")
        self.assertEqual(MarketBot._parse_ai_direction("  Naik "), "naik")
        self.assertIsNone(MarketBot._parse_ai_direction("datar"))
        self.assertIsNone(MarketBot._parse_ai_direction(""))

    def test_parse_verdict(self):
        self.assertEqual(MarketBot._parse_ai_verdict("benar\nharga naik"), "benar")
        self.assertEqual(MarketBot._parse_ai_verdict("salah."), "salah")
        self.assertEqual(MarketBot._parse_ai_verdict("flat"), "flat")
        self.assertIsNone(MarketBot._parse_ai_verdict("mungkin"))


class TestComputeRuleResult(unittest.TestCase):

    def test_benar_naik(self):
        out = MarketBot._compute_rule_result("naik", 2400.0, 2410.0, 0.05)
        self.assertEqual(out["result"], "benar")
        self.assertEqual(out["actual_direction"], "naik")

    def test_salah_naik(self):
        out = MarketBot._compute_rule_result("naik", 2400.0, 2390.0, 0.05)
        self.assertEqual(out["result"], "salah")

    def test_benar_turun(self):
        out = MarketBot._compute_rule_result("turun", 2400.0, 2390.0, 0.05)
        self.assertEqual(out["result"], "benar")

    def test_flat_below_threshold(self):
        out = MarketBot._compute_rule_result("naik", 2400.0, 2400.3, 0.05)
        self.assertEqual(out["result"], "flat")
        self.assertEqual(out["actual_direction"], "flat")

    def test_none_when_price_missing(self):
        self.assertIsNone(MarketBot._compute_rule_result("naik", None, 2410.0, 0.05))
        self.assertIsNone(MarketBot._compute_rule_result("naik", 2400.0, None, 0.05))
        self.assertIsNone(MarketBot._compute_rule_result("naik", 0.0, 2410.0, 0.05))


class TestAftermathPredictionSection(unittest.TestCase):
    """Section 🎯 Prediksi Bot di pesan aftermath (integrasi /prediksi → aftermath)."""

    def _bot_with_store(self):
        bot = MarketBot.__new__(MarketBot)
        bot.ai = None  # tanpa network — fallback interpretasi statis
        bot.news_preds = NewsPredictionStore()
        bot.news_preds._loaded = True
        return bot

    def test_section_shows_verdict_when_settled(self):
        bot = self._bot_with_store()
        event = _event("Non-Farm Payrolls (NFP)", minutes_to=1)
        key = MarketBot._aftermath_key(event)
        bot.news_preds.add_prediction(
            event_key=key, event_name=event["event"], direction="naik",
            price_at_prediction=2400.0, event_dt_utc=event["_dt_utc"],
        )
        bot.news_preds.settle(
            event_key=key, result="benar", actual_direction="naik",
            price_after=2410.0, move_pct=0.42, reasoning="sesuai",
        )
        msg = asyncio.run(bot._build_aftermath_message(event, "DXY: 104.2", manual=True))
        self.assertIn("Prediksi Bot", msg)
        self.assertIn("benar", msg)
        self.assertIn("+0.42%", msg)

    def test_section_shows_pending_when_not_settled(self):
        bot = self._bot_with_store()
        event = _event()
        key = MarketBot._aftermath_key(event)
        bot.news_preds.add_prediction(
            event_key=key, event_name=event["event"], direction="turun",
            price_at_prediction=2400.0, event_dt_utc=event["_dt_utc"],
        )
        msg = asyncio.run(bot._build_aftermath_message(event, "DXY: 104.2", manual=True))
        self.assertIn("Prediksi Bot", msg)
        self.assertIn("belum dievaluasi", msg)

    def test_no_section_without_record(self):
        bot = self._bot_with_store()
        msg = asyncio.run(
            bot._build_aftermath_message(_event("CPI / Inflasi AS (YoY)"), "DXY: 104.2", manual=True)
        )
        self.assertNotIn("Prediksi Bot", msg)

    def test_no_crash_without_store_attr(self):
        # Pola test lama: bot tanpa news_preds — aftermath tetap jalan
        bot = MarketBot.__new__(MarketBot)
        bot.ai = None
        msg = asyncio.run(bot._build_aftermath_message(_event(), "DXY: 104.2", manual=True))
        self.assertNotIn("Prediksi Bot", msg)
        self.assertIn("ANALISIS DAMPAK", msg)


class TestNewsPredictionFlows(unittest.TestCase):

    def _bot(self, ai_text="naik\nEmas naik karena data lemah."):
        bot = MarketBot.__new__(MarketBot)
        bot.ai = FakeAI(ai_text)
        bot.market = FakeMarket()
        bot.macro = FakeMacro([_event(minutes_to=3)])
        bot.news = FakeNews()
        bot.news_preds = NewsPredictionStore()
        bot.news_preds._loaded = True
        return bot

    def test_check_news_predictions_creates_and_sends(self):
        bot = self._bot()
        app = FakeApplication(subscribers=[111, 222])
        asyncio.run(bot.check_news_predictions(app))

        recs = bot.news_preds.get_recent(10)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["direction"], "naik")
        self.assertEqual(rec["status"], STATUS_PENDING)
        self.assertEqual(rec["price_at_prediction"], 2400.0)
        self.assertEqual(len(app.bot.sent), 2)  # ke 2 subscriber
        self.assertIn("PREDIKSI NEWS", app.bot.sent[0]["text"])
        self.assertIn("naik", app.bot.sent[0]["text"])

    def test_check_news_predictions_skips_when_no_gold_price(self):
        # Tanpa harga acuan prediksi tidak bisa dievaluasi nanti → lewati
        # (dicek ulang di run berikutnya — tidak stuck pending selamanya).
        bot = self._bot()
        bot.market = type(
            "M", (), {"get_yahoo_data": staticmethod(lambda *a, **k: {"error": "no data"})}
        )()
        app = FakeApplication(subscribers=[111])
        asyncio.run(bot.check_news_predictions(app))
        self.assertEqual(bot.news_preds.get_stats()["total"], 0)
        self.assertEqual(app.bot.sent, [])

    def test_check_news_predictions_skips_event_outside_window(self):
        bot = self._bot()
        bot.macro = FakeMacro([_event(minutes_to=120)])  # 2 jam lagi — bukan T-5
        app = FakeApplication(subscribers=[111])
        asyncio.run(bot.check_news_predictions(app))
        self.assertEqual(len(bot.news_preds.get_recent(10)), 0)
        self.assertEqual(app.bot.sent, [])

    def test_check_news_predictions_dedup(self):
        bot = self._bot()
        app = FakeApplication(subscribers=[111])
        asyncio.run(bot.check_news_predictions(app))
        first = bot.news_preds.get_stats()["total"]
        asyncio.run(bot.check_news_predictions(app))  # run kedua — dedup
        self.assertEqual(bot.news_preds.get_stats()["total"], first)
        self.assertEqual(len(app.bot.sent), 1)

    def test_settle_evaluates_with_ai_verdict(self):
        bot = self._bot(ai_text="salah\nHarga justru turun setelah rilis.")
        # Prediksi 1 jam lalu — sudah lewat jendela settle 15 menit
        bot.news_preds.add_prediction(
            event_key="CPI|2026-08-07T11:00:00+00:00",
            event_name="CPI / Inflasi AS (YoY)",
            event_dt_utc=NOW - timedelta(hours=1),
            direction="naik",
            price_at_prediction=2350.0,
            reasoning="alasan",
        )
        app = FakeApplication(subscribers=[111])
        asyncio.run(bot.settle_news_predictions(app))

        rec = bot.news_preds.get_prediction("CPI|2026-08-07T11:00:00+00:00")
        self.assertEqual(rec["status"], STATUS_SETTLED)
        self.assertEqual(rec["result"], "salah")  # AI menilai (menimpa aturan benar)
        self.assertIsNotNone(rec["move_pct"])
        self.assertEqual(len(app.bot.sent), 1)
        self.assertIn("PREDIKSI SALAH", app.bot.sent[0]["text"])

    def test_settle_skips_when_no_price(self):
        bot = self._bot()
        bot.market = type("M", (), {"get_yahoo_data": staticmethod(lambda *a, **k: {"error": "no data"})})()
        bot.news_preds.add_prediction(
            event_key="k1", event_name="E1", event_dt_utc=NOW - timedelta(hours=1),
            direction="naik", price_at_prediction=2350.0,
        )
        app = FakeApplication(subscribers=[111])
        asyncio.run(bot.settle_news_predictions(app))
        rec = bot.news_preds.get_prediction("k1")
        self.assertEqual(rec["status"], STATUS_PENDING)  # tetap pending — retry
        self.assertEqual(app.bot.sent, [])

    def test_prediksi_command_empty_state(self):
        bot = self._bot()
        upd = FakeUpdate("/prediksi")
        asyncio.run(bot.prediksi_command(upd, FakeContext()))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("PREDIKSI NEWS", joined)
        self.assertIn("Belum ada prediksi", joined)

    def test_prediksi_command_shows_stats(self):
        bot = self._bot()
        bot.news_preds.add_prediction(
            event_key="a", event_name="CPI / Inflasi AS (YoY)", direction="naik",
            price_at_prediction=2400.0,
        )
        bot.news_preds.settle(
            event_key="a", result="benar", actual_direction="naik",
            price_after=2410.0, move_pct=0.42, reasoning="sesuai",
        )
        bot.news_preds.add_prediction(
            event_key="b", event_name="Fed Funds Rate Decision (FOMC)", direction="turun",
        )
        upd = FakeUpdate("/prediksi")
        asyncio.run(bot.prediksi_command(upd, FakeContext()))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("WIN RATE", joined)
        self.assertIn("*1*", joined)  # total
        self.assertIn("✅ Benar: *1*", joined)
        self.assertIn("CPI / Inflasi AS", joined)

    def test_prediksi_command_help(self):
        bot = self._bot()
        upd = FakeUpdate("/prediksi help")
        asyncio.run(bot.prediksi_command(upd, FakeContext()))
        joined = "\n".join(t for t, _ in upd.message.replies)
        self.assertIn("/prediksi history", joined)


if __name__ == "__main__":
    unittest.main()
