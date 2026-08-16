"""Unit tests untuk pre-warm cache COT (job Jumat malam) — tanpa network.

Memverifikasi:
- Jendela waktu (Jumat >= 21:00 / Sabtu < 12:00) — hari lain skip.
- Pre-warm mengisi cache SEMUA instrumen (legacy & TFF).
- Data identik dengan cache → skip tulis (tidak boros request).
- Interpretasi AI lama dipertahankan (tidak panggil AI ulang).
- Kegagalan satu instrumen tidak menghentikan sisanya.
"""

import asyncio
import unittest
from datetime import datetime
from unittest import mock

from bot import commands_cot as cot_mod
from bot import handlers as handlers_mod
from bot.handlers import MarketBot
from bot.scheduler_jobs import SchedulerJobsMixin


class _FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _FakeUpdate:
    def __init__(self, text, user_id=9999, chat_id=777):
        self.message = _FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": chat_id})()


class _FakeContext:
    def __init__(self, bot_data=None):
        self.bot_data = bot_data if bot_data is not None else {}
        self.bot = mock.MagicMock()
        # send_chat_action dipanggil dengan await — harus awaitable
        self.bot.send_chat_action = mock.AsyncMock()


class _FakeApplication:
    def __init__(self):
        self.bot_data = {}
        self.bot = mock.MagicMock()


def _mk_bot(ai=None):
    bot = MarketBot.__new__(MarketBot)
    # Default: engine AI ada (MagicMock) agar jalur interpretasi AI diuji;
    # None hanya dipakai test yang ingin memastikan AI tidak dipanggil.
    bot.ai = mock.MagicMock() if ai is None else ai
    return bot


def _mk_data(display="Gold Futures (COMEX)", with_ai=False):
    data = {
        "display": display,
        "keywords": ["gold"],
        "market_name": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date": datetime(2026, 8, 11).date(),
        "open_interest": 500000,
        "noncommercial": {"long": 300000, "short": 100000, "net": 200000, "change": 10000},
        "commercial": {"long": 100000, "short": 400000, "net": -300000, "change": -5000},
        "nonreportable": {"long": 1, "short": 2},
        "prev_week": {"date": datetime(2026, 8, 4).date()},
    }
    if with_ai:
        data["ai_interpretation"] = "AI lama"
    return data


class TestCOTPrewarmWindow(unittest.TestCase):
    # Default COT_PREWARM_DAYS = Senin-Sabtu (1-6), COT_PREWARM_HOUR = 4.

    def test_default_days_inside_window(self):
        # 2026-08-10 = Senin (ISO 1), 08-11 = Selasa (2), 08-14 = Jumat (5),
        # 08-15 = Sabtu (6) — semua dalam jendela default (>= jam 4).
        for dt in (
            datetime(2026, 8, 10, 4, 0),   # Senin pagi — kasus permintaan user
            datetime(2026, 8, 11, 4, 0),   # Selasa
            datetime(2026, 8, 14, 4, 0),   # Jumat
            datetime(2026, 8, 15, 4, 0),   # Sabtu
            datetime(2026, 8, 15, 11, 59),
        ):
            self.assertTrue(
                SchedulerJobsMixin._is_cot_prewarm_window(dt),
                f"{dt} harus dalam jendela",
            )

    def test_before_hour_and_sunday_skip(self):
        # Sebelum jam pintu masuk
        self.assertFalse(SchedulerJobsMixin._is_cot_prewarm_window(datetime(2026, 8, 14, 3, 59)))
        # Minggu (2026-08-16 = ISO 7) — di luar default
        self.assertFalse(SchedulerJobsMixin._is_cot_prewarm_window(datetime(2026, 8, 16, 4, 0)))

    def test_custom_days_respected(self):
        # COT_PREWARM_DAYS=1 (hanya Senin) → Selasa di luar jendela
        with mock.patch("bot.scheduler_jobs.COT_PREWARM_DAYS", [1]):
            self.assertTrue(SchedulerJobsMixin._is_cot_prewarm_window(datetime(2026, 8, 10, 4, 0)))
            self.assertFalse(SchedulerJobsMixin._is_cot_prewarm_window(datetime(2026, 8, 11, 4, 0)))
        # Semua hari (1-7) → Minggu ikut pre-warm
        with mock.patch("bot.scheduler_jobs.COT_PREWARM_DAYS", [1, 2, 3, 4, 5, 6, 7]):
            self.assertTrue(SchedulerJobsMixin._is_cot_prewarm_window(datetime(2026, 8, 16, 4, 0)))


