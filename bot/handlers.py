"""
Telegram Bot Handlers - Agregator MarketBot (mixin per domain).
Method dipecah ke bot/handlers_utils.py + mixin per domain; file ini
menggabungkannya & me-re-export nama publik agar import lama tetap jalan.
"""
from bot.messages import (
    WELCOME_MESSAGE,
    HELP_MESSAGE,
    ABOUT_MESSAGE,
    STATUS_MESSAGE_TEMPLATE,
    get_ai_providers_status,
    get_data_sources_status,
    get_analysis_engine_status,
)
from config.settings import (
    MORNING_BRIEF_TIMEZONE,
    ADMIN_USER_IDS,
    ENABLE_MULTI_AGENT,
    USER_DAILY_QUOTA,
)
from ai.engine import AIFallbackEngine
from analysis.director import AnalysisDirector
from telegram.ext import (
    Application,
    ContextTypes,
)
from utils.chart_generator import ChartGenerator
from typing import Dict
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from data.macro_data import MacroDataFetcher
from data.market_data import MarketDataAggregator
from data.news_data import NewsFetcher
from data.news_predictions import NewsPredictionStore
from analysis.sentiment import SentimentAnalyzer
import asyncio
from data.cache import cache
from data.conversation_memory import get_context, get_history, clear
from datetime import datetime, timezone
from data.database import db
import time
import logging

from bot.handlers_utils import (
    TG_MAX_MESSAGE_CHARS,
    split_long_text,
    _strip_provider_prefix,
    strip_markdown_asterisks,
    label_to_symbol,
    MENU_KEYBOARD_LABELS,
    MENU_KEYBOARD_ACTIONS,
    _menu_reply_keyboard,
    _main_menu_inline_keyboard,
    _quick_action_keyboard,
    _strip_reply_markup,
    detect_fast_price_query,
    MEMORY_USAGE,
    safe_reply_text,
    safe_send_message,
    safe_edit_message_text,
    _edit_progress_message,
)
from bot.scheduler_jobs import SchedulerJobsMixin
from bot.commands_market import MarketCommandsMixin
from bot.commands_journal import JournalCommandsMixin
from bot.commands_watchlist import WatchlistCommandsMixin
from bot.commands_plan import PlanCommandsMixin
from bot.commands_cot import CotCommandsMixin
from bot.message_flow import MessageFlowMixin
from bot.callback_flow import CallbackFlowMixin

# Nama publik yang di-re-export (test lama & modul lain mengimpor dari
# bot.handlers) — jangan dihapus ruff meski tidak dipakai di file ini.
__all__ = [
    "MarketBot",
    "TG_MAX_MESSAGE_CHARS",
    "split_long_text",
    "_strip_provider_prefix",
    "strip_markdown_asterisks",
    "label_to_symbol",
    "MENU_KEYBOARD_LABELS",
    "MENU_KEYBOARD_ACTIONS",
    "_menu_reply_keyboard",
    "_main_menu_inline_keyboard",
    "_quick_action_keyboard",
    "_strip_reply_markup",
    "detect_fast_price_query",
    "MEMORY_USAGE",
    "safe_reply_text",
    "safe_send_message",
    "safe_edit_message_text",
    "_edit_progress_message",
]

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)

