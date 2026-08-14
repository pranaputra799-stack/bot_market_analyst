"""Unit tests untuk notifikasi admin otomatis (anti silent-fail).

Mencakup:
- utils/admin_alerts.notify_admins: kirim ke semua admin, best-effort saat gagal
- data/database.check_required_tables: deteksi tabel Supabase hilang (404)
- bot.handlers.MarketBot.notify_ai_outage: alert AI-down sekali + alert pulih
"""

import asyncio
import unittest
from unittest import mock

from utils import admin_alerts as alerts_mod
from data import database as db_mod
from bot import handlers as handlers_mod
from bot.handlers import MarketBot


class TestNotifyAdmins(unittest.TestCase):
    class _Bot:
        def __init__(self, fail_ids=()):
            self.fail_ids = set(fail_ids)
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None):
            if chat_id in self.fail_ids:
                raise RuntimeError("Forbidden")
            self.sent.append((chat_id, text, parse_mode))

    def test_sends_to_all_admins(self):
        bot = self._Bot()
        with mock.patch.object(alerts_mod, "ADMIN_USER_IDS", [1, 2, 3]):
            sent = asyncio.run(alerts_mod.notify_admins(bot, "hello"))
        self.assertEqual(sent, 3)
        self.assertEqual(len(bot.sent), 3)

    def test_failure_is_best_effort(self):
        bot = self._Bot(fail_ids={2})
        with mock.patch.object(alerts_mod, "ADMIN_USER_IDS", [1, 2, 3]):
            sent = asyncio.run(alerts_mod.notify_admins(bot, "hello"))
        self.assertEqual(sent, 2, "Admin yang gagal di-skip, sisanya tetap terkirim")
        self.assertEqual(len(bot.sent), 2)

    def test_no_admins_is_noop(self):
        bot = self._Bot()
        with mock.patch.object(alerts_mod, "ADMIN_USER_IDS", []):
            sent = asyncio.run(alerts_mod.notify_admins(bot, "hello"))
        self.assertEqual(sent, 0)
        self.assertEqual(bot.sent, [])


class TestCheckRequiredTables(unittest.TestCase):
    def _resp(self, status_code):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.raise_for_status.return_value = None
        return resp

    def _patch_session(self, status_by_table):
        session = mock.Mock()
        session.get.side_effect = lambda url, **kw: self._resp(
            status_by_table[url.split("/rest/v1/")[1].split("?")[0]]
        )
        return mock.patch.object(db_mod, "_session", return_value=session)

    def test_not_configured_returns_empty(self):
        with mock.patch.object(db_mod, "SUPABASE_URL", ""), \
                mock.patch.object(db_mod, "SUPABASE_KEY", ""):
            self.assertEqual(db_mod.Database.check_required_tables(), [])

    def test_all_tables_present_returns_empty(self):
        ok = {t: 200 for t in db_mod.Database.REQUIRED_TABLES}
        with mock.patch.object(db_mod, "SUPABASE_URL", "https://x.supabase.co"), \
                mock.patch.object(db_mod, "SUPABASE_KEY", "k"), \
                self._patch_session(ok):
            self.assertEqual(db_mod.Database.check_required_tables(), [])

    def test_missing_table_detected(self):
        status = {t: 200 for t in db_mod.Database.REQUIRED_TABLES}
        status["news_predictions"] = 404
        with mock.patch.object(db_mod, "SUPABASE_URL", "https://x.supabase.co"), \
                mock.patch.object(db_mod, "SUPABASE_KEY", "k"), \
                self._patch_session(status):
            self.assertEqual(db_mod.Database.check_required_tables(), ["news_predictions"])

    def test_network_error_not_reported_as_missing(self):
        """Error jaringan BUKAN dianggap tabel hilang (anti false-positive)."""
        session = mock.Mock()
        session.get.side_effect = RuntimeError("network down")
        with mock.patch.object(db_mod, "SUPABASE_URL", "https://x.supabase.co"), \
                mock.patch.object(db_mod, "SUPABASE_KEY", "k"), \
                mock.patch.object(db_mod, "_session", return_value=session):
            self.assertEqual(db_mod.Database.check_required_tables(), [])


class TestAiOutageNotify(unittest.TestCase):
    def _setup(self, down=False, was_down=False):
        bot = MarketBot.__new__(MarketBot)
        bot.ai = mock.MagicMock()
        bot.ai.is_total_failure_active.return_value = down
        app = mock.MagicMock()
        app.bot_data = {"_ai_down_notified": was_down}
        app.bot = mock.MagicMock()
        return bot, app

    def test_notifies_once_when_down(self):
        bot, app = self._setup(down=True, was_down=False)
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", [1]), \
                mock.patch("utils.admin_alerts.notify_admins", new=mock.AsyncMock()) as m:
            asyncio.run(bot.notify_ai_outage(app))
        m.assert_awaited_once()
        self.assertTrue(app.bot_data["_ai_down_notified"])

    def test_no_repeat_alert_while_still_down(self):
        bot, app = self._setup(down=True, was_down=True)
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", [1]), \
                mock.patch("utils.admin_alerts.notify_admins", new=mock.AsyncMock()) as m:
            asyncio.run(bot.notify_ai_outage(app))
        m.assert_not_awaited()

    def test_sends_recovery_alert(self):
        bot, app = self._setup(down=False, was_down=True)
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", [1]), \
                mock.patch("utils.admin_alerts.notify_admins", new=mock.AsyncMock()) as m:
            asyncio.run(bot.notify_ai_outage(app))
        m.assert_awaited_once()
        self.assertFalse(app.bot_data["_ai_down_notified"])

    def test_noop_without_admin_ids(self):
        bot, app = self._setup(down=True)
        with mock.patch.object(handlers_mod, "ADMIN_USER_IDS", []), \
                mock.patch("utils.admin_alerts.notify_admins", new=mock.AsyncMock()) as m:
            asyncio.run(bot.notify_ai_outage(app))
        m.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
