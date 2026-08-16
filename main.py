#!/usr/bin/env python3
"""
MarketAI Analyst Bot - Main Entry Point
Bot Telegram untuk analisis pasar keuangan dengan multi-AI dan multi-data source.

Usage:
    python main.py              # Run bot dengan polling
    python main.py --webhook    # Run bot dengan webhook (untuk production)

Environment variables diatur di file .env
"""
import asyncio
import json
import logging
import signal
import sys
import os
import time as _time  # alias: nama `time` dipakai datetime.time (scheduler)
from datetime import datetime, time, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Set path untuk import - pastikan root project selalu ada di sys.path
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)
# Pindah ke direktori project agar file relatif (bot_data.pkl, .env, dll) berfungsi
os.chdir(_BASE_DIR)


from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from config.settings import (
    TELEGRAM_TOKEN,
    LOG_LEVEL,
    LOG_FILE,
    LOG_TO_FILE,
    MORNING_BRIEF_HOUR,
    MORNING_BRIEF_MINUTE,
    MORNING_BRIEF_TIMEZONE,
    ECONOMIC_ALERT_ENABLED,
    ECONOMIC_ALERT_DIGEST_HOUR,
    ECONOMIC_ALERT_DIGEST_MINUTE,
    ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES,
    EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES,
    SESSION_ALERT_ENABLED,
    SESSION_ALERT_INTERVAL_MINUTES,
    AI_USAGE_REPORT_ENABLED,
    AI_USAGE_REPORT_HOUR,
    AI_USAGE_REPORT_MINUTE,
    COT_PREWARM_ENABLED,
    COT_PREWARM_HOUR,
    COT_PREWARM_MINUTE,
    COT_PREWARM_MAX_INSTRUMENTS,
    NEWS_PREDICTION_ENABLED,
    NEWS_PREDICTION_CHECK_INTERVAL_MINUTES,
    BOT_USERNAME,
    BOT_NAME,
    PORT,
    WEBHOOK_URL,
    WEBHOOK_LISTEN,
    WEBHOOK_SECRET,
    IS_CLOUD,
    BOT_RUN_MODE,
)
from bot.handlers import MarketBot
from data.cache import cleanup_all
from data.database import db
from data.oanda_stream import start_stream
from utils.health_server import start_health_server

# ===================== LOGGING SETUP =====================
log_handlers = [logging.StreamHandler()]
if LOG_TO_FILE and LOG_FILE:
    log_handlers.append(logging.FileHandler(LOG_FILE))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    handlers=log_handlers,
)
logger = logging.getLogger(__name__)

# True setelah post_init selesai (bot siap menerima update). Dipakai untuk
# membedakan kegagalan STARTUP (idle, hindari crash-loop) vs kegagalan RUNTIME
# (biarkan platform me-restart — health tidak boleh tampak sehat saat bot mati).
BOT_STARTED = False

# ===================== ERROR TRACKING (Sentry) =====================
# Aktif hanya jika SENTRY_DSN diisi di .env / dashboard deploy.
# Import dibungkus try/except agar bot tetap jalan walau sentry-sdk belum terpasang.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    try:
        import sentry_sdk  # type: ignore

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment="production" if IS_CLOUD else "development",
            traces_sample_rate=0.1,
        )
        logger.info("Sentry error tracking initialized")
    except Exception as e:
        logger.warning(f"Sentry init failed: {e}")


async def handle_bot_error(update: object, context) -> None:
    """Error handler GLOBAL PTB — semua exception handler terekam + user dikabari.

    Tanpa ini, exception di handler hanya tercatat di log: user tidak mendapat
    umpan balik apa pun (bot tampak "diam"/tidak jalan). Dengan handler ini,
    exception selalu di-log lengkap dan user yang aktif dikirimi pesan singkat.
    """
    logger.error(
        "Exception saat memproses update: %s",
        context.error,
        exc_info=context.error,
    )
    # Kabari user yang terkena dampak (jika update punya chat) — anti silent-fail.
    effective_chat = getattr(update, "effective_chat", None) if update is not None else None
    if effective_chat is not None:
        try:
            await effective_chat.send_message(
                "⚠️ Terjadi kesalahan internal saat memproses permintaanmu. "
                "Silakan coba lagi — kalau berulang, cek /status."
            )
        except Exception:
            pass