class TestCOTPrewarm(unittest.TestCase):
    def test_prewarm_caches_all_instruments(self):
        bot = _mk_bot()
        app = _FakeApplication()
        instruments = [
            {"keywords": ["gold"], "display": "Gold", "prefer": []},
            {"keywords": ["euro fx"], "display": "Euro", "prefer": []},
            {"keywords": ["usd index"], "display": "DXY", "prefer": [], "report": "tff"},
        ]
        data_gold = _mk_data("Gold")
        data_eur = _mk_data("Euro")
        data_dxy = _mk_data("DXY")
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("bot.scheduler_jobs.COT_INSTRUMENTS", instruments), \
             mock.patch("bot.scheduler_jobs.fetch_year_rows", return_value=["row-legacy"]), \
             mock.patch("bot.scheduler_jobs.fetch_tff_rows", return_value=["row-tff"]), \
             mock.patch("bot.scheduler_jobs.extract_market", side_effect=lambda rows, cfg: {
                 "gold": data_gold, "euro fx": data_eur, "usd index": data_dxy,
             }[cfg["keywords"][0]]), \
             mock.patch.object(bot, "_cot_cache_key", side_effect=lambda cfg: "cot:" + cfg["keywords"][0]), \
             mock.patch.object(bot, "_cot_ai_interpretation", new=mock.AsyncMock(return_value="AI baru")), \
             mock.patch("bot.scheduler_jobs.db.get_cot_cache_async", new=mock.AsyncMock(return_value={})) as m_get, \
             mock.patch("bot.scheduler_jobs.db.set_cot_cache_async", new=mock.AsyncMock(return_value=True)) as m_set:
            asyncio.run(bot.prewarm_cot_cache(app))

        self.assertEqual(m_set.await_count, 3)
        keys = {c.args[0] for c in m_set.await_args_list}
        self.assertEqual(keys, {"cot:gold", "cot:euro fx", "cot:usd index"})
        # DXY (TFF) harus diekstrak dari row TFF
        self.assertEqual(m_get.await_count, 3)
        # Interpretasi AI dihasilkan untuk data baru (cek per-instrumen)
        by_key = {c.args[0]: c.args[1] for c in m_set.await_args_list}
        self.assertEqual(by_key["cot:gold"]["ai_interpretation"], "AI baru")
        self.assertEqual(by_key["cot:usd index"]["ai_interpretation"], "AI baru")
        # Statistik run tercatat
        self.assertEqual(app.bot_data["cot_prewarm_stats"]["ok"], 3)

    def test_identical_data_skips_write_and_keeps_old_ai(self):
        bot = _mk_bot()
        app = _FakeApplication()
        instruments = [{"keywords": ["gold"], "display": "Gold", "prefer": []}]
        data = _mk_data("Gold", with_ai=True)  # AI lama sudah ada
        cached = {"data": {"data": {  # bentuk cache: row.data berisi JSON-safe
            "display": "Gold", "keywords": ["gold"],
            "market_name": "GOLD - COMMODITY EXCHANGE INC.",
            "report_date": "2026-08-11",
            "open_interest": 500000,
            "noncommercial": {"long": 300000, "short": 100000, "net": 200000, "change": 10000},
            "commercial": {"long": 100000, "short": 400000, "net": -300000, "change": -5000},
            "nonreportable": {"long": 1, "short": 2},
            "prev_week": {"date": "2026-08-04"},
            "ai_interpretation": "AI lama",
        }}}
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("bot.scheduler_jobs.COT_INSTRUMENTS", instruments), \
             mock.patch("bot.scheduler_jobs.fetch_year_rows", return_value=["row"]), \
             mock.patch("bot.scheduler_jobs.fetch_tff_rows", return_value=[]), \
             mock.patch("bot.scheduler_jobs.extract_market", return_value=data), \
             mock.patch.object(bot, "_cot_cache_key", return_value="cot:gold"), \
             mock.patch.object(bot, "_cot_ai_interpretation", new=mock.AsyncMock(return_value="TIDAK BOLEH DIPANGGIL")) as m_ai, \
             mock.patch("bot.scheduler_jobs.db.get_cot_cache_async", new=mock.AsyncMock(return_value=cached)), \
             mock.patch("bot.scheduler_jobs.db.set_cot_cache_async", new=mock.AsyncMock(return_value=True)) as m_set:
            asyncio.run(bot.prewarm_cot_cache(app))

        m_set.assert_not_awaited()  # data identik → tidak tulis ulang
        m_ai.assert_not_awaited()   # AI lama dipertahankan → tidak panggil AI
        self.assertEqual(app.bot_data["cot_prewarm_stats"]["skipped"], 1)

    def test_failed_instrument_does_not_stop_others(self):
        bot = _mk_bot()
        app = _FakeApplication()
        instruments = [
            {"keywords": ["gold"], "display": "Gold", "prefer": []},
            {"keywords": ["silver"], "display": "Silver", "prefer": []},
        ]
        def _extract(rows, cfg):
            if cfg["keywords"][0] == "silver":
                raise RuntimeError("network down")
            return _mk_data(cfg["keywords"][0])
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("bot.scheduler_jobs.COT_INSTRUMENTS", instruments), \
             mock.patch("bot.scheduler_jobs.fetch_year_rows", return_value=["row"]), \
             mock.patch("bot.scheduler_jobs.fetch_tff_rows", return_value=[]), \
             mock.patch("bot.scheduler_jobs.extract_market", side_effect=_extract), \
             mock.patch.object(bot, "_cot_cache_key", side_effect=lambda cfg: "cot:" + cfg["keywords"][0]), \
             mock.patch("bot.scheduler_jobs.db.get_cot_cache_async", new=mock.AsyncMock(return_value={})), \
             mock.patch("bot.scheduler_jobs.db.set_cot_cache_async", new=mock.AsyncMock(return_value=True)) as m_set:
            asyncio.run(bot.prewarm_cot_cache(app))

        # Gold sukses, Silver gagal — tapi run tetap selesai & Gold ter-cache
        self.assertEqual(m_set.await_count, 1)
        self.assertEqual(m_set.await_args.args[0], "cot:gold")
        stats = app.bot_data["cot_prewarm_stats"]
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["failed"], 1)

    def test_max_instruments_limits_run(self):
        bot = _mk_bot()
        app = _FakeApplication()
        instruments = [
            {"keywords": ["gold"], "display": "Gold", "prefer": []},
            {"keywords": ["silver"], "display": "Silver", "prefer": []},
            {"keywords": ["euro fx"], "display": "Euro", "prefer": []},
        ]
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("bot.scheduler_jobs.COT_INSTRUMENTS", instruments), \
             mock.patch("bot.scheduler_jobs.fetch_year_rows", return_value=["row"]), \
             mock.patch("bot.scheduler_jobs.fetch_tff_rows", return_value=[]), \
             mock.patch("bot.scheduler_jobs.extract_market", return_value=_mk_data("X")), \
             mock.patch.object(bot, "_cot_cache_key", return_value="cot:x"), \
             mock.patch("bot.scheduler_jobs.db.get_cot_cache_async", new=mock.AsyncMock(return_value={})), \
             mock.patch("bot.scheduler_jobs.db.set_cot_cache_async", new=mock.AsyncMock(return_value=True)) as m_set:
            asyncio.run(bot.prewarm_cot_cache(app, max_instruments=2))

        self.assertEqual(m_set.await_count, 2)
        self.assertEqual(app.bot_data["cot_prewarm_stats"]["ok"], 2)

    def test_all_archives_fail_is_noop(self):
        bot = _mk_bot()
        app = _FakeApplication()
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("bot.scheduler_jobs.COT_INSTRUMENTS", [{"keywords": ["gold"], "display": "Gold", "prefer": []}]), \
             mock.patch("bot.scheduler_jobs.fetch_year_rows", return_value=[]), \
             mock.patch("bot.scheduler_jobs.fetch_tff_rows", return_value=[]), \
             mock.patch("bot.scheduler_jobs.db.set_cot_cache_async", new=mock.AsyncMock()) as m_set, \
             mock.patch("bot.scheduler_jobs.notify_admins", new=mock.AsyncMock()) as m_notify:
            asyncio.run(bot.prewarm_cot_cache(app))
        m_set.assert_not_awaited()
        # Gagal total → admin DINOTIFIKASI (bukan cuma log)
        m_notify.assert_awaited_once()
        self.assertIn("gagal total", m_notify.await_args.args[1].lower())
        self.assertIs(m_notify.await_args.args[0], app.bot)
        # Mark: notif sudah terkirim hari ini
        self.assertIn("cot_prewarm_notified", app.bot_data)

    def test_skip_without_db_saves_resources(self):
        """Supabase tidak terhubung → job tidak download arsip sama sekali
        (hemat CPU/kuota di free tier) dan tidak menulis cache apa pun."""
        bot = _mk_bot()
        app = _FakeApplication()
        with mock.patch("bot.scheduler_jobs.db.is_connected", return_value=False), \
             mock.patch("bot.scheduler_jobs.COT_INSTRUMENTS", [{"keywords": ["gold"], "display": "Gold", "prefer": []}]), \
             mock.patch("bot.scheduler_jobs.fetch_year_rows") as m_fetch, \
             mock.patch("bot.scheduler_jobs.fetch_tff_rows") as m_fetch_tff, \
             mock.patch("bot.scheduler_jobs.db.set_cot_cache_async", new=mock.AsyncMock()) as m_set, \
             mock.patch("bot.scheduler_jobs.notify_admins", new=mock.AsyncMock()) as m_notify:
            asyncio.run(bot.prewarm_cot_cache(app))
        m_fetch.assert_not_called()
        m_fetch_tff.assert_not_called()
        m_set.assert_not_awaited()
        m_notify.assert_not_awaited()
        self.assertNotIn("cot_prewarm_stats", app.bot_data)

    def test_total_failure_notify_rate_limited_per_day(self):
        """Job berjalan tiap pagi (Senin-Sabtu): bila CFTC down berhari-hari,
        admin hanya dinotifikasi 1x per hari kalender (tidak spam)."""
        bot = _mk_bot()
        app = _FakeApplication()
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("bot.scheduler_jobs.COT_INSTRUMENTS", [{"keywords": ["gold"], "display": "Gold", "prefer": []}]), \
             mock.patch("bot.scheduler_jobs.fetch_year_rows", return_value=[]), \
             mock.patch("bot.scheduler_jobs.fetch_tff_rows", return_value=[]), \
             mock.patch("bot.scheduler_jobs.db.set_cot_cache_async", new=mock.AsyncMock()), \
             mock.patch("bot.scheduler_jobs.notify_admins", new=mock.AsyncMock()) as m_notify:
            # Run 1 (hari ini): notif dikirim
            asyncio.run(bot.prewarm_cot_cache(app))
            self.assertEqual(m_notify.await_count, 1)
            # Run 2 di hari yang sama: tidak notif lagi
            asyncio.run(bot.prewarm_cot_cache(app))
            self.assertEqual(m_notify.await_count, 1)


