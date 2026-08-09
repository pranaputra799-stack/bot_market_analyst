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
import logging
import sys
import os
from datetime import datetime, time, timedelta

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
    NEWS_PREDICTION_ENABLED,
    NEWS_PREDICTION_CHECK_INTERVAL_MINUTES,
    PRICE_ALERT_CHECK_MINUTES,
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
from data.cache import cache, cleanup_all
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


async def post_init(application: Application):
    """Setup setelah bot initialize."""
    # Set commands untuk menu bot
    commands = [
        BotCommand("start", "🚀 Mulai bot"),
        BotCommand("help", "❓ Bantuan & panduan"),
        BotCommand("morning", "🌅 Morning Brief harian"),
        BotCommand("sentiment", "🧠 Sentimen pasar"),
        BotCommand("calendar", "📅 Kalender Ekonomi"),
        BotCommand("alert", "🔔 Notifikasi event ekonomi"),
        BotCommand("pa", "🎯 Alert harga (target)"),
        BotCommand("status", "✅ Status sistem & API"),
        BotCommand("clear", "🧹 Bersihkan konteks"),
        BotCommand("memory", "🧠 Lihat & hapus riwayat percakapan"),
        BotCommand("chart", "📈 Grafik harga"),
        BotCommand("overview", "🌍 Overview pasar"),
        BotCommand("sentimen", "🧠 Sentimen retail (OANDA)"),
        BotCommand("watch", "👀 Watchlist instrumen"),
        BotCommand("riwayat", "📜 Riwayat harga tersimpan"),
        BotCommand("subscribe", "🔔 Langganan Morning Brief"),
        BotCommand("unsubscribe", "🔕 Berhenti langganan"),
        BotCommand("about", "ℹ️ Tentang bot ini"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info(f"Bot {BOT_NAME} (@{BOT_USERNAME}) started successfully!")

    # Muat state persisten dari Supabase — alert harga (/pa) & subscriber
    # notifikasi event (/alert). Dulunya RAM-only, jadi hilang saat restart;
    # sekarang dimuat ulang agar alert lama tetap aktif setelah deploy.
    try:
        alerts = await db.get_price_alerts_async()
        if alerts:
            application.bot_data["price_alerts"] = alerts
        subscribers = await db.get_event_alert_subscribers_async()
        if subscribers:
            application.bot_data["event_alert_subscribers"] = subscribers
        notified = await db.get_event_alert_notified_async()
        if notified:
            application.bot_data["event_alert_notified"] = notified
        logger.info(
            f"Loaded persistent state: {len(alerts)} price alerts, "
            f"{len(subscribers)} event subscribers, {len(notified)} notified keys"
        )
    except Exception as e:
        logger.warning(f"Gagal memuat state persisten dari database: {e}")


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


async def price_alert_callback(context):
    """
    Callback untuk mengecek alert harga per-user (/pa).
    Dipanggil berkala setiap PRICE_ALERT_CHECK_MINUTES menit.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.check_price_alerts(context.application)
        except Exception as e:
            logger.warning(f"Price alert check failed: {e}")


async def price_history_recorder_callback(context):
    """
    Callback untuk merekam snapshot harga watchlist user (fitur /riwayat).
    Dipanggil berkala setiap 30 menit. Data di-cache data layer (OANDA
    real-time TTL 30 dtk) sehingga biaya request sangat kecil.
    """
    bot_instance = context.application.bot_data.get("market_bot")
    if bot_instance:
        try:
            await bot_instance.record_price_history(context.application)
        except Exception as e:
            logger.warning(f"Price history recorder failed: {e}")


async def price_history_cleanup_callback(context):
    """
    Bersihkan snapshot harga yang lebih tua dari 30 hari (perkecil ukuran tabel).
    Dipanggil harian.
    """
    try:
        await asyncio.to_thread(db.delete_old_price_history, 30)
    except Exception as e:
        logger.warning(f"Price history cleanup failed: {e}")


async def cache_cleanup_callback(context):
    """
    Bersihkan cache kedaluwarsa (memori + Supabase L2) secara berkala
    agar RAM proses bot tidak membengkak seiring waktu.
    """
    try:
        await asyncio.to_thread(cleanup_all)
    except Exception as e:
        logger.warning(f"Cache cleanup failed: {e}")


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
            application.job_queue.run_repeating(
                event_aftermath_callback,
                interval=timedelta(minutes=ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES),
                first=90,  # Mulai 90 detik setelah start
                name="event_aftermath",
            )
            logger.info(
                f"Event aftermath analysis scheduled every {ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES} minutes"
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

        # ===== Price Alerts =====
        # Alert harga per-user (/pa) diperiksa berkala; lebih cepat dari interval
        # event reminder karena harga bergerak terus.
        application.job_queue.run_repeating(
            price_alert_callback,
            interval=timedelta(minutes=PRICE_ALERT_CHECK_MINUTES),
            first=60,  # Mulai 60 detik setelah start
            name="price_alerts",
        )
        logger.info(
            f"Price alerts scheduled every {PRICE_ALERT_CHECK_MINUTES} minutes"
        )

        # Bersihkan cache kedaluwarsa (memori + Supabase) setiap 10 menit
        application.job_queue.run_repeating(
            cache_cleanup_callback,
            interval=timedelta(minutes=10),
            first=120,  # Mulai 2 menit setelah start
            name="cache_cleanup",
        )
        logger.info("Cache cleanup scheduled every 10 minutes")

        # ===== Riwayat Harga (fitur /watch & /riwayat) =====
        # Rekam snapshot harga watchlist user setiap 30 menit; bersihkan data
        # lebih tua dari 30 hari sekali sehari (04:00).
        application.job_queue.run_repeating(
            price_history_recorder_callback,
            interval=timedelta(minutes=30),
            first=300,  # Mulai 5 menit setelah start (beri waktu stream terkoneksi)
            name="price_history_recorder",
        )
        logger.info("Price history recorder scheduled every 30 minutes")

        cleanup_time = time(hour=4, minute=0, tzinfo=brief_tz)
        application.job_queue.run_daily(
            price_history_cleanup_callback,
            time=cleanup_time,
            name="price_history_cleanup",
        )
        logger.info("Price history cleanup scheduled daily at 04:00")
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
    application.add_handler(CommandHandler("morning", bot.morning_brief_command))
    application.add_handler(CommandHandler("sentiment", bot.sentiment_command))
    application.add_handler(CommandHandler("calendar", bot.calendar_command))
    application.add_handler(CommandHandler("aftermath", bot.aftermath_command))
    application.add_handler(CommandHandler("prediksi", bot.prediksi_command))
    application.add_handler(CommandHandler("alert", bot.alert_command))
    application.add_handler(CommandHandler("pa", bot.price_alert_command))
    application.add_handler(CommandHandler("chart", bot.chart_command))
    application.add_handler(CommandHandler("overview", bot.overview_command))
    application.add_handler(CommandHandler("sentimen", bot.retail_sentiment_command))
    application.add_handler(CommandHandler("watch", bot.watch_command))
    application.add_handler(CommandHandler("riwayat", bot.history_command))
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


def run_webhook():
    """Jalankan bot dengan webhook (untuk production/deploy).

    Di Railway/Render: PORT & WEBHOOK_URL diisi otomatis oleh platform.
    Di JustRunMy: set manual di panel — PORT=8080 (sesuai mapping port HTTPS
    yang dibuat) + WEBHOOK_URL=https://<app>.justrunmy.app + BOT_RUN_MODE=webhook.
    """
    logger.info(f"Starting bot in webhook mode on port {PORT}...")
    logger.info(f"Webhook URL: {WEBHOOK_URL}/{TELEGRAM_TOKEN[:8]}...")

    application = build_application()

    # Setup webhook
    application.run_webhook(
        listen=WEBHOOK_LISTEN,
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
        secret_token=WEBHOOK_SECRET,  # Security: validasi webhook request
    )


def main():
    """Entry point utama."""
    # Validate token
    if not TELEGRAM_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN tidak ditemukan!\n"
            "Buat file .env dengan isi:\n"
            "TELEGRAM_BOT_TOKEN=your_token_here"
        )
        sys.exit(1)

    # Health endpoint (aiohttp daemon thread, port terpisah HEALTH_PORT) untuk
    # uptime monitoring / Docker healthcheck. Opsional — nonaktifkan via
    # HEALTH_ENDPOINT_ENABLED=false. Gagal start tidak menghentikan bot.
    try:
        start_health_server()
    except Exception as e:
        logger.warning(f"Health endpoint gagal diinisialisasi: {e}")

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
        run_webhook()
    else:
        logger.info(
            f"Polling mode terpilih (BOT_RUN_MODE={BOT_RUN_MODE!r}, "
            f"WEBHOOK_URL={'set' if WEBHOOK_URL else 'kosong'}) — "
            "long polling jalan tanpa perlu port publik/URL."
        )
        run_polling()


if __name__ == "__main__":
    main()
