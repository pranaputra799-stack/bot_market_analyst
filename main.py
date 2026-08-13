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
from telegram import BotCommand

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
    # Set commands untuk menu bot
    commands = [
        BotCommand("start", "🚀 Mulai bot"),
        BotCommand("help", "❓ Bantuan & panduan"),
        BotCommand("morning", "🌅 Morning Brief harian"),
        BotCommand("sentiment", "🧠 Sentimen pasar"),
        BotCommand("calendar", "📅 Kalender Ekonomi"),
        BotCommand("aftermath", "🎯 Analisis dampak event (contoh: /aftermath nfp)"),
        BotCommand("prediksi", "🎯 Win rate prediksi news (XAU/USD)"),
        BotCommand("alert", "🔔 Notifikasi event ekonomi"),
        BotCommand("status", "✅ Status sistem & API"),
        BotCommand("clear", "🧹 Bersihkan konteks"),
        BotCommand("memory", "🧠 Lihat & hapus riwayat percakapan"),
        BotCommand("overview", "🌍 Overview pasar"),
        BotCommand("sentimen", "🧠 Sentimen retail (OANDA)"),
        BotCommand("subscribe", "🔔 Langganan Morning Brief"),
        BotCommand("unsubscribe", "🔕 Berhenti langganan"),
        BotCommand("about", "ℹ️ Tentang bot ini"),
    ]
    # Kegagalan set menu perintah TIDAK boleh mematikan bot (mis. transient
    # network error / token bermasalah) — log & lanjut agar bot tetap merespons.
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"set_my_commands gagal (bot tetap jalan): {e}")

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
    application.add_handler(CommandHandler("memory", bot.memory_command))
    # Admin-only (ADMIN_USER_IDS) — tidak dipajang di menu command bot
    application.add_handler(CommandHandler("broadcast", bot.broadcast_command))
    application.add_handler(CommandHandler("stats", bot.stats_command))
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


def run_polling():
    """Jalankan bot dengan metode polling (untuk development)."""
    logger.info("Starting bot in polling mode...")

    application = build_application()

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
        try:
            loop.run_until_complete(application.initialize())
            loop.run_until_complete(application.start())
            loop.run_until_complete(
                application.bot.set_webhook(
                    url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
                    secret_token=WEBHOOK_SECRET,
                )
            )
        except Exception:
            global BOT_STARTED
            BOT_STARTED = False
            logger.exception(
                "Setup webhook gagal (initialize/start/set_webhook) — server "
                "dibiarkan hidup untuk diagnosa; perbaiki env lalu redeploy."
            )
        else:
            logger.info(
                f"Webhook terdaftar di Telegram: {WEBHOOK_URL}/{TELEGRAM_TOKEN[:8]}..."
            )

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