def _fake_cot_update(data: str):
    """Update tiruan untuk callback `cot:`/`settings_cot` — edit_message_text
    merekam pesan ke message.replies seperti reply_text."""
    msg = _FakeMessage()

    async def _edit(text, **kwargs):
        msg.replies.append((text, kwargs))
        return text

    q = type("Q", (), {})()
    q.data = data
    q.from_user = type("U", (), {"id": 9999})()
    q.message = msg
    q.answer = mock.AsyncMock()
    q.edit_message_text = _edit  # instance attr — tidak ter-bind, signature (text, **kwargs)
    return type("U", (), {
        "callback_query": q,
        "effective_chat": type("C", (), {"id": 777})(),
    })()


class TestCotStatusText(unittest.TestCase):
    """Formatter jadwal & statistik pre-warm untuk /status (murni)."""

    def test_no_run_yet(self):
        txt = SchedulerJobsMixin._cot_prewarm_status_text({})
        self.assertIn("Aktif", txt)
        self.assertIn("04:00", txt)
        self.assertIn("Belum pernah berjalan", txt)

    def test_with_stats(self):
        bot_data = {
            "cot_prewarm_stats": {
                "ok": 31, "skipped": 1, "failed": 0,
                "at": "2026-08-15T21:05:00+00:00",
            }
        }
        txt = SchedulerJobsMixin._cot_prewarm_status_text(bot_data)
        self.assertIn("31 di-cache", txt)
        self.assertIn("1 segar", txt)
        self.assertIn("0 gagal", txt)
        self.assertIn("Terakhir", txt)
        # 'at' (UTC) dikonversi ke WIB (+7) → 16 Agu 04:05
        self.assertIn("04:05", txt)

    def test_notified_date_shown(self):
        bot_data = {
            "cot_prewarm_stats": {"ok": 0, "skipped": 0, "failed": 5, "at": "2026-08-15T21:05:00+00:00"},
            "cot_prewarm_notified": "2026-08-16",
        }
        txt = SchedulerJobsMixin._cot_prewarm_status_text(bot_data)
        self.assertIn("Notif admin gagal terakhir", txt)
        self.assertIn("16 Aug 2026", txt)

    def test_no_notified_no_line(self):
        txt = SchedulerJobsMixin._cot_prewarm_status_text({"cot_prewarm_stats": {"ok": 1}})
        self.assertNotIn("Notif admin", txt)

    def test_disabled(self):
        with mock.patch("bot.scheduler_jobs.COT_PREWARM_ENABLED", False):
            txt = SchedulerJobsMixin._cot_prewarm_status_text({})
        self.assertIn("Nonaktif", txt)

    def test_all_days_shows_every_day(self):
        with mock.patch("bot.scheduler_jobs.COT_PREWARM_DAYS", [1, 2, 3, 4, 5, 6, 7]):
            txt = SchedulerJobsMixin._cot_prewarm_status_text({})
        self.assertIn("Setiap hari", txt)

    def test_custom_days_listed(self):
        with mock.patch("bot.scheduler_jobs.COT_PREWARM_DAYS", [1, 6]):
            txt = SchedulerJobsMixin._cot_prewarm_status_text({})
        self.assertIn("Senin", txt)
        self.assertIn("Sabtu", txt)