async def post_init(application: Application):
    """Setup setelah bot initialize."""
    # Set commands untuk menu bot — daftar ada di utils/bot_menu.py (satu
    # sumber kebenaran). Retry + bersihkan semua scope: kalau deploy lama
    # meninggalkan command mati (/pa, /chart, /watch) di Telegram, menu
    # langsung diperbarui ke versi terbaru. Kegagalan TIDAK mematikan bot.
    from utils.bot_menu import set_bot_commands

    await set_bot_commands(application.bot)

    # Log diagnostik startup: token (termask) + mode — membantu cek konfigurasi
    # di panel JustRunMy bila bot tidak merespons.
    token_hint = (
        f"{TELEGRAM_TOKEN[:6]}...{TELEGRAM_TOKEN[-4:]}" if TELEGRAM_TOKEN else "(KOSONG!)"
    )
    logger.info(
        f"Bot {BOT_NAME} (@{BOT_USERNAME}) started! token={token_hint} "
        f"run_mode={BOT_RUN_MODE} webhook_url={'set' if WEBHOOK_URL else 'kosong'}"
    )

    # Muat state persisten dari Supabase — subscriber notifikasi event (/alert).
    # Dulunya RAM-only, jadi hilang saat restart; sekarang dimuat ulang agar
    # langganan tetap aktif setelah deploy.
    try:
        subscribers = await db.get_event_alert_subscribers_async()
        if subscribers:
            application.bot_data["event_alert_subscribers"] = subscribers
        notified = await db.get_event_alert_notified_async()
        if notified:
            application.bot_data["event_alert_notified"] = notified
        logger.info(
            f"Loaded persistent state: {len(subscribers)} event subscribers, "
            f"{len(notified)} notified keys"
        )
    except Exception as e:
        logger.warning(f"Gagal memuat state persisten dari database: {e}")

    # Muat kuota harian per-user dari Supabase — kuota TIDAK reset saat
    # restart/spin-down free tier (sebelumnya in-memory → bisa disiasati
    # dengan restart). Best-effort; tanpa Supabase no-op.
    bot_instance = application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.load_daily_usage()
        except Exception as e:
            logger.warning(f"Load daily usage gagal saat boot: {e}")

    # Cek tabel Supabase WAJIB — notif admin bila ada yang hilang (penyebab
    # paling umum fitur persisten diam-diam mati: subscriber, cache L2,
    # prediksi news tidak tersimpan). Best-effort; tanpa ADMIN_USER_IDS no-op.
    try:
        missing = await db.check_required_tables_async()
        if missing:
            from utils.admin_alerts import notify_admins

            await notify_admins(
                application.bot,
                "⚠️ *Supabase: tabel belum lengkap!*\n\n"
                f"Tabel hilang: {', '.join(missing)}\n\n"
                "Jalankan `migrations/supabase.sql` di Supabase SQL Editor lalu "
                "redeploy. Tanpa tabel ini, subscriber, cache persisten, & "
                "prediksi news tidak tersimpan.",
            )
    except Exception as e:
        logger.warning(f"Supabase schema check gagal: {e}")

    global BOT_STARTED
    BOT_STARTED = True


async def morning_brief_callback(context):
    """
    Callback untuk mengirim morning brief terjadwal.
    Dipanggil setiap hari jam yang ditentukan.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        await bot_instance.send_scheduled_morning_brief(context.application)


async def event_digest_callback(context):
    """
    Callback untuk mengirim digest event ekonomi harian.
    Dipanggil setiap hari jam ECONOMIC_ALERT_DIGEST_HOUR.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        await bot_instance.send_scheduled_event_digest(context.application)


async def event_reminder_callback(context):
    """
    Callback untuk mengecek & mengirim reminder event high-impact.
    Dipanggil berkala setiap ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES menit.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        await bot_instance.check_event_reminders(context.application)


async def event_aftermath_callback(context):
    """
    Callback untuk mengirim analisis dampak event high-impact yang BARU SAJA rilis
    (angka Actual vs Forecast + pengaruh ke DXY + penjelasan berita).
    Dipanggil berkala setiap ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES menit.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.check_event_aftermath(context.application)
        except Exception as e:
            logger.warning(f"Event aftermath check failed: {e}")


async def news_prediction_callback(context):
    """
    Callback untuk membuat & mengirim prediksi arah emas (XAU/USD) menjelang
    event ekonomi high-impact rilis. Dipanggil berkala.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.check_news_predictions(context.application)
        except Exception as e:
            logger.warning(f"News prediction check failed: {e}")


async def news_prediction_settle_callback(context):
    """
    Callback untuk mengevaluasi prediksi news yang sudah lewat masa rilis
    (AI menilai benar/salah) & mengirim hasilnya. Dipanggil berkala.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.settle_news_predictions(context.application)
        except Exception as e:
            logger.warning(f"News prediction settle failed: {e}")


async def cache_cleanup_callback(context):
    """
    Bersihkan cache kedaluwarsa (memori + Supabase L2) secara berkala
    agar RAM proses bot tidak membengkak seiring waktu.

    Sekaligus men-flush aktivitas user (last_active_at + total_questions)
    ke Supabase secara batch — numpang job yang sudah ada, tanpa tambahan
    request per pesan dan tanpa wake-up tambahan di Render free tier.
    """
    try:
        await asyncio.to_thread(cleanup_all)
    except Exception as e:
        logger.warning(f"Cache cleanup failed: {e}")
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.flush_user_activity()
        except Exception as e:
            logger.warning(f"User activity flush failed: {e}")
        # Flush kuota harian per-user (persist) — batch, tanpa request per pesan.
        try:
            await bot_instance.flush_daily_usage()
        except Exception as e:
            logger.warning(f"Daily usage flush failed: {e}")
        # Notif admin saat semua AI provider down / pulih (rate-limited) —
        # tanpa ini, AI-down hanya tampil sebagai pesan error di sisi user.
        try:
            await bot_instance.notify_ai_outage(context.application)
        except Exception as e:
            logger.warning(f"AI outage check failed: {e}")


async def admin_online_callback(context):
    """Kirim notif 'bot online' ke admin (dipakai mode polling — setelah start)."""
    from utils.admin_alerts import notify_admins

    try:
        await notify_admins(
            context.bot,
            f"✅ *{BOT_NAME}* online — mode {BOT_RUN_MODE.upper()} (polling).",
        )
    except Exception as e:
        logger.warning(f"Notif admin online gagal: {e}")


