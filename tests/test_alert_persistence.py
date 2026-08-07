"""Unit tests untuk persistensi alert ke Supabase — TANPA network.

DB di-mock sehingga tidak ada request HTTP keluar. Verifikasi bahwa:
- /pa clear/del/add menulis daftar alert terbaru ke database
- /alert on/off & tombol menu menulis subscriber terbaru ke database
- job scheduler (check_price_alerts) menyimpan daftar tersisa setelah terpicu
- helper best-effort tidak crash walau DB gagal / tidak terkonfigurasi
"""

import asyncio
import unittest
from unittest import mock

from bot.handlers import MarketBot
from data.database import db
from tests.test_handlers_utils import _FakeUpdate, _FakeContext


class _FakeApplication:
    def __init__(self):
        self.bot_data = {}


def _alert(alert_id, user_id, symbol="EURUSD=X", display_name="EUR/USD",
           target=1.10, direction="above", chat_id=777):
    return {
        "id": alert_id, "chat_id": chat_id, "user_id": user_id,
        "symbol": symbol, "display_name": display_name,
        "target": target, "direction": direction,
    }


class TestPriceAlertCommandPersistence(unittest.TestCase):
    """/pa clear & /pa del menulis daftar yang tersisa ke DB."""

    def test_clear_persists_remaining(self):
        ctx = _FakeContext()
        ctx.bot_data["price_alerts"] = [
            _alert(1, 9999),
            _alert(2, 8888, symbol="GC=F", display_name="XAU/USD (Gold)",
                   target=2300.0, direction="below", chat_id=888),
        ]
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/pa clear", user_id=9999)
        with mock.patch.object(db, "save_price_alerts_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.price_alert_command(upd, ctx))
        m.assert_awaited_once()
        saved = m.await_args.args[0]
        self.assertEqual([a["id"] for a in saved], [2])  # alert user lain tetap

    def test_del_persists_remaining(self):
        ctx = _FakeContext()
        ctx.bot_data["price_alerts"] = [_alert(1, 9999), _alert(2, 8888)]
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/pa del 1", user_id=9999)
        with mock.patch.object(db, "save_price_alerts_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.price_alert_command(upd, ctx))
        m.assert_awaited_once()
        saved = m.await_args.args[0]
        self.assertEqual([a["id"] for a in saved], [2])

    def test_new_alert_id_derived_from_max(self):
        """Id baru = max(id) + 1 (bukan counter RAM) agar aman setelah restart."""
        ctx = _FakeContext()
        ctx.bot_data["price_alerts"] = [_alert(7, 9999)]
        bot = MarketBot.__new__(MarketBot)
        bot.market = type("M", (), {})()
        # Add membutuhkan harga → mock fetch data (sync, dipanggil via to_thread)
        with mock.patch.object(bot.market, "get_yahoo_data", create=True,
                               return_value={"current_price": 1.08}), \
             mock.patch.object(db, "save_price_alerts_async",
                               new_callable=mock.AsyncMock) as m:
            upd = _FakeUpdate("/pa eurusd 1.10", user_id=9999)
            asyncio.run(bot.price_alert_command(upd, ctx))
        saved = m.await_args.args[0]
        new_ids = [a["id"] for a in saved]
        self.assertIn(8, new_ids)

    def test_list_does_not_write(self):
        ctx = _FakeContext()
        ctx.bot_data["price_alerts"] = [_alert(1, 9999)]
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/pa list", user_id=9999)
        with mock.patch.object(db, "save_price_alerts_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.price_alert_command(upd, ctx))
        m.assert_not_awaited()


class TestEventSubscriberPersistence(unittest.TestCase):
    """/alert on/off & tombol menu menulis subscriber terbaru ke DB."""

    def test_alert_on_persists(self):
        ctx = _FakeContext()
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/alert", user_id=9999)  # tanpa arg → on
        with mock.patch.object(db, "save_event_alert_subscribers_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.alert_command(upd, ctx))
        m.assert_awaited_once()
        self.assertIn(777, m.await_args.args[0])

    def test_alert_off_persists_removal(self):
        ctx = _FakeContext()
        ctx.bot_data["event_alert_subscribers"] = {777, 555}
        bot = MarketBot.__new__(MarketBot)
        upd = _FakeUpdate("/alert off", user_id=9999)
        with mock.patch.object(db, "save_event_alert_subscribers_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.alert_command(upd, ctx))
        m.assert_awaited_once()
        self.assertEqual(m.await_args.args[0], {555})

    def test_callback_alert_on_persists(self):
        class _QMsg:
            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append((text, kwargs))

        class _Query:
            data = "alert_on"

            def __init__(self):
                self.message = _QMsg()

            async def answer(self):
                pass

        query = _Query()
        upd = type("U", (), {
            "callback_query": query,
            "effective_chat": type("C", (), {"id": 777})(),
        })()
        ctx = _FakeContext()
        bot = MarketBot.__new__(MarketBot)
        with mock.patch.object(db, "save_event_alert_subscribers_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.handle_callback(upd, ctx))
        m.assert_awaited_once()
        self.assertIn(777, m.await_args.args[0])


class TestReminderDedupPersistence(unittest.TestCase):
    """check_event_reminders menulis kunci event yang baru di-notify ke DB."""

    def _run(self, events, notified=None, subscribers=(777,)):
        from datetime import datetime, timedelta, timezone

        class _Bot:
            async def send_message(self, chat_id, text, **kwargs):
                return None

        app = _FakeApplication()
        app.bot = _Bot()
        app.bot_data["event_alert_subscribers"] = set(subscribers)
        app.bot_data["event_alert_notified"] = set(notified or [])

        bot = MarketBot.__new__(MarketBot)
        bot.macro = type("M", (), {})()

        with mock.patch.object(bot.macro, "get_economic_calendar", create=True,
                               new_callable=mock.AsyncMock, return_value=events), \
             mock.patch.object(db, "save_event_alert_notified_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.check_event_reminders(app))
        return m, app

    @staticmethod
    def _event(name="CPI / Inflasi AS (YoY)", hours_ahead=0.5, impact="high"):
        from datetime import datetime, timedelta, timezone

        dt = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        return {
            "event": name, "country": "US", "country_emoji": "🇺🇸",
            "time": dt.strftime("%d %b %Y %H:%M"), "_dt_utc": dt,
            "impact": impact, "impact_label": "🔥 HIGH",
        }

    def test_new_event_persists_notified_key(self):
        m, _ = self._run([self._event()])
        m.assert_awaited_once()
        saved = m.await_args.args[0]
        self.assertEqual(len(saved), 1)
        self.assertIn("CPI / Inflasi AS (YoY)", next(iter(saved)))

    def test_no_change_does_not_persist(self):
        # Event sudah pernah di-notify → tidak ada key baru → tidak menulis DB
        e = self._event()
        key = f"{e['event']}|{e['_dt_utc'].isoformat()}"
        m, _ = self._run([e], notified=[key])
        m.assert_not_awaited()


class TestSchedulerPersistence(unittest.TestCase):
    """check_price_alerts menyimpan daftar tersisa setelah alert terpicu."""

    def test_check_price_alerts_persists_remaining(self):
        app = _FakeApplication()
        app.bot_data["price_alerts"] = [
            _alert(1, 9999, target=1.10, direction="above"),   # EUR/USD → terpicu (harga 1.12)
            _alert(2, 8888, symbol="GC=F", display_name="XAU/USD (Gold)",
                   target=2350.0, direction="below", chat_id=888),  # gold 2400 → belum
        ]
        bot = MarketBot.__new__(MarketBot)
        bot.market = type("M", (), {})()
        # Harga per simbol: EUR/USD 1.12 (terpicu above 1.10); gold 2400 (di atas
        # target below 2350 → belum terpicu, tetap di daftar)
        def _fake_price(symbol, **kwargs):
            price = 1.12 if symbol == "EURUSD=X" else 2400.0
            return {"current_price": price}

        with mock.patch.object(bot.market, "get_yahoo_data", create=True,
                               side_effect=_fake_price), \
             mock.patch.object(db, "save_price_alerts_async",
                               new_callable=mock.AsyncMock) as m:
            asyncio.run(bot.check_price_alerts(app))
        m.assert_awaited_once()
        remaining = m.await_args.args[0]
        self.assertEqual([a["id"] for a in remaining], [2])

    def test_persist_best_effort_no_crash(self):
        """Helper tidak boleh crash walau DB error (mis. network down)."""
        ctx = _FakeContext()
        bot = MarketBot.__new__(MarketBot)
        with mock.patch.object(db, "save_price_alerts_async",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(db, "save_event_alert_subscribers_async",
                               side_effect=RuntimeError("db down")):
            asyncio.run(bot._persist_price_alerts(ctx))       # tidak raise
            asyncio.run(bot._persist_alert_subscribers(ctx))  # tidak raise


if __name__ == "__main__":
    unittest.main()