class TestCotQuickActions(unittest.TestCase):
    """Tombol quick action di pesan /cot (callback `cot:<alias>`)."""

    def test_usage_has_quick_keyboard(self):
        bot = _mk_bot()
        bot._daily_usage = {}  # dipakai _check_command_rate_limit
        upd = _FakeUpdate("/cot")
        ctx = _FakeContext()
        with mock.patch.object(bot, "_check_command_rate_limit", new=mock.AsyncMock(return_value=True)):
            asyncio.run(bot.cot_command(upd, ctx))
        _, kwargs = upd.message.replies[0]
        kb = kwargs.get("reply_markup")
        self.assertIsNotNone(kb, "pesan /cot harus punya keyboard quick action")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("cot:gold", callbacks)
        self.assertIn("cot:eur", callbacks)
        self.assertIn("cot:vix", callbacks)

    def test_quick_action_reports_known_instrument(self):
        bot = _mk_bot()
        # _cot_report_text di-stub → callback hanya mendelegasikan + render
        expected = "📊 laporan COT Gold"
        with mock.patch.object(bot, "_cot_report_text", new=mock.AsyncMock(return_value=expected)), \
             mock.patch.object(bot, "_cot_quick_keyboard") as m_kb:
            upd = _fake_cot_update("cot:gold")
            ctx = _FakeContext()
            asyncio.run(bot.handle_callback(upd, ctx))
            m_kb.assert_called_once()
            text, _ = upd.callback_query.message.replies[0]
            self.assertIn("laporan COT Gold", text)

    def test_quick_action_unknown_instrument(self):
        bot = _mk_bot()
        with mock.patch.object(bot, "_cot_report_text", new=mock.AsyncMock()) as m:
            upd = _fake_cot_update("cot:zzzz")
            ctx = _FakeContext()
            asyncio.run(bot.handle_callback(upd, ctx))
            m.assert_not_awaited()
            text, _ = upd.callback_query.message.replies[0]
            self.assertIn("tidak dikenali", text)