class MarketBot(
    SchedulerJobsMixin,
    MarketCommandsMixin,
    JournalCommandsMixin,
    WatchlistCommandsMixin,
    PlanCommandsMixin,
    CotCommandsMixin,
    MessageFlowMixin,
    CallbackFlowMixin,
):
    """
    Main bot class untuk menangani semua interaksi Telegram.
    Now with multi-agent analysis system for richer responses.
    """

    AFTERMATH_SEARCH_LOOKBACK_DAYS = 14
    AFTERMATH_USAGE = (
        "🎯 *ANALISIS DAMPAK EVENT* (manual)\n\n"
        "Lihat analisis pengaruh sebuah event ekonomi terhadap DXY, Gold & FX — "
        "angka Actual vs Forecast, interpretasi, dan penjelasan berita.\n\n"
        "Contoh:\n"
        "`/aftermath nfp` — Non-Farm Payrolls terbaru\n"
        "`/aftermath cpi` — inflasi AS\n"
        "`/aftermath fomc` — keputusan suku bunga Fed\n"
        "`/aftermath gdp` — pertumbuhan ekonomi\n\n"
        "Event dicari dari 14 hari terakhir (yang baru rilis) sampai besok."
    )
    BROADCAST_USAGE = (
        "📣 *BROADCAST* (khusus admin)\n\n"
        "Mengirim pesan ke semua subscriber bot (morning brief + alert event).\n\n"
        "Cara pakai:\n"
        "`/broadcast <pesan>` — lihat pratinjau jumlah penerima\n"
        "`/broadcast send <pesan>` — KIRIM sungguhan\n\n"
        "Contoh:\n"
        "`/broadcast send Selamat pagi! Ada update penting dari bot.`"
    )
    COMMAND_RATE_LIMIT_SECONDS = 15.0
    EVENT_AFTERMATH_DEDUP_TTL_DAYS = 7
    GOLD_PREDICTION_SYMBOL = "GC=F"
    JOURNAL_USAGE = (
        "📓 *TRADING JOURNAL*\n\n"
        "Catat & evaluasi transaksimu (edukasi).\n\n"
        "`/journal add XAU/USD long 2400 2390 2420 0.5` — tambah posisi\n"
        "`/journal close 12 2410` — tutup posisi id 12 di harga 2410\n"
        "`/journal list` — transaksi terbaru\n"
        "`/journal stats` — win rate & rekap per pair\n"
        "`/journal del 12` — hapus entri\n\n"
        "Format add: `<simbol> <long|short> <entry> [sl] [tp] [lot]`"
    )
    MAP_INSTRUMENTS = [
        ("EUR/USD", "eur/usd"),
        ("GBP/USD", "gbp/usd"),
        ("USD/JPY", "usd/jpy"),
        ("USD/IDR", "usd/idr"),
        ("XAU/USD", "xau/usd spot"),
        ("XAG/USD", "xag/usd"),
        ("DXY", "dxy"),
        ("BTC/USD", "btc/usd"),
        ("ETH/USD", "eth/usd"),
        ("S&P 500", "s&p 500"),
    ]
    PREDICTION_USAGE = (
        "🎯 *PREDIKSI NEWS — XAU/USD (GOLD)*\n\n"
        "Bot memprediksi arah emas (*naik*/turun) 5 menit sebelum event ekonomi "
        "high-impact rilis (NFP, CPI, FOMC, GDP, dll), lalu AI menilai benar/salah "
        "setelah rilis. Notifikasi dikirim ke subscriber `/alert`.\n\n"
        "`/prediksi` — statistik win rate + 10 prediksi terakhir\n"
        "`/prediksi history` — riwayat 25 prediksi terakhir\n"
        "`/prediksi help` — bantuan ini"
    )
    RISK_USAGE = (
        "📐 *POSITION SIZE CALCULATOR*\n\n"
        "Hitung ukuran posisi berdasarkan modal, risiko per trade, dan SL.\n\n"
        "Contoh:\n"
        "`/risk 1000 2 20` — modal $1.000, risiko 2%, SL 20 pips (default XAU/USD)\n"
        "`/risk 500 1 15 EUR/USD`\n"
        "`/risk 1000 2 30 USD/JPY 155` — pair ber-quote JPY butuh harga quote\n\n"
        "Simbol didukung: forex mayor, XAU/USD (Gold), XAG/USD (Silver)."
    )
    SETTINGS_HELP = (
        "⚙️ *PENGATURAN*\n\n"
        "Semua pengaturan bot dalam satu tempat — ketuk tombol untuk mengubah:\n\n"
        "🔔 *Alert Event* — notifikasi otomatis: digest harian + reminder sebelum rilis "
        "+ analisis aftermath + prediksi arah emas\n"
        "🌅 *Morning Brief* — ringkasan pasar otomatis setiap pagi\n"
        "👁️ *Watchlist* — instrumen favorit (morning brief fokus + /map watchlist)\n"
        "🧠 *Konteks* — hapus riwayat percakapan yang bot ingat (privasi)"
    )

    def __init__(self):
        self.ai = AIFallbackEngine()
        self.market = MarketDataAggregator()
        self.macro = MacroDataFetcher()
        self.news = NewsFetcher()
        self.chart = ChartGenerator()
        self.sentiment = SentimentAnalyzer(ai_engine=self.ai, news_fetcher=self.news)
        self.news_preds = NewsPredictionStore()
        self.start_time = time.time()
        self.total_questions = 0
        # Aktivitas user dalam memori (user_id → jumlah pertanyaan) — di-flush
        # batch ke Supabase setiap ~10 menit (numpang job cache cleanup) agar
        # statistik user bertahan lintas restart tanpa request per pesan.
        self._user_activity: Dict[int, int] = {}
        # Batas user unik yang ditrack di _user_activity — anti membengkak tanpa
        # batas bila Supabase TIDAK dikonfigurasi (flush gagal → hitungan selalu
        # dikembalikan ke memori). User terlama di-evict (FIFO aproximatif).
        self._MAX_USER_ACTIVITY_ENTRIES = 5000
        # Kuota harian per-user (user_id → [tanggal, jumlah]). In-memory:
        # reset saat restart — trade-off sengaja agar ringan di free tier.
        self._daily_usage: Dict[int, list] = {}

        # Initialize multi-agent analysis system
        if ENABLE_MULTI_AGENT:
            self.analysis_director = AnalysisDirector(
                ai_engine=self.ai,
                market_data=self.market,
                macro_data=self.macro,
                news_fetcher=self.news,
            )
            logger.info("Multi-agent analysis system initialized")
        else:
            self.analysis_director = None
            logger.info("Multi-agent analysis system disabled (using legacy mode)")
    async    def _check_command_rate_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """
        True bila command boleh jalan; False bila kena rate limit (pesan sudah dikirim).

        Rate limit PER-USER (context.user_data) — user lain tidak terpengaruh.
        """
        user_id = update.effective_user.id
        user_data = context.user_data
        now = time.time()
        last = user_data.get("last_ai_command_time", 0)
        remaining = self.COMMAND_RATE_LIMIT_SECONDS - (now - last)
        if remaining > 0:
            wait = int(remaining) + 1
            await safe_reply_text(
                update.message,
                f"⏳ Mohon tunggu *{wait} detik* sebelum menjalankan perintah ini lagi "
                f"(analisis AI butuh waktu & kuota).",
                parse_mode="Markdown",
            )
            return False
        # Kuota harian per-user (proteksi kuota AI gratis)
        if self._daily_quota_exceeded(user_id):
            await safe_reply_text(
                update.message,
                f"⏳ Kuota harian *{USER_DAILY_QUOTA}* pertanyaan sudah habis. "
                f"Kembali lagi besok ya 🙏",
                parse_mode="Markdown",
            )
            return False
        self._consume_daily_quota(user_id)
        user_data["last_ai_command_time"] = now
        return True
    async def notify_ai_outage(self, application: Application) -> None:
        """Notif admin saat semua AI provider down (rate-limited) & saat pulih.

        Dipanggil berkala (numpang job cache cleanup tiap 10 menit). Mencegah
        silent-fail: kalau SEMUA provider AI gagal, user hanya melihat pesan
        error "semua provider sedang tidak tersedia" — tanpa notif ini admin
        tidak tahu ada masalah nyata di balik layar.

        Rate-limited: alert down dikirim sekali; alert pulih dikirim saat
        provider kembali normal (state disimpan di bot_data, bukan RAM class).
        """
        from utils.admin_alerts import notify_admins

        if not ADMIN_USER_IDS:
            return
        bot_data = application.bot_data
        down = bool(self.ai and self.ai.is_total_failure_active())
        was_down = bool(bot_data.get("_ai_down_notified", False))
        if down and not was_down:
            await notify_admins(
                application.bot,
                "⚠️ *AI DOWN:* semua provider AI gagal beruntun — bot tidak bisa "
                "menjawab analisis baru. Cek API keys & rate limit provider "
                "(OpenRouter/Groq/Gemini/dll), lalu /status.",
            )
            bot_data["_ai_down_notified"] = True
        elif not down and was_down:
            await notify_admins(
                application.bot,
                "✅ *AI pulih:* provider AI kembali normal — analisis bisa "
                "dijalankan lagi.",
            )
            bot_data["_ai_down_notified"] = False
    def _daily_quota_exceeded(self, user_id: int) -> bool:
        """True bila user sudah melewati kuota harian (USER_DAILY_QUOTA)."""
        if USER_DAILY_QUOTA <= 0:
            return False
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rec = self._daily_usage.get(user_id)
        if not rec or rec[0] != today:
            # Tanggal baru (atau user baru) — mulai dari 0
            self._daily_usage[user_id] = [today, 0]
            return False
        return rec[1] >= USER_DAILY_QUOTA
    def _consume_daily_quota(self, user_id: int) -> None:
        """Catat satu penggunaan kuota harian (dipanggil saat pipeline AI jalan)."""
        if USER_DAILY_QUOTA <= 0:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rec = self._daily_usage.get(user_id)
        if not rec or rec[0] != today:
            # Bersihkan entri tanggal lama kalau dict sudah besar (anti bocor)
            if len(self._daily_usage) > self._MAX_USER_ACTIVITY_ENTRIES:
                try:
                    stale = [
                        uid for uid, r in self._daily_usage.items() if r[0] != today
                    ]
                    for uid in stale:
                        self._daily_usage.pop(uid, None)
                except Exception:
                    pass
            self._daily_usage[user_id] = [today, 0]
        self._daily_usage[user_id][1] += 1
    def _track_user_activity(self, user_id: int) -> None:
        """
        Catat satu pertanyaan dari user (dengan batas jumlah user unik).

        Batasi user unik yang ditrack: bila Supabase TIDAK dikonfigurasi, flush
        gagal dan hitungan terus dikembalikan ke memori → tanpa batas, dict
        membengkak seiring bertambahnya user. User terlama di-evict (FIFO
        aproximatif via iterasi dict) — hitungan hanya statistik, aman hilang.
        """
        if user_id not in self._user_activity and len(self._user_activity) >= self._MAX_USER_ACTIVITY_ENTRIES:
            try:
                self._user_activity.pop(next(iter(self._user_activity)))
            except (StopIteration, RuntimeError):
                pass
        self._user_activity[user_id] = self._user_activity.get(user_id, 0) + 1
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /start."""
        user = update.effective_user
        logger.info(f"User {user.id} ({user.first_name}) started the bot")

        # Simpan/update user ke database (async — jangan blokir event loop)
        await db.upsert_user_async(user.id, user.username, user.first_name)

        # Keyboard menu — ringkas, tanpa duplikasi fitur: harga cepat →
        # overview/kalender → analisis → notifikasi → pengaturan → bantuan.
        # Fitur lain tetap tersedia via perintah (/sentimen, /subscribe, dll).
        reply_markup = _main_menu_inline_keyboard()

        await safe_reply_text(
            update.message,
            WELCOME_MESSAGE,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        # Reply keyboard persistent di bawah kolom input — menu selalu terlihat
        # tanpa harus scroll pesan / ketik perintah. Tombolnya mengirim label
        # sebagai teks; handle_message mengenalinya dan menjalankan aksi yang
        # sama dengan tombol menu di atas.
        try:
            await update.message.reply_text(
                "📌 *Menu siap di keyboard bawah* — ketuk tombol kapan saja. 👇",
                parse_mode="Markdown",
                reply_markup=_menu_reply_keyboard(),
            )
        except Exception as e:
            logger.debug(f"Reply keyboard setup gagal (bot tetap jalan): {e}")
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /help."""
        await safe_reply_text(
            update.message,
            HELP_MESSAGE,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    async def about_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /about."""
        await safe_reply_text(
            update.message,
            ABOUT_MESSAGE,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /status."""
        uptime_seconds = int(time.time() - self.start_time)
        uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m"

        # Helper ini melakukan I/O jaringan (cek Yahoo Finance, dll) — jalankan
        # di thread agar tidak memblokir event loop Telegram.
        ai_status, data_status, analysis_status = await asyncio.gather(
            asyncio.to_thread(get_ai_providers_status, self.ai),
            asyncio.to_thread(get_data_sources_status, self.market, self.macro, self.news),
            asyncio.to_thread(get_analysis_engine_status),
            return_exceptions=True,
        )
        # Satu helper gagal (mis. timeout jaringan) tidak boleh menghentikan /status
        if isinstance(ai_status, Exception):
            ai_status = "  ⚠️ Status provider tidak dapat dimuat"
        if isinstance(data_status, Exception):
            data_status = "  ⚠️ Status data tidak dapat dimuat"
        if isinstance(analysis_status, Exception):
            analysis_status = "  • ⬜ Multi-Agent: Tidak tersedia"
        cache_stats = cache.get_stats()

        # Jadwal & statistik run terakhir pre-warm COT (dari bot_data — ditulis
        # oleh job prewarm_cot_cache; tanpa run → 'Belum pernah berjalan').
        cot_prewarm_status = self._cot_prewarm_status_text(context.bot_data)

        status_msg = STATUS_MESSAGE_TEMPLATE.format(
            bot_status="✅ ONLINE",
            uptime=uptime_str,
            total_questions=self.total_questions,
            ai_providers_status=ai_status,
            data_sources_status=data_status,
            cache_stats=f"{cache_stats['active_entries']} entries aktif",
            analysis_engine_status=analysis_status,
            cot_prewarm_status=cot_prewarm_status,
            server_time=datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S"),
        )

        await safe_reply_text(
            update.message,
            status_msg,
            parse_mode="Markdown",
        )
    async def flush_user_activity(self) -> None:
        """
        Flush aktivitas user (last_active_at + total_questions) ke Supabase.

        Dipanggil berkala dari job cache cleanup (10 menit) — batch satu request
        untuk semua user, tanpa menambah beban per pesan. Kegagalan DB aman
        (hitungan tetap dipertahankan di memori untuk flush berikutnya).
        """
        if not self._user_activity:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            (uid, now_iso, count)
            for uid, count in self._user_activity.items()
        ]
        self._user_activity.clear()
        try:
            ok = await db.update_user_activity_async(rows)
            if not ok:
                # Gagal (mis. DB belum dikonfigurasi) — pulihkan hitungan agar
                # tidak hilang; coba lagi di flush berikutnya.
                for uid, _last_active, count in rows:
                    self._user_activity[uid] = self._user_activity.get(uid, 0) + count
                logger.debug("User activity flush gagal — hitungan dikembalikan ke memori")
        except Exception as e:
            logger.warning(f"User activity flush error: {e}")
            for uid, _last_active, count in rows:
                self._user_activity[uid] = self._user_activity.get(uid, 0) + count
    async def flush_daily_usage(self) -> None:
        """
        Flush kuota harian per-user ke Supabase (batch, numpang job 10 menit).

        Kuota persist supaya TIDAK reset saat bot restart / spin-down free tier
        (sebelumnya murni in-memory — bisa disiasati dengan restart). Gagal DB
        aman: hitungan dikembalikan ke memori untuk flush berikutnya.
        """
        if not self._daily_usage:
            return
        rows = [
            (uid, date_str, count)
            for uid, (date_str, count) in self._daily_usage.items()
        ]
        self._daily_usage.clear()
        try:
            ok = await db.update_daily_usage_async(rows)
            if not ok:
                for uid, date_str, count in rows:
                    self._daily_usage[uid] = [date_str, count]
                logger.debug("Daily usage flush gagal — hitungan dikembalikan ke memori")
        except Exception as e:
            logger.warning(f"Daily usage flush error: {e}")
            for uid, date_str, count in rows:
                self._daily_usage[uid] = [date_str, count]

    async def load_daily_usage(self) -> None:
        """
        Muat kuota harian HARI INI dari Supabase ke memori (dipanggil saat boot).

        Tanpa ini kuota yang sudah terpakai sebelum restart hilang (reset →
        user bisa melewati batas dengan restart). Best-effort: gagal DB aman,
        bot tetap jalan dengan kuota mulai dari 0.
        """
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            loaded = await db.get_daily_usage_async(today)
            if loaded:
                self._daily_usage = {
                    uid: [today, count]
                    for uid, count in loaded.items()
                }
                logger.info(f"Loaded daily usage dari Supabase: {len(loaded)} user")
        except Exception as e:
            logger.warning(f"Load daily usage gagal: {e}")
    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /subscribe - Berlangganan Morning Brief."""
        chat_id = update.effective_chat.id

        if await db.is_subscribed_async(chat_id):
            await safe_reply_text(update.message, "✅ Anda sudah berlangganan Morning Brief.")
            return

        if await db.add_subscriber_async(chat_id):
            await safe_reply_text(update.message, "🎉 Berhasil! Anda sekarang akan menerima Morning Brief setiap pagi.")
        else:
            await safe_reply_text(update.message, "❌ Gagal mendaftar langganan. Database mungkin belum dikonfigurasi.")
    async def unsubscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /unsubscribe - Berhenti berlangganan Morning Brief."""
        chat_id = update.effective_chat.id

        if not await db.is_subscribed_async(chat_id):
            await safe_reply_text(update.message, "⚠️ Anda memang belum berlangganan Morning Brief.")
            return

        if await db.remove_subscriber_async(chat_id):
            await safe_reply_text(update.message, "👋 Berhasil berhenti langganan Morning Brief.")
        else:
            await safe_reply_text(update.message, "❌ Gagal membatalkan langganan. Database mungkin belum dikonfigurasi.")
    async def _build_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Bangun pesan + tombol menu pengaturan dengan STATUS TERKINI.

        Returns:
            (message, reply_markup) — selalu punya keyboard (tidak None).
        """
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        # Status Alert Event (dari bot_data — sinkron dengan /alert)
        alert_on = chat_id in context.bot_data.get("event_alert_subscribers", set())
        alert_status = "✅ AKTIF" if alert_on else "❌ NONAKTIF"
        alert_btn = (
            "🔔 Matikan Alert Event" if alert_on else "🔔 Aktifkan Alert Event"
        )

        # Status Morning Brief (dari Supabase)
        try:
            brief_on = await db.is_subscribed_async(chat_id)
        except Exception as e:
            logger.debug(f"Cek status brief gagal: {e}")
            brief_on = False
        brief_status = "✅ AKTIF" if brief_on else "❌ NONAKTIF"
        brief_btn = (
            "🌅 Berhenti Langganan Brief" if brief_on else "🌅 Langganan Morning Brief"
        )

        # Status konteks percakapan
        history = get_history(user_id)
        ctx_count = len(history)
        ctx_label = f"🧠 Konteks: {ctx_count} percakapan tersimpan" if ctx_count else "🧠 Konteks: kosong"

        # Status watchlist (dari Supabase — best-effort)
        try:
            watchlist = await db.get_watchlist_async(user_id)
        except Exception as e:
            logger.debug(f"Cek watchlist gagal: {e}")
            watchlist = []
        wl_status = f"👁️ Watchlist: {len(watchlist)} instrumen" if watchlist else "👁️ Watchlist: kosong"

        message = (
            "⚙️ *PENGATURAN*\n\n"
            f"🔔 *Alert Event:* {alert_status}\n"
            f"🌅 *Morning Brief:* {brief_status}\n"
            f"{wl_status}\n"
            f"{ctx_label}\n\n"
            "Ketuk tombol di bawah untuk mengubah."
        )

        keyboard = [
            [InlineKeyboardButton(alert_btn, callback_data="settings_alert")],
            [InlineKeyboardButton(brief_btn, callback_data="settings_brief")],
            [InlineKeyboardButton("👁️ Kelola Watchlist", callback_data="settings_watchlist")],
            [InlineKeyboardButton("🧹 Bersihkan Konteks", callback_data="settings_clear")],
            [InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu")],
        ]
        return message, InlineKeyboardMarkup(keyboard)
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk /settings — buka menu Pengaturan (sama dengan tombol ⚙️).

        Menampilkan status semua pengaturan + tombol toggle di keyboard inline,
        sehingga user bisa mengubah tanpa harus ketik perintah.
        """
        message, kb = await self._build_settings_menu(update, context)
        await safe_reply_text(
            update.message,
            message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    async def clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk /clear - Hapus konteks percakapan user (privasi)."""
        user_id = update.effective_user.id
        clear(user_id)
        await safe_reply_text(
            update.message,
            "🧹 *Konteks percakapan dibersihkan.*\n\n"
            "Saya tidak lagi mengingat percakapan sebelumnya — mulai dari nol! 😊",
            parse_mode="Markdown",
        )
    async def memory_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler /memory — lihat & hapus riwayat percakapan user.

        Riwayat tersimpan di memori + app_cache Supabase (TTL 24 jam). Perintah
        ini menampilkan apa yang bot ingat tentang user (Q&A terakhir + konteks
        terstruktur aset/arah tren) dan cara menghapusnya (/memory clear).
        """
        user_id = update.effective_user.id
        text = (update.message.text or "").lower()
        arg = text.replace("/memory", "", 1).strip()

        if arg in ("clear", "del", "delete", "hapus"):
            clear(user_id)
            await safe_reply_text(
                update.message,
                "🧹 *Riwayat percakapan dihapus.*\n\n"
                "Bot tidak lagi mengingat percakapan sebelumnya — mulai dari nol! 😊",
                parse_mode="Markdown",
            )
            return

        history = get_history(user_id)
        ctx = get_context(user_id)

        if not history and not ctx:
            await safe_reply_text(update.message, MEMORY_USAGE, parse_mode="Markdown")
            return

        lines = ["🧠 *Yang bot ingat tentang kamu:*"]
        # Jawaban AI mengandung markdown (**, _, dll) — strip asterisks agar tidak
        # merusak parse_mode Markdown pesan ini (entity tidak seimbang = error).
        for ex in reversed(history):  # terbaru di atas
            q = strip_markdown_asterisks((ex.get("q") or "").strip())
            a = strip_markdown_asterisks((ex.get("a") or "").strip())
            if q:
                lines.append(f"\n👤 *Kamu:* {q}")
            if a:
                lines.append(f"🤖 *Bot:* {a[:200]}")
        if ctx:
            ctx_lines = []
            if ctx.get("asset_focus"):
                ctx_lines.append(f"• Fokus aset: *{ctx['asset_focus']}*")
            if ctx.get("direction"):
                ctx_lines.append(f"• Arah tren: *{ctx['direction']}*")
            if ctx_lines:
                lines.append("\n📌 *Konteks terakhir:*")
                lines.extend(ctx_lines)
        lines.append("\n⏳ Otomatis terhapus setelah 24 jam.")
        lines.append("🗑️ Hapus sekarang: `/memory clear`")
        await safe_reply_text(update.message, "\n".join(lines), parse_mode="Markdown")
    async def syncmenu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /syncmenu — sinkronkan menu perintah (khusus admin)."""
        user_id = update.effective_user.id
        if user_id not in ADMIN_USER_IDS:
            await safe_reply_text(
                update.message,
                "🔒 Perintah ini khusus admin bot.",
                parse_mode="Markdown",
            )
            return
        from utils.bot_menu import set_bot_commands

        ok = await set_bot_commands(context.bot)
        if ok:
            await safe_reply_text(
                update.message,
                "✅ Menu perintah berhasil disinkronkan ke versi terbaru "
                "(command lama seperti /pa dihapus).",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(
                update.message,
                "❌ Gagal sinkronisasi menu (coba lagi / cek log).",
                parse_mode="Markdown",
            )
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /stats — statistik sistem lengkap (khusus admin)."""
        user_id = update.effective_user.id
        if user_id not in ADMIN_USER_IDS:
            await safe_reply_text(
                update.message,
                "🔒 Perintah ini khusus admin bot.",
                parse_mode="Markdown",
            )
            return

        # Semua I/O jaringan di-thread agar tidak memblokir event loop
        user_stats, counts = await asyncio.gather(
            asyncio.to_thread(db.get_user_stats),
            asyncio.to_thread(db.get_counts),
            return_exceptions=True,
        )
        if isinstance(user_stats, Exception):
            user_stats = {}
        if isinstance(counts, Exception):
            counts = {}

        # Pemakaian token dari engine (dari field usage response API)
        eng_stats = self.ai.get_stats()
        usage = eng_stats.get("usage", {}) or {}
        by_provider = usage.get("by_provider", {}) or {}
        provider_lines = []
        for provider, tok in sorted(by_provider.items()):
            total = (tok.get("prompt_tokens") or 0) + (tok.get("completion_tokens") or 0)
            provider_lines.append(
                f"  • {provider}: {total:,} token (in {tok.get('prompt_tokens', 0):,} / "
                f"out {tok.get('completion_tokens', 0):,})"
            )
        token_part = "\n".join(provider_lines) if provider_lines else "  • Belum ada pemakaian tercatat"

        # Statistik prediksi news (XAU/USD win rate) dari store in-memory
        pred_stats = self.news_preds.get_stats()
        pred_part = (
            f"  • Total: {pred_stats.get('total', 0)} | Selesai: {pred_stats.get('settled', 0)} | "
            f"Pending: {pred_stats.get('pending', 0)}\n"
            f"  • Benar: {pred_stats.get('benar', 0)} | Salah: {pred_stats.get('salah', 0)} | "
            f"Flat: {pred_stats.get('flat', 0)}\n"
            f"  • Win rate: {pred_stats.get('win_rate', 0):.1f}%"
            if pred_stats.get('win_rate') is not None
            else f"  • Total: {pred_stats.get('total', 0)} | Belum ada prediksi selesai"
        )

        uptime_seconds = int(time.time() - self.start_time)
        uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m"
        cache_stats = cache.get_stats()
        supabase_ready = "✅ Terhubung" if db.is_connected() else "⚠️ Tidak dikonfigurasi"

        msg = (
            "📊 *STATISTIK SISTEM* (admin)\n\n"
            f"⏱ *Uptime:* {uptime_str}\n"
            f"📥 *Pertanyaan (session):* {self.total_questions}\n\n"
            f"💰 *Token AI:*\n{token_part}\n"
            f"  **Total:** {usage.get('total_tokens', 0):,} "
            f"(in {usage.get('prompt_tokens', 0):,} / out {usage.get('completion_tokens', 0):,})\n\n"
            f"🗄️ *Supabase:* {supabase_ready}\n"
            f"  • User terdaftar: {user_stats.get('total_users', 0)}\n"
            f"  • User aktif (24 jam): {user_stats.get('active_24h', 0)}\n"
            f"  • Pertanyaan (DB): {user_stats.get('total_questions', 0)}\n"
            f"  • Subscriber morning brief: {counts.get('subscribers', 0)}\n"
            f"  • Subscriber alert event: {counts.get('event_alert_subscribers', 0)}\n\n"
            f"📈 *Prediksi News (XAU/USD):*\n{pred_part}\n\n"
            f"💾 *Cache:* {cache_stats.get('active_entries', 0)} entries aktif"
        )
        await safe_reply_text(update.message, msg, parse_mode="Markdown")
    async def usage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /usage — laporan pemakaian AI (token & request, khusus admin).

        Lebih ringkas dari /stats: fokus pada token & request per provider agar
        admin bisa memantau kuota gratis sebelum kena limit.
        """
        user_id = update.effective_user.id
        if user_id not in ADMIN_USER_IDS:
            await safe_reply_text(
                update.message,
                "🔒 Perintah ini khusus admin bot.",
                parse_mode="Markdown",
            )
            return
        from bot.messages import format_ai_usage_report

        report = format_ai_usage_report(self.ai.get_stats())
        await safe_reply_text(update.message, report, parse_mode="Markdown")
    async def broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /broadcast — kirim pesan ke semua subscriber (khusus admin)."""
        user_id = update.effective_user.id
        if user_id not in ADMIN_USER_IDS:
            await safe_reply_text(
                update.message,
                "🔒 Perintah ini khusus admin bot.",
                parse_mode="Markdown",
            )
            return

        text = update.message.text or ""
        arg = text.replace("/broadcast", "", 1).strip()
        if not arg or arg.lower() in ("help", "bantuan"):
            await safe_reply_text(update.message, self.BROADCAST_USAGE, parse_mode="Markdown")
            return

        confirm = arg.lower().startswith("send ")
        message = arg[5:].strip() if confirm else arg
        if not message:
            await safe_reply_text(update.message, self.BROADCAST_USAGE, parse_mode="Markdown")
            return

        # Kumpulkan penerima: subscriber morning brief (DB) + event alert (bot_data)
        recipients: set = set()
        try:
            recipients |= set(await db.get_all_subscribers_async())
        except Exception as e:
            logger.warning(f"Broadcast: gagal ambil subscriber DB: {e}")
        recipients |= set(context.bot_data.get("event_alert_subscribers", set()))
        # Admin tidak perlu menerima pesannya sendiri
        recipients.discard(user_id)

        if not recipients:
            await safe_reply_text(
                update.message,
                "📭 Belum ada subscriber untuk menerima broadcast.",
                parse_mode="Markdown",
            )
            return

        if not confirm:
            await safe_reply_text(
                update.message,
                f"📣 *PRATINJAU BROADCAST*\n\n"
                f"Pesan akan dikirim ke *{len(recipients)}* chat.\n\n"
                f"\"{message[:200]}\"\n\n"
                f"Kirim ulang dengan awalan `/broadcast send` untuk KIRIM sungguhan.",
                parse_mode="Markdown",
            )
            return

        # Kirim sungguhan
        sent = 0
        failed = 0
        for chat_id in list(recipients):
            try:
                await safe_send_message(
                    context.bot,
                    chat_id=chat_id,
                    text=f"📣 *PENGUMUMAN*\n\n{message}",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                sent += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Broadcast gagal ke {chat_id}: {e}")
                if "Forbidden" in str(e):
                    # User block bot / keluar — hapus dari daftar subscriber event
                    subs = context.bot_data.get("event_alert_subscribers", set())
                    subs.discard(chat_id)
                    context.bot_data["event_alert_subscribers"] = subs
                    await self._persist_alert_subscribers(context)

        await safe_reply_text(
            update.message,
            f"✅ *Broadcast selesai* — terkirim: {sent}, gagal: {failed} (dari {len(recipients)}).",
            parse_mode="Markdown",
        )