async def session_alert_callback(context):
    """Kirim alert sesi market baru buka (Sydney/Tokyo/London/New York)."""
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.send_session_alerts(context.application)
        except Exception as e:
            logger.warning(f"Session alert check failed: {e}")


async def cot_prewarm_callback(context):
    """Pre-warm cache COT (CFTC) — jadwal COT_PREWARM_HOUR:MINUTE harian.

    Hanya berjalan saat di jendela rilis mingguan (Jumat malam s.d. Sabtu siang
    WIB — dicek di SchedulerJobsMixin.prewarm_cot_cache). Mengisi cache Supabase
    SEMUA instrumen COT sehingga /cot langsung instan tanpa download di tengah
    request user. Best-effort: kegagalan hanya di-log, tidak pernah raise.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.prewarm_cot_cache(
                context.application, max_instruments=COT_PREWARM_MAX_INSTRUMENTS
            )
        except Exception as e:
            logger.warning(f"COT pre-warm job gagal: {e}")


async def ai_usage_report_callback(context):
    """Kirim laporan pemakaian AI harian ke admin (token & request per provider).

    Data kumulatif sejak bot start (di engine.stats). Dikirim 1x/hari via job
    run_daily — 1 pesan, tanpa beban berarti di free tier.
    """
    from utils.admin_alerts import notify_admins
    from bot.messages import format_ai_usage_report

    try:
        bot_instance = context.application.bot_data.get("market_bot")
        if not bot_instance:
            return
        report = format_ai_usage_report(bot_instance.ai.get_stats())
        await notify_admins(context.bot, report)
    except Exception as e:
        logger.warning(f"AI usage report gagal: {e}")


def setup_scheduler(application: Application, bot: MarketBot):
    """
    Setup job queue untuk morning brief & notifikasi event ekonomi.
    
    Menggunakan JobQueue dari python-telegram-bot untuk scheduling.
    Morning brief dikirim setiap hari jam MORNING_BRIEF_HOUR, dan
    notifikasi event ekonomi (digest harian + reminder) bila ECONOMIC_ALERT_ENABLED.
    """
    # Simpan bot instance di bot_data untuk diakses oleh callback
    application.bot_data["market_bot"] = bot

    # Cek JobQueue tersedia
    if application.job_queue:
        # Jadwalkan morning brief setiap hari pada zona waktu yang dikonfigurasi.
        # Penting: python-telegram-bot v20 menginterpretasikan datetime.time naive
        # sebagai UTC, sehingga tanpa tzinfo brief akan terkirim jam 07:00 UTC
        # (bukan 07:00 WIB) di server yang memakai UTC.
        try:
            brief_tz = ZoneInfo(MORNING_BRIEF_TIMEZONE)
        except Exception:
            logger.warning(
                f"Invalid MORNING_BRIEF_TIMEZONE '{MORNING_BRIEF_TIMEZONE}', falling back to UTC"
            )
            brief_tz = ZoneInfo("UTC")
        brief_time = time(hour=MORNING_BRIEF_HOUR, minute=MORNING_BRIEF_MINUTE, tzinfo=brief_tz)
        application.job_queue.run_daily(
            morning_brief_callback,
            time=brief_time,
            name="morning_brief",
        )
        logger.info(
            f"Morning brief scheduled daily at {MORNING_BRIEF_HOUR:02d}:{MORNING_BRIEF_MINUTE:02d}"
        )

        # Kirim morning brief saat bot pertama kali jalan
        # (jika waktu sekarang masih pagi, lewatkan)
        now = datetime.now(brief_tz)
        brief_datetime = now.replace(
            hour=MORNING_BRIEF_HOUR,
            minute=MORNING_BRIEF_MINUTE,
            second=0,
            microsecond=0,
        )
        if now > brief_datetime and now < brief_datetime + timedelta(hours=1):
            # Baru lewat waktu brief, kirim sekarang
            logger.info("Sending initial morning brief...")
            application.job_queue.run_once(
                morning_brief_callback,
                when=5,  # Delay 5 detik
                name="initial_morning_brief",
            )

        # ===== Economic Event Alerts =====
        # Digest harian + reminder SEBELUM event + analisis aftermath SETELAH event
        # (dampak ke DXY + berita). Semua memakai daftar subscriber /alert yang sama.
        if ECONOMIC_ALERT_ENABLED:
            alert_time = time(hour=ECONOMIC_ALERT_DIGEST_HOUR, minute=ECONOMIC_ALERT_DIGEST_MINUTE, tzinfo=brief_tz)
            application.job_queue.run_daily(
                event_digest_callback,
                time=alert_time,
                name="event_digest",
            )
            logger.info(
                f"Economic event digest scheduled daily at {ECONOMIC_ALERT_DIGEST_HOUR:02d}:{ECONOMIC_ALERT_DIGEST_MINUTE:02d}"
            )

            application.job_queue.run_repeating(
                event_reminder_callback,
                interval=timedelta(minutes=ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES),
                first=30,  # Mulai 30 detik setelah start
                name="event_reminders",
            )
            logger.info(
                f"Economic event reminders scheduled every {ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES} minutes"
            )

            # Analisis aftermath (SETELAH rilis) — ikut gating ECONOMIC_ALERT_ENABLED
            # agar satu saklar mematikan seluruh notifikasi event. Dikontrol lebih
            # lanjut oleh EVENT_AFTERMATH_ENABLED di dalam check_event_aftermath.
            # Interval TERPISAH (default 30 mnt) dari reminder (15 mnt): aftermath
            # tidak butuh ketepatan waktu (event sudah rilis, jendela lookback 6 jam),
            # sehingga jarangnya pengecekan menghemat beban FRED + AI di hari tanpa news.
            application.job_queue.run_repeating(
                event_aftermath_callback,
                interval=timedelta(minutes=EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES),
                first=90,  # Mulai 90 detik setelah start
                name="event_aftermath",
            )
            logger.info(
                f"Event aftermath analysis scheduled every {EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES} minutes"
            )

        # ===== News Prediction (XAU/USD) =====
        # Prediksi arah emas sebelum event high-impact rilis + evaluasi setelah
        # rilis. Interval kecil (default 1 menit) agar prediksi mendekati T-5 menit.
        if NEWS_PREDICTION_ENABLED:
            application.job_queue.run_repeating(
                news_prediction_callback,
                interval=timedelta(minutes=NEWS_PREDICTION_CHECK_INTERVAL_MINUTES),
                first=45,  # Mulai 45 detik setelah start
                name="news_predictions",
            )
            application.job_queue.run_repeating(
                news_prediction_settle_callback,
                interval=timedelta(minutes=NEWS_PREDICTION_CHECK_INTERVAL_MINUTES),
                first=75,  # Mulai 75 detik setelah start
                name="news_prediction_settle",
            )
            logger.info(
                f"News predictions scheduled every {NEWS_PREDICTION_CHECK_INTERVAL_MINUTES} minutes"
            )

        # Bersihkan cache kedaluwarsa (memori + Supabase) setiap 10 menit
        application.job_queue.run_repeating(
            cache_cleanup_callback,
            interval=timedelta(minutes=10),
            first=120,  # Mulai 2 menit setelah start
            name="cache_cleanup",
        )
        logger.info("Cache cleanup scheduled every 10 minutes")

        # ===== Market Session Alerts =====
        # Notif "sesi market buka" (Sydney/Tokyo/London/New York) ke subscriber
        # morning brief. Tanpa AI — hanya cek jam & kirim. Interval 30 mnt sesuai
        # jendela deteksi "baru buka" (30 mnt) di utils/sessions.py.
        if SESSION_ALERT_ENABLED:
            application.job_queue.run_repeating(
                session_alert_callback,
                interval=timedelta(minutes=SESSION_ALERT_INTERVAL_MINUTES),
                first=150,  # Mulai 2,5 menit setelah start
                name="session_alerts",
            )
            logger.info(
                f"Market session alerts scheduled every {SESSION_ALERT_INTERVAL_MINUTES} minutes"
            )

        # ===== Pre-warm Cache COT (CFTC mingguan) =====
        # CFTC rilis laporan Jumat malam WIB. Job run_daily mengisi cache
        # Supabase semua instrumen COT di jendela rilis (Jumat >= 21:00 / Sabtu
        # < 12:00 — dicek di prewarm_cot_cache) sehingga /cot langsung instan
        # tanpa download di tengah request user. Tanpa AI untuk data lama
        # (reuse interpretasi yang ada) — 1x/minggu, ringan untuk free tier.
        if COT_PREWARM_ENABLED:
            prewarm_time = time(
                hour=COT_PREWARM_HOUR,
                minute=COT_PREWARM_MINUTE,
                tzinfo=brief_tz,
            )
            application.job_queue.run_daily(
                cot_prewarm_callback,
                time=prewarm_time,
                name="cot_prewarm",
            )
            logger.info(
                f"COT pre-warm scheduled daily at "
                f"{COT_PREWARM_HOUR:02d}:{COT_PREWARM_MINUTE:02d} ({MORNING_BRIEF_TIMEZONE})"
            )

        # ===== Laporan AI Usage Harian (ke admin) =====
        # Token & request per provider — 1 pesan/hari. Membantu pantau kuota
        # gratis sebelum kena limit (biasanya reset per hari di sisi provider).
        if AI_USAGE_REPORT_ENABLED:
            report_time = time(
                hour=AI_USAGE_REPORT_HOUR,
                minute=AI_USAGE_REPORT_MINUTE,
                tzinfo=brief_tz,
            )
            application.job_queue.run_daily(
                ai_usage_report_callback,
                time=report_time,
                name="ai_usage_report",
            )
            logger.info(
                f"AI usage report scheduled daily at "
                f"{AI_USAGE_REPORT_HOUR:02d}:{AI_USAGE_REPORT_MINUTE:02d} ({MORNING_BRIEF_TIMEZONE})"
            )

    else:
        logger.warning("JobQueue not available, morning brief & event alerts scheduling disabled. Install pytz if needed.")


def register_handlers(application: Application, bot: MarketBot):
    """
    Daftarkan semua handler (perintah, callback, pesan) ke application.
    Dipakai bersama oleh mode polling & webhook agar tidak dobel.
    """
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("about", bot.about_command))
    application.add_handler(CommandHandler("status", bot.status_command))
    application.add_handler(CommandHandler("clear", bot.clear_command))
    application.add_handler(CommandHandler("settings", bot.settings_command))
    application.add_handler(CommandHandler("memory", bot.memory_command))
    # Admin-only (ADMIN_USER_IDS) — tidak dipajang di menu command bot
    application.add_handler(CommandHandler("broadcast", bot.broadcast_command))
    application.add_handler(CommandHandler("stats", bot.stats_command))
    application.add_handler(CommandHandler("usage", bot.usage_command))
    application.add_handler(CommandHandler("syncmenu", bot.syncmenu_command))
    application.add_handler(CommandHandler("morning", bot.morning_brief_command))
    application.add_handler(CommandHandler("sentiment", bot.sentiment_command))
    application.add_handler(CommandHandler("calendar", bot.calendar_command))
    application.add_handler(CommandHandler("aftermath", bot.aftermath_command))
    application.add_handler(CommandHandler("prediksi", bot.prediksi_command))
    application.add_handler(CommandHandler("alert", bot.alert_command))
    application.add_handler(CommandHandler("overview", bot.overview_command))
    application.add_handler(CommandHandler("sentimen", bot.retail_sentiment_command))
    application.add_handler(CommandHandler("subscribe", bot.subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", bot.unsubscribe_command))
    application.add_handler(CommandHandler("risk", bot.risk_command))
    application.add_handler(CommandHandler("pivot", bot.pivot_command))
    application.add_handler(CommandHandler("map", bot.map_command))
    application.add_handler(CommandHandler("journal", bot.journal_command))
    application.add_handler(CommandHandler("watchlist", bot.watchlist_command))
    # /plan memakai ConversationHandler (alur tanya-jawab /plan setup). Wajib
    # didaftarkan SEBELUM MessageHandler teks umum di bawah agar pesan user saat
    # percakapan aktif diarahkan ke state handler-nya.
    from bot.conversation_plan import build_plan_setup_conversation

    application.add_handler(
        build_plan_setup_conversation(entry=bot.plan_conversation_entry)
    )
    application.add_handler(CommandHandler("cot", bot.cot_command))
    # Admin-only (ADMIN_USER_IDS) — pemicu manual pre-warm cache COT
    application.add_handler(CommandHandler("cotrefresh", bot.cotrefresh_command))
    application.add_handler(CallbackQueryHandler(bot.handle_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
    )


def build_application():
    """Buat Application + MarketBot + scheduler, lalu daftarkan semua handler."""
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    bot = MarketBot()
    setup_scheduler(application, bot)
    register_handlers(application, bot)

    # Error handler global: exception handler apa pun di-log + user dikabari
    # (tanpa ini kegagalan hanya tampak diam di sisi user).
    application.add_error_handler(handle_bot_error)

    # Mulai streaming harga OANDA real-time (daemon thread; no-op bila
    # OANDA_API_KEY belum di-set). Harga live langsung tersedia untuk
    # jawaban harga & tidak membebani REST quota.
    start_stream()

    return application


def _notify_admin_online(loop, application, mode: str) -> None:
    """Kirim notif 'bot online' ke admin (best-effort, tidak pernah raise)."""
    from utils.admin_alerts import notify_admins

    try:
        loop.run_until_complete(
            notify_admins(
                application.bot,
                f"✅ *{BOT_NAME}* online — mode {mode.upper()}.",
            )
        )
    except Exception as e:
        logger.warning(f"Notif admin online gagal: {e}")


def _notify_admin_setup_failed(loop, application) -> None:
    """Kirim notif 'setup webhook gagal' ke admin (best-effort)."""
    from utils.admin_alerts import notify_admins

    try:
        loop.run_until_complete(
            notify_admins(
                application.bot,
                "⚠️ *Bot gagal start (setup webhook).*\n\n"
                "Bot dalam mode idle — perbaiki environment (token/URL/network) "
                "lalu redeploy. Cek log Render untuk detail.",
            )
        )
    except Exception as e:
        logger.warning(f"Notif admin setup gagal: {e}")


def run_polling():
    """Jalankan bot dengan metode polling (untuk development)."""
    logger.info("Starting bot in polling mode...")

    application = build_application()

    # Notif 'bot online' ke admin setelah polling benar-benar start — job
    # queue baru berjalan DI DALAM run_polling, jadi run_once baru di-fetch
    # setelah itu (when=5 dtk memberi waktu initialize/start selesai).
    if application.job_queue:
        application.job_queue.run_once(
            admin_online_callback,
            when=5,
            name="admin_online_notif",
        )

    # Start polling
    logger.info("Bot is polling... Press Ctrl+C to stop.")
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


def resolve_run_mode(bot_run_mode: str, is_cloud: bool, webhook_url: str) -> str:
    """
    Tentukan mode menjalankan bot (murni — mudah di-test).

    Prioritas:
    1. BOT_RUN_MODE eksplisit ("webhook" / "polling") — untuk JustRunMy
       pengguna memilih secara eksplisit (platform tidak terdeteksi otomatis).
    2. auto: webhook bila platform cloud terdeteksi (Railway/Render/Koyeb)
       ATAU WEBHOOK_URL terisi (cara deteksi JustRunMy yang sudah di-set
       manual); selain itu polling (dev lokal / JustRunMy tanpa port mapping).

    Returns:
        "webhook" atau "polling".
    """
    mode = (bot_run_mode or "auto").strip().lower()
    if mode in ("webhook", "polling"):
        return mode
    # auto
    if is_cloud or webhook_url:
        return "webhook"
    return "polling"


def _log_update_error(task: asyncio.Task) -> None:
    """Log error tak terduga dari task proses update webhook (anti silent-fail)."""
    exc = task.exception()
    if exc is not None:
        logger.error(f"Gagal memproses update webhook: {exc}")


# ===================== SETUP WEBHOOK RETRY (transient-safe) =====================
# Telegram API kadang membalas error transient (500/502/503/NetworkError) —
# mis. sesaat saat deploy / beban edge. Tanpa retry, SATU kegagalan sesaat di
# fase setup (initialize/set_webhook) membuat bot idle permanen sampai redeploy
# manual. Retry per-fase dengan backoff exponential memakai asyncio.sleep agar
# event loop tetap responsif (server webhook & /health tidak ikut membeku).
SETUP_RETRY_ATTEMPTS = 5
SETUP_RETRY_BASE_DELAY = 5.0  # detik; backoff 5→10→20→40 (total ±75s per fase)


async def _setup_webhook_with_retry(application) -> Optional[Exception]:
    """initialize → start → set_webhook dengan retry per-fase.

    initialize() & set_webhook() di-retry (error transient Telegram 500/502/
    503/NetworkError); start() cukup SEKALI (memulai JobQueue — memanggil
    ulang bisa dobel job scheduler).

    Returns:
        None bila sukses; Exception terakhir bila semua percobaan gagal
        (pemanggil memutuskan idle, bukan crash-loop).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, SETUP_RETRY_ATTEMPTS + 1):
        try:
            await application.initialize()
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            logger.warning(
                f"initialize attempt {attempt}/{SETUP_RETRY_ATTEMPTS} gagal: {e}"
            )
            if attempt < SETUP_RETRY_ATTEMPTS:
                await asyncio.sleep(SETUP_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

    if last_exc is not None:
        return last_exc

    await application.start()

    for attempt in range(1, SETUP_RETRY_ATTEMPTS + 1):
        try:
            await application.bot.set_webhook(
                url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
                secret_token=WEBHOOK_SECRET,
            )
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            logger.warning(
                f"set_webhook attempt {attempt}/{SETUP_RETRY_ATTEMPTS} gagal: {e}"
            )
            if attempt < SETUP_RETRY_ATTEMPTS:
                await asyncio.sleep(SETUP_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

    return last_exc


def build_webhook_app(application, url_path: str, secret: Optional[str]):
    """Buat aiohttp app: GET /health (200) + POST /<url_path> (webhook Telegram).

    Menggantikan server tornado bawaan PTB. Kenapa perlu diganti:
    - Server tornado PTB hanya menerima POST (SUPPORTED_METHODS=("POST",)) →
      GET /health dari UptimeRobot / Render health check selalu 405/404, jadi
      keep-alive tidak pernah mendapat 200 (monitor dianggap DOWN).
    - Server kita melayani GET /health → 200 JSON (keep-alive & healthCheckPath
      Render) DAN POST /<token> → Update.de_json + application.process_update
      (API publik PTB, perilaku webhook identik dengan bawaan).

    Dipisah (murni) agar mudah di-test tanpa Application sungguhan.
    """
    from aiohttp import web
    from telegram import Update
    from utils.health_server import build_health_payload

    async def health_handler(request):
        return web.json_response(build_health_payload(), status=200)

    async def webhook_handler(request):
        if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return web.Response(status=403)
        try:
            data = json.loads(await request.text())
        except Exception:
            return web.Response(status=400)
        if not getattr(application, "running", True):
            # Server sudah bind (port terbuka) tapi application belum siap
            # (masih initialize/start) — minta Telegram retry nanti supaya
            # update tidak hilang.
            return web.Response(status=503)
        update = Update.de_json(data, application.bot)
        if update is not None:
            # Fire-and-forget: balas 200 SEGERA. Pipeline AI bisa >20 dtk — kalau
            # di-await, Telegram timeout ~20 dtk lalu RETRY webhook → update
            # diproses dobel (jawaban ganda). process_update sudah mengatur
            # semaphore (concurrent_updates) & mengarahkan error handler ke
            # process_error, jadi aman dijalankan di background.
            task = asyncio.create_task(application.process_update(update))
            # PENTING: simpan referensi KUAT ke task. asyncio hanya memegang
            # weak reference ke task — task yang pending (pipeline AI bisa
            # 5-30 dtk dengan banyak await) tanpa referensi eksternal bisa
            # di-GC di tengah jalan ("Task was destroyed but it is pending!")
            # dan update hilang DIAM-DIAM (Telegram sudah dapat 200).
            # Set berisi referensi kuat; discard otomatis saat task selesai.
            bg_tasks = getattr(application, "_bg_tasks", None)
            if bg_tasks is None:
                bg_tasks = set()
                application._bg_tasks = bg_tasks
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)
            task.add_done_callback(_log_update_error)
        return web.Response(status=200)

    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post(f"/{url_path}", webhook_handler)
    return app


def run_webhook():
    """Jalankan bot dengan webhook (untuk production/deploy).

    Di Railway/Render: PORT & WEBHOOK_URL diisi otomatis oleh platform.
    Di JustRunMy: set manual di panel — PORT=8080 (sesuai mapping port HTTPS
    yang dibuat) + WEBHOOK_URL=https://<app>.justrunmy.app + BOT_RUN_MODE=webhook.

    Server webhook memakai aiohttp (build_webhook_app) — selain POST webhook
    Telegram, endpoint GET /health publik (200) tersedia untuk UptimeRobot
    keep-alive & Render healthCheckPath.
    """
    from aiohttp import web

    logger.info(f"Starting bot in webhook mode on port {PORT}...")

    # Validasi WEBHOOK_URL SEBELUM bind — di Render diisi otomatis
    # (RENDER_EXTERNAL_URL), di JustRunMy manual. Kosong di mode webhook =
    # salah konfigurasi: gagal dengan pesan jelas (main() akan idle, bukan
    # crash-loop), bukan set_webhook(url="/<token>") yang menyesatkan.
    if not WEBHOOK_URL:
        logger.error("=" * 60)
        logger.error("WEBHOOK_URL KOSONG tapi mode webhook terpilih!")
        logger.error("Render: cek RENDER_EXTERNAL_URL terbaca (IS_RENDER).")
        logger.error(
            "JustRunMy: set WEBHOOK_URL=https://<app>.justrunmy.app "
            "+ PORT sesuai mapping HTTPS di panel + BOT_RUN_MODE=webhook."
        )
        logger.error("=" * 60)
        raise RuntimeError("WEBHOOK_URL kosong di mode webhook")
    logger.info(f"Webhook URL: {WEBHOOK_URL}/{TELEGRAM_TOKEN[:8]}...")

    application = build_application()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = None
    try:
        # 1) BIND SERVER PALING DULU — port langsung terbuka di 0.0.0.0:PORT
        #    agar port-scan Render & health check /health sukses TANPA menunggu
        #    initialize/set_webhook (butuh network round-trip, bisa 3-10 dtk).
        #    Sampai application siap, POST webhook dibalas 503 (Telegram retry
        #    otomatis, update tidak hilang) dan GET /health tetap 200.
        app = build_webhook_app(application, TELEGRAM_TOKEN, WEBHOOK_SECRET)
        runner = web.AppRunner(app, access_log=None)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, WEBHOOK_LISTEN, PORT)
        loop.run_until_complete(site.start())
        logger.info(
            f"Webhook aiohttp aktif: POST /<token> + GET /health di "
            f"http://{WEBHOOK_LISTEN}:{PORT}"
        )

        # 2) initialize (→ post_init) → start → daftar webhook ke Telegram.
        # Kegagalan SETUP (bukan runtime) TIDAK boleh crash-loop: server sudah
        # bind, biarkan hidup (idle) agar port Render tetap terbuka & /health
        # 200 untuk diagnosa — Telegram belum punya webhook (update tak masuk)
        # sampai env diperbaiki + redeploy.
        #
        # Retry PER-FASE (lihat _setup_webhook_with_retry): Telegram kadang
        # membalas error transient (500/502/503/NetworkError) — tanpa retry,
        # satu kegagalan sesaat membuat bot idle permanen sampai redeploy
        # manual. Backoff memakai asyncio.sleep agar event loop tetap
        # responsif (server webhook + /health tidak ikut membeku).
        global BOT_STARTED
        try:
            setup_exc = loop.run_until_complete(_setup_webhook_with_retry(application))
        except Exception:
            BOT_STARTED = False
            logger.exception(
                "Setup webhook gagal (initialize/start/set_webhook) — server "
                "dibiarkan hidup untuk diagnosa; perbaiki env lalu redeploy."
            )
        else:
            if setup_exc is not None:
                BOT_STARTED = False
                logger.error(
                    f"Setup webhook gagal setelah {SETUP_RETRY_ATTEMPTS} percobaan "
                    f"({setup_exc}) — server dibiarkan hidup untuk diagnosa; "
                    "perbaiki env lalu redeploy."
                )
                _notify_admin_setup_failed(loop, application)
            else:
                logger.info(
                    f"Webhook terdaftar di Telegram: {WEBHOOK_URL}/{TELEGRAM_TOKEN[:8]}..."
                )
                _notify_admin_online(loop, application, "webhook")

        # Blok sampai sinyal stop. PTB 20.7 TIDAK punya Application.idle()
        # (method dihapus; run_polling/run_webhook bawaan memakai loop.run_forever
        # di __run, dan stop_running() memanggil loop.stop()). Kita replikasi:
        # run_forever() + handler sinyal (SIGTERM/SIGINT → loop.stop()) agar
        # shutdown platform / Ctrl+C menghentikan loop secara graceful, lalu
        # blok finally membersihkan aiohttp runner + application.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
            try:
                loop.add_signal_handler(sig, loop.stop)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows / loop non-main-thread tanpa add_signal_handler.
                # Shutdown hanya via Ctrl+C / kill paksa platform — sama seperti
                # fallback PTB, tapi catat supaya tidak membingungkan saat debug.
                logger.warning(
                    f"Signal handler SIG {sig} tidak terpasang — shutdown graceful nonaktif."
                )
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.debug("Webhook server dihentikan via KeyboardInterrupt.")
    finally:
        if runner is not None:
            try:
                loop.run_until_complete(runner.cleanup())
            except Exception:
                pass
        for cleanup in (application.stop, application.shutdown):
            try:
                loop.run_until_complete(cleanup())
            except Exception:
                pass
        loop.close()