class TestCotSettingsButton(unittest.TestCase):
    """Tombol '📊 COT Pre-warm' di menu /settings."""

    def test_settings_menu_has_button_and_status(self):
        bot = _mk_bot()
        upd = _FakeUpdate("/settings")
        ctx = _FakeContext()
        ctx.bot_data["cot_prewarm_stats"] = {"ok": 31, "skipped": 1, "failed": 0, "at": "2026-08-15T21:05:00+00:00"}
        with mock.patch.object(handlers_mod.db, "is_subscribed_async", new=mock.AsyncMock(return_value=False)), \
             mock.patch.object(handlers_mod.db, "get_watchlist_async", new=mock.AsyncMock(return_value=[])):
            asyncio.run(bot.settings_command(upd, ctx))
        text, kwargs = upd.message.replies[0]
        kb = kwargs.get("reply_markup")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("settings_cot", callbacks)
        self.assertIn("COT Pre-warm", text)
        self.assertIn("Aktif", text)

    def test_callback_shows_detail_and_back_button(self):
        bot = _mk_bot()
        ctx = _FakeContext()
        ctx.bot_data["cot_prewarm_stats"] = {"ok": 31, "skipped": 1, "failed": 0, "at": "2026-08-15T21:05:00+00:00"}
        upd = _fake_cot_update("settings_cot")
        asyncio.run(bot.handle_callback(upd, ctx))
        text, kwargs = upd.callback_query.message.replies[0]
        self.assertIn("COT PRE-WARM", text)
        self.assertIn("31 di-cache", text)
        kb = kwargs.get("reply_markup")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("settings", callbacks)  # tombol kembali ke pengaturan


