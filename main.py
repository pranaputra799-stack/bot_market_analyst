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
    PRICE_ALERT_CHECK_MINUTES,
    BOT_USERNAME,
    BOT_NAME,
    PORT,
    WEBHOOK_URL,
    WEBHOOK_LISTEN,
    WEBHOOK_SECRET,
    IS_CLOUD,
)
from bot.handlers import MarketBot
from data.cache import cache, cleanup_all

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
        BotCommand("chart", "📈 Grafik harga"),
        BotCommand("overview", "🌍 Overview pasar"),
        BotCommand("subscribe", "🔔 Langganan Morning Brief"),
        BotCommand("unsubscribe", "🔕 Berhenti langganan"),
        BotCommand("about", "ℹ️ Tentang bot ini"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info(f"Bot {BOT_NAME} (@{BOT_USERNAME}) started successfully!")


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
    application.add_handler(CommandHandler("morning", bot.morning_brief_command))
    application.add_handler(CommandHandler("sentiment", bot.sentiment_command))
    application.add_handler(CommandHandler("calendar", bot.calendar_command))
    application.add_handler(CommandHandler("alert", bot.alert_command))
    application.add_handler(CommandHandler("pa", bot.price_alert_command))
    application.add_handler(CommandHandler("chart", bot.chart_command))
    application.add_handler(CommandHandler("overview", bot.overview_command))
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


def run_webhook():
    """Jalankan bot dengan webhook (untuk production/deploy).
    
    Di Railway:
    - PORT = 8080 (dari env Railway)
    - WEBHOOK_URL = https://{RAILWAY_PUBLIC_DOMAIN} (auto-detect)
    - Listen di 0.0.0.0
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

    # Auto-detect: cloud -> webhook, else -> polling lokal
    if IS_CLOUD:
        logger.info("Cloud environment detected! Using webhook mode.")
        run_webhook()
    elif "--webhook" in sys.argv:
        run_webhook()
    else:
        run_polling()


if __name__ == "__main__":
    main()