async def _token_valid_async(token: str) -> bool:
    """Cek token Telegram valid via getMe (tanpa crash).

    Hanya token yang DITOLAK Telegram (InvalidToken) dianggap fatal; error
    jaringan/sementara (NetworkError, TimedOut, dll) → lanjut (bot tetap
    mencoba jalan dan error nyata tampil di log).

    Catatan PTB 20.x: exception untuk token ditolak adalah
    telegram.error.InvalidToken (bukan Unauthorized yang ada di PTB <20).
    """
    from telegram import Bot
    from telegram.error import InvalidToken, TelegramError
    try:
        # PTB 20.x: timeout dioper per-method (bukan di Bot.__init__).
        # Timeout DIKECILKAN (5s): token yang ditolak gagal cepat (HTTP 401),
        # jadi timeout panjang hanya berguna untuk jaringan hang — dan justru
        # menunda bind port webhook melewati jendela port-scan Render.
        bot = Bot(token=token)
        me = await bot.get_me(read_timeout=5, connect_timeout=5)
        logger.info(f"Token valid — bot @{me.username} (id {me.id})")
        return True
    except InvalidToken:
        logger.error("Token Telegram DITOLAK (InvalidToken) — cek token di @BotFather.")
        return False
    except TelegramError as e:
        logger.warning(f"Gagal memvalidasi token ({type(e).__name__}, sementara): {e}")
        return True
    except Exception as e:
        # Hanya token yang DITOLAK eksplisit yang fatal. Error lain (jaringan,
        # non-Telegram, dll.) dianggap sementara — bot tetap coba jalan dan
        # error nyata tetap tampil di log.
        logger.warning(f"Gagal memvalidasi token ({type(e).__name__}, dianggap sementara): {e}")
        return True
    finally:
        try:
            await bot.close()
        except Exception:
            pass