class TestCotRefreshCommand(unittest.TestCase):
    """Perintah admin /cotrefresh — pemicu manual pre-warm."""

    def test_rejects_non_admin(self):
        bot = _mk_bot()
        upd = _FakeUpdate("/cotrefresh", user_id=424242)
        ctx = _FakeContext()
        with mock.patch.object(cot_mod, "ADMIN_USER_IDS", [9999]), \
             mock.patch.object(bot, "prewarm_cot_cache", new=mock.AsyncMock()) as m:
            asyncio.run(bot.cotrefresh_command(upd, ctx))
        m.assert_not_awaited()
        self.assertIn("khusus admin", upd.message.replies[0][0])

    def test_manual_prewarm_reports_stats(self):
        bot = _mk_bot()
        upd = _FakeUpdate("/cotrefresh", user_id=9999)
        ctx = _FakeContext()
        with mock.patch.object(cot_mod, "ADMIN_USER_IDS", [9999]), \
             mock.patch.object(bot, "prewarm_cot_cache", new=mock.AsyncMock()) as m:
            asyncio.run(bot.cotrefresh_command(upd, ctx))
        m.assert_awaited_once()
        # prewarm dipanggil dengan adapter minimal (bot + bot_data)
        args = m.await_args
        self.assertEqual(args.args[0].bot, ctx.bot)
        self.assertIs(args.args[0].bot_data, ctx.bot_data)
        self.assertEqual(args.kwargs.get("max_instruments"), 0)
        # Respon sukses
        text = upd.message.replies[0][0]
        self.assertIn("selesai", text.lower())

    def test_limit_argument_passed(self):
        bot = _mk_bot()
        upd = _FakeUpdate("/cotrefresh 10", user_id=9999)
        ctx = _FakeContext()
        with mock.patch.object(cot_mod, "ADMIN_USER_IDS", [9999]), \
             mock.patch.object(bot, "prewarm_cot_cache", new=mock.AsyncMock()) as m:
            asyncio.run(bot.cotrefresh_command(upd, ctx))
        self.assertEqual(m.await_args.kwargs.get("max_instruments"), 10)

    def test_invalid_limit_shows_usage(self):
        bot = _mk_bot()
        upd = _FakeUpdate("/cotrefresh abc", user_id=9999)
        ctx = _FakeContext()
        with mock.patch.object(cot_mod, "ADMIN_USER_IDS", [9999]), \
             mock.patch.object(bot, "prewarm_cot_cache", new=mock.AsyncMock()) as m:
            asyncio.run(bot.cotrefresh_command(upd, ctx))
        m.assert_not_awaited()
        self.assertIn("Format", upd.message.replies[0][0])

    def test_total_failure_reports_error(self):
        bot = _mk_bot()
        upd = _FakeUpdate("/cotrefresh", user_id=9999)
        ctx = _FakeContext()
        ctx.bot_data["cot_prewarm_stats"] = {"ok": 0, "skipped": 0, "failed": 5}
        with mock.patch.object(cot_mod, "ADMIN_USER_IDS", [9999]), \
             mock.patch.object(bot, "prewarm_cot_cache", new=mock.AsyncMock()) as m:
            asyncio.run(bot.cotrefresh_command(upd, ctx))
        m.assert_awaited_once()
        self.assertIn("gagal total", upd.message.replies[0][0].lower())

    def test_exception_in_prewarm_is_caught(self):
        bot = _mk_bot()
        upd = _FakeUpdate("/cotrefresh", user_id=9999)
        ctx = _FakeContext()
        with mock.patch.object(cot_mod, "ADMIN_USER_IDS", [9999]), \
             mock.patch.object(bot, "prewarm_cot_cache", new=mock.AsyncMock(side_effect=RuntimeError("x"))):
            asyncio.run(bot.cotrefresh_command(upd, ctx))
        self.assertIn("gagal", upd.message.replies[0][0].lower())


if __name__ == "__main__":
    unittest.main()