def _idle_forever() -> None:
    """Jaga proses tetap hidup (health server daemon tetap melayani /health).

    Dipakai saat konfigurasi kritis (token) bermasalah — mencegah crash-loop
    restart di platform (JustRunMy dkk). Diagnosis lewat log & /health tetap
    tersedia; perbaiki environment lalu redeploy.
    """
    logger.error(
        "Bot TIDAK berjalan (lihat error di atas). Proses dijaga hidup agar "
        "container tidak restart-loop. Perbaiki environment lalu redeploy."
    )
    try:
        while True:
            _time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    """Entry point utama."""
    # Health endpoint DULU (daemon thread) — selalu hidup supaya platform tidak
    # men-restart berulang saat konfigurasi salah (endpoint /health tetap OK).
    try:
        start_health_server()
    except Exception as e:
        logger.warning(f"Health endpoint gagal diinisialisasi: {e}")

    # Validasi token: kosong ATAU ditolak Telegram → JANGAN sys.exit (itu yang
    # memicu crash-loop restart). Log error jelas + proses tetap hidup.
    if not TELEGRAM_TOKEN:
        logger.error("=" * 60)
        logger.error("TELEGRAM_BOT_TOKEN TIDAK DITEMUKAN di environment!")
        logger.error("Isi env TELEGRAM_BOT_TOKEN di panel JustRunMy lalu redeploy.")
        logger.error("=" * 60)
        _idle_forever()
        return
    if not asyncio.run(_token_valid_async(TELEGRAM_TOKEN)):
        _idle_forever()
        return

    # Pilih mode: BOT_RUN_MODE eksplisit > auto-detect (cloud / WEBHOOK_URL) >
    # polling. Flag --webhook tetap didukung sebagai override cepat.
    mode = resolve_run_mode(BOT_RUN_MODE, IS_CLOUD, WEBHOOK_URL)
    if "--webhook" in sys.argv:
        mode = "webhook"

    if mode == "webhook":
        logger.info(
            f"Webhook mode terpilih (BOT_RUN_MODE={BOT_RUN_MODE!r}, "
            f"IS_CLOUD={IS_CLOUD}, WEBHOOK_URL={'set' if WEBHOOK_URL else 'kosong'})"
        )
        try:
            run_webhook()
        except Exception:
            if BOT_STARTED:
                # Kegagalan runtime setelah bot berjalan — biarkan platform
                # me-restart (health tidak boleh tampak sehat saat bot mati).
                logger.exception("Bot crash saat berjalan — platform akan me-restart")
                raise
            logger.exception("Webhook mode gagal start — bot tidak jalan")
            _idle_forever()
    else:
        logger.info(
            f"Polling mode terpilih (BOT_RUN_MODE={BOT_RUN_MODE!r}, "
            f"WEBHOOK_URL={'set' if WEBHOOK_URL else 'kosong'}) — "
            "long polling jalan tanpa perlu port publik/URL."
        )
        try:
            run_polling()
        except Exception:
            if BOT_STARTED:
                # Kegagalan runtime setelah bot berjalan — biarkan platform
                # me-restart (health tidak boleh tampak sehat saat bot mati).
                logger.exception("Bot crash saat berjalan — platform akan me-restart")
                raise
            logger.exception("Polling mode gagal start — bot tidak jalan")
            _idle_forever()


if __name__ == "__main__":
    main()
