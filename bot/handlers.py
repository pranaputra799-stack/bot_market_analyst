"""
Telegram Bot Handlers - Semua handler untuk perintah dan pesan dari user.
Menggunakan python-telegram-bot v20.x.
Now with multi-agent analysis system from MarketLens.
"""
import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ContextTypes,
)

from config.settings import (
    MORNING_BRIEF_TIMEZONE,
    MORNING_BRIEF_CHAT_IDS,
    ADMIN_USER_IDS,
    ENABLE_MULTI_AGENT,
    ECONOMIC_ALERT_LEAD_HOURS,
    EVENT_AFTERMATH_ENABLED,
    EVENT_AFTERMATH_LOOKBACK_HOURS,
    NEWS_PREDICTION_ENABLED,
    NEWS_PREDICTION_LEAD_MINUTES,
    NEWS_PREDICTION_SETTLE_MINUTES,
    NEWS_PREDICTION_MIN_MOVE_PCT,
    NEWS_PREDICTION_MAX_PER_RUN,
    USER_DAILY_QUOTA,
    SESSION_ALERT_ENABLED,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore
from config.providers import YAHOO_SYMBOLS, OANDA_SYMBOLS, FRED_INDICATORS
from ai.engine import AIFallbackEngine
from data.market_data import MarketDataAggregator
from data.macro_data import MacroDataFetcher
from data.news_data import NewsFetcher
from data.cache import cache
from data.database import db
from data.news_predictions import NewsPredictionStore
from data.conversation_memory import format_history, add_exchange, get_context, get_history, clear
from utils.chart_generator import ChartGenerator
from utils.risk_calculator import calculate_position_size, format_risk_result
from utils.sessions import sessions_just_opened, format_session_text
from analysis.director import AnalysisDirector
from analysis.indicators import compute_indicators, format_key_levels, format_indicators_for_prompt
from analysis.fact_check import build_fact_check_note, strip_fact_check_note
from utils.validators import sanitize_text
from prompts.loader import format_prompt
from analysis.sentiment import SentimentAnalyzer
from bot.messages import (
    WELCOME_MESSAGE,
    HELP_MESSAGE,
    ABOUT_MESSAGE,
    STATUS_MESSAGE_TEMPLATE,
    ERROR_MESSAGE,
    RATE_LIMIT_MESSAGE,
    DISCLAIMER,
    format_price,
    MORNING_BRIEF_TEMPLATE,
    ALERT_ON_MESSAGE,
    ALERT_OFF_MESSAGE,
    get_ai_providers_status,
    get_data_sources_status,
    get_analysis_engine_status,
)

logger = logging.getLogger(__name__)


# Batas maksimal karakter per pesan Telegram (4096). Dipakai 4000 agar ada ruang
# aman untuk emoji/karakter UTF-8 yang bisa dihitung berbeda.
TG_MAX_MESSAGE_CHARS = 4000


def split_long_text(text: str, max_len: int = TG_MAX_MESSAGE_CHARS) -> List[str]:
    """
    Pecah teks panjang menjadi beberapa bagian agar tidak melebihi batas
    4096 karakter per pesan Telegram.

    Memotong di batas paragraf, lalu batas baris, agar konten tidak terpotong
    di tengah kalimat. Bagian-bagian yang sudah dipecah tetap utuh (tidak ada
    konten yang dibuang).

    Args:
        text: Teks yang akan dipecah
        max_len: Batas panjang per bagian

    Returns:
        List of text chunks
    """
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    current = ""

    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 <= max_len:
            current = f"{current}\n\n{para}" if current else para
            continue

        # Flush paragraf yang sudah terkumpul
        if current:
            chunks.append(current)
            current = ""

        if len(para) <= max_len:
            current = para
            continue

        # Paragraf sendirian lebih panjang dari max_len: pecah per baris,
        # lalu hard-split bila masih ada baris yang lebih panjang dari max_len.
        buf = ""
        for line in para.split("\n"):
            if len(buf) + len(line) + 1 <= max_len:
                buf = f"{buf}\n{line}" if buf else line
            else:
                if buf:
                    chunks.append(buf)
                while len(line) > max_len:
                    chunks.append(line[:max_len])
                    line = line[max_len:]
                buf = line
        if buf:
            current = buf

    if current:
        chunks.append(current)

    return [c for c in chunks if c]


async def _reply_chunk(message, chunk: str, parse_mode: Optional[str], kwargs: Dict):
    """Kirim satu chunk via reply_text dengan fallback plain text jika Markdown gagal."""
    try:
        return await message.reply_text(chunk, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if any(err in str(e).lower() for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await message.reply_text(chunk, **kwargs_copy)
        raise


async def safe_reply_text(message, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Kirim reply_text dengan aman.
    - Fallback ke plain text jika parse mode gagal.
    - Otomatis pecah jadi beberapa pesan jika melebihi batas 4096 karakter
      (menyebabkan morning brief / analisis panjang tidak terpotong).
    """
    try:
        return await _reply_chunk(message, text, parse_mode, kwargs)
    except BadRequest as e:
        if "too long" in str(e).lower():
            logger.info(f"Message too long ({len(text)} chars), splitting into parts...")
            result = None
            chunks = split_long_text(text)
            for i, chunk in enumerate(chunks):
                chunk_kwargs = kwargs if i == len(chunks) - 1 else _strip_reply_markup(kwargs)
                result = await _reply_chunk(message, chunk, parse_mode, chunk_kwargs)
            return result
        raise


async def _send_chunk(bot, chat_id: int, chunk: str, parse_mode: Optional[str], kwargs: Dict):
    """Kirim satu chunk via send_message dengan fallback plain text jika Markdown gagal."""
    try:
        return await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if any(err in str(e).lower() for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await bot.send_message(chat_id=chat_id, text=chunk, **kwargs_copy)
        raise


async def safe_send_message(bot, chat_id: int, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Kirim send_message dengan aman.
    - Fallback ke plain text jika parse mode gagal.
    - Otomatis pecah jadi beberapa pesan jika melebihi batas 4096 karakter.
    """
    try:
        return await _send_chunk(bot, chat_id, text, parse_mode, kwargs)
    except BadRequest as e:
        if "too long" in str(e).lower():
            logger.info(f"Message too long ({len(text)} chars), splitting into parts...")
            result = None
            for chunk in split_long_text(text):
                result = await _send_chunk(bot, chat_id, chunk, parse_mode, kwargs)
            return result
        raise


async def safe_edit_message_text(query, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Edit message text dengan aman.
    - Fallback ke plain text jika parse mode gagal.
    - Jika teks terlalu panjang untuk di-edit, kirim sebagai pesan balasan baru
      (dipecah bila perlu) agar konten tidak hilang.
    """
    try:
        return await query.edit_message_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return None  # konten sudah sama persis — tidak perlu edit
        if "too long" in msg:
            if query.message is not None:
                logger.info(f"Edit message too long ({len(text)} chars), replying with parts instead...")
                result = None
                for chunk in split_long_text(text):
                    result = await _reply_chunk(query.message, chunk, parse_mode, kwargs)
                return result
            # Tidak ada message untuk di-reply (mis. inline mode) — biarkan error asli
            raise
        if any(err in msg for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await query.edit_message_text(text, **kwargs_copy)
        raise


async def _edit_progress_message(message, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Ganti pesan progress dengan jawaban akhir (edit_text).
    - Fallback plain text jika Markdown gagal.
    - Jika terlalu panjang untuk di-edit, reply berchunk lalu hapus pesan progress.
    """
    try:
        return await message.edit_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return None  # teks sama persis — abaikan
        if "too long" in msg:
            result = None
            chunks = split_long_text(text)
            for i, chunk in enumerate(chunks):
                # Hanya chunk terakhir yang membawa tombol aksi cepat
                chunk_kwargs = kwargs if i == len(chunks) - 1 else _strip_reply_markup(kwargs)
                result = await _reply_chunk(message, chunk, parse_mode, chunk_kwargs)
            try:
                await message.delete()  # bersihkan pesan progress
            except Exception:
                pass
            return result
        if any(err in msg for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await message.edit_text(text, **kwargs_copy)
        raise


def _strip_provider_prefix(text: str) -> str:
    """
    Hapus prefix '[via Provider] 🤖' dari response AI (ditambahkan engine
    untuk request tanpa system_override) agar konten analisis bersih.
    """
    if "[via" in text:
        parts = text.split("\n\n", 1)
        return parts[1] if len(parts) > 1 else text
    return text


def strip_markdown_asterisks(text: str) -> str:
    """
    Hapus simbol '*' (markdown bold/italic) dari teks respons AI agar tidak
    tampil mentah di Telegram.

    Asterisk yang diapit angka (mis. perkalian "5*3" atau footnote) dipertahankan
    agar data numerik tidak rusak.
    """
    if not text:
        return text
    # 1) Hapus pasangan markdown: **teks** atau *teks* (sisakan isinya)
    text = re.sub(r"\*{1,2}([^*\n]+?)\*{1,2}", r"\1", text)
    # 2) Buang asterisk tersisa yang tidak berpasangan (markdown rusak),
    #    kecuali yang diapit angka (perkalian/footnote numerik).
    text = re.sub(r"(?<!\d)\*+(?!\d)", "", text)
    return text


# Kata kunci pertanyaan HARGA sederhana — dijawab INSTAN dari data (tanpa AI)
# agar user tidak menunggu pipeline multi-agent untuk cek harga biasa.
_PRICE_QUERY_KEYWORDS = (
    "harga", "price", "berapa", "rate", "kurs", "nilai", "quote",
    "spot", "live price", "harga sekarang", "harga saat ini",
)
# Jika pertanyaan mengandung kata ini, BUKAN cek harga sederhana — serahkan
# ke pipeline analisis penuh (teknikal, fundamental, prediksi, edukasi, dll).
_ANALYSIS_EXCLUDE_KEYWORDS = (
    "analisis", "teknikal", "support", "resistance", "rsi", "macd",
    "prediksi", "forecast", "ramalan", "kenapa", "mengapa", "sebab",
    "dampak", "korelasi", "hubungan", "banding", "perbedaan", "vs",
    "berita", "sentimen", "chart", "grafik", "cara", "belajar",
    "apa itu", "pengertian", "definisi", "risiko", "jadwal",
    "mana yang", "naik apa turun", "masih bisa", "akan naik", "akan turun",
)

# ===================== QUICK ACTION BUTTONS =====================
# Tombol aksi lanjutan di bawah jawaban analisis — memakai konteks multi-turn
# (fokus aset dari conversation memory) untuk pertanyaan follow-up seperti
# "support-nya berapa?" tanpa perlu menyebut instrumen lagi.

# Label fokus aset (dari conversation_memory.extract_asset_focus) → simbol Yahoo
_LABEL_TO_YAHOO = {
    "XAU/USD (Gold)": "GC=F",
    "XAG/USD (Silver)": "SI=F",
    "BTC/USD (Bitcoin)": "BTC-USD",
    "ETH/USD (Ethereum)": "ETH-USD",
    "DXY (Dollar Index)": "DX-Y.NYB",
    "S&P 500": "^GSPC",
    "NASDAQ": "^IXIC",
    "VIX": "^VIX",
}


def label_to_symbol(label: Optional[str]) -> Optional[str]:
    """Konversi label fokus aset (dari conversation memory) ke simbol Yahoo Finance."""
    if not label:
        return None
    if label in _LABEL_TO_YAHOO:
        return _LABEL_TO_YAHOO[label]
    # Label pair forex "XXX/YYY" → "XXXYYY=X"
    m = re.match(r"^([A-Z]{3})/([A-Z]{3})$", label.strip().upper())
    if m:
        return f"{m.group(1)}{m.group(2)}=X"
    return None


# ===================== REPLY KEYBOARD (menu di keyboard bawah) =====================
# Label tombol keyboard = label menu inline (konsisten). Saat tombol ditekan,
# Telegram mengirim label sebagai pesan teks → handle_message mendeteksi label
# dan menjalankan aksi yang sama dengan callback menu (lihat _run_keyboard_menu_action).

# Label tombol (baris keyboard) → aksi menu (callback_data yang sama)
MENU_KEYBOARD_LABELS = [
    ["🥇 Harga Gold", "💱 EUR/USD"],
    ["🌍 Overview Pasar", "📅 Kalender"],
    ["📰 Sentimen Pasar", "🏛️ Data Makro"],
    ["🌅 Morning Brief", "🎯 Prediksi News"],
    ["🔔 Alert Event", "⚙️ Pengaturan"],
    ["❓ Bantuan"],
]

MENU_KEYBOARD_ACTIONS = {
    "🥇 Harga Gold": "gold_price",
    "💱 EUR/USD": "eurusd",
    "🌍 Overview Pasar": "overview",
    "📅 Kalender": "calendar",
    "📰 Sentimen Pasar": "sentiment",
    "🏛️ Data Makro": "macro",
    "🌅 Morning Brief": "morning",
    "🎯 Prediksi News": "prediksi",
    "🔔 Alert Event": "alert_on",
    "⚙️ Pengaturan": "settings",
    "❓ Bantuan": "help",
}


def _menu_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard persistent di bawah kolom input — menu selalu terlihat."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(label) for label in row] for row in MENU_KEYBOARD_LABELS],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Pilih menu atau tanya pasar...",
    )


def _main_menu_inline_keyboard() -> InlineKeyboardMarkup:
    """Menu utama inline (di dalam pesan) — dipakai /start dan tombol 'Kembali'."""
    keyboard = [
        [
            InlineKeyboardButton("🥇 Harga Gold", callback_data="gold_price"),
            InlineKeyboardButton("💱 EUR/USD", callback_data="eurusd"),
        ],
        [
            InlineKeyboardButton("🌍 Overview Pasar", callback_data="overview"),
            InlineKeyboardButton("📅 Kalender", callback_data="calendar"),
        ],
        [
            InlineKeyboardButton("📰 Sentimen Pasar", callback_data="sentiment"),
            InlineKeyboardButton("🏛️ Data Makro", callback_data="macro"),
        ],
        [
            InlineKeyboardButton("🌅 Morning Brief", callback_data="morning"),
            InlineKeyboardButton("🎯 Prediksi News", callback_data="prediksi"),
        ],
        [
            InlineKeyboardButton("🔔 Alert Event", callback_data="alert_on"),
            InlineKeyboardButton("⚙️ Pengaturan", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("❓ Bantuan", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def _quick_action_keyboard(symbol: Optional[str] = None):
    """Keyboard aksi cepat — simbol ter-embed di callback agar tombol lama
    tetap bekerja untuk instrumen yang benar walau konteks sudah berubah."""
    sr_data = f"qa:sr:{symbol}" if symbol else "qa:sr"
    scenario_data = f"qa:scenario:{symbol}" if symbol else "qa:scenario"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 S/R & Target", callback_data=sr_data),
            InlineKeyboardButton("🔮 Skenario", callback_data=scenario_data),
        ],
        [
            InlineKeyboardButton("🧹 Bersihkan Konteks", callback_data="qa:clear"),
        ],
    ])


def _strip_reply_markup(kwargs: Dict) -> Dict:
    """Salin kwargs tanpa reply_markup — untuk chunk pesan selain yang terakhir
    agar keyboard aksi cepat tidak muncul di setiap potongan pesan panjang."""
    return {k: v for k, v in kwargs.items() if k != "reply_markup"}


def detect_fast_price_query(text: str):
    """
    Deteksi pertanyaan harga sederhana yang bisa dijawab INSTAN tanpa AI.

    Contoh yang cocok:
      "berapa harga eurusd?", "harga gold sekarang", "rate usd/jpy",
      "harga bitcoin", "kurs eurusd", "spot eurusd"

    Contoh yang TIDAK cocok (harus lewat pipeline analisis penuh):
      "kenapa gold naik?", "analisis teknikal eurusd", "berapa prediksi gold?"

    Returns:
        Tuple (yahoo_symbol, display_name) atau None jika bukan pertanyaan harga.
    """
    if not text:
        return None
    q = text.lower().strip()

    # Harus mengandung kata kunci harga
    if not any(kw in q for kw in _PRICE_QUERY_KEYWORDS):
        return None
    # Jangan sentuh pertanyaan yang butuh analisis (bukan sekadar cek harga)
    if any(kw in q for kw in _ANALYSIS_EXCLUDE_KEYWORDS):
        return None

    symbol, display_name = ChartGenerator.get_chart_symbol_from_text(q)
    if not symbol:
        return None
    return symbol, display_name


MEMORY_USAGE = (
    "🧠 *RIWAYAT PERCAKAPAN*\n\n"
    "Bot menyimpan beberapa pertukaran terakhir percakapanmu sebagai konteks "
    "agar jawaban follow-up konsisten (tersimpan di database, 24 jam).\n\n"
    "Perintah:\n"
    "`/memory` — lihat riwayat & konteks yang tersimpan\n"
    "`/memory clear` — hapus riwayat sekarang\n\n"
    "🔒 Privasi: riwayat otomatis terhapus setelah 24 jam."
)


class MarketBot:
    """
    Main bot class untuk menangani semua interaksi Telegram.
    Now with multi-agent analysis system for richer responses.
    """

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

    # ===================== RATE LIMIT COMMAND AI-HEAVY =====================
    # Interval minimum antar command yang menjalankan pipeline AI / fetch data
    # berat (/morning, /aftermath, /sentiment, /sentimen, /overview, /calendar).
    # Tanpa limit, spam command memicu banyak pipeline LLM paralel → 429 provider
    # free tier + lonjakan RAM di container kecil (Render free 512MB).
    COMMAND_RATE_LIMIT_SECONDS = 15.0

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

    # ===================== COMMAND HANDLERS =====================

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

        status_msg = STATUS_MESSAGE_TEMPLATE.format(
            bot_status="✅ ONLINE",
            uptime=uptime_str,
            total_questions=self.total_questions,
            ai_providers_status=ai_status,
            data_sources_status=data_status,
            cache_stats=f"{cache_stats['active_entries']} entries aktif",
            analysis_engine_status=analysis_status,
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

    # ===================== PENGATURAN (SETTINGS) =====================
    # Satu menu untuk mengatur SEMUA yang bisa diubah user: notifikasi event
    # (/alert), langganan morning brief (/subscribe), dan konteks percakapan
    # (/clear). Tombol inline memakai callback `settings*`; dari reply keyboard
    # label '⚙️ Pengaturan' memicu aksi yang sama.

    SETTINGS_HELP = (
        "⚙️ *PENGATURAN*\n\n"
        "Semua pengaturan bot dalam satu tempat — ketuk tombol untuk mengubah:\n\n"
        "🔔 *Alert Event* — notifikasi otomatis: digest harian + reminder sebelum rilis "
        "+ analisis aftermath + prediksi arah emas\n"
        "🌅 *Morning Brief* — ringkasan pasar otomatis setiap pagi\n"
        "🧠 *Konteks* — hapus riwayat percakapan yang bot ingat (privasi)"
    )

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

        message = (
            "⚙️ *PENGATURAN*\n\n"
            f"🔔 *Alert Event:* {alert_status}\n"
            f"🌅 *Morning Brief:* {brief_status}\n"
            f"{ctx_label}\n\n"
            "Ketuk tombol di bawah untuk mengubah."
        )

        keyboard = [
            [InlineKeyboardButton(alert_btn, callback_data="settings_alert")],
            [InlineKeyboardButton(brief_btn, callback_data="settings_brief")],
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

    # ===================== CLEAR COMMAND =====================

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

    # ===================== ADMIN COMMAND (/stats) =====================
    # Khusus ADMIN_USER_IDS — ringkasan sistem untuk admin: pemakaian token
    # AI, data Supabase (user aktif, subscriber, prediksi), cache, uptime.
    # Data Supabase memanfaatkan kolom aktivitas user (last_active_at /
    # total_questions) yang di-flush batch tiap 10 menit.

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

    # ===================== ADMIN COMMAND (/broadcast) =====================
    # Khusus ADMIN_USER_IDS (env .env / dashboard). Kirim pengumuman ke semua
    # subscriber: morning brief (DB) + notifikasi event (bot_data).
    # Alur 2 langkah demi keamanan: preview jumlah penerima dulu, lalu
    # konfirmasi dengan `/broadcast send` agar pesan tidak terkirim tanpa sengaja.

    BROADCAST_USAGE = (
        "📣 *BROADCAST* (khusus admin)\n\n"
        "Mengirim pesan ke semua subscriber bot (morning brief + alert event).\n\n"
        "Cara pakai:\n"
        "`/broadcast <pesan>` — lihat pratinjau jumlah penerima\n"
        "`/broadcast send <pesan>` — KIRIM sungguhan\n\n"
        "Contoh:\n"
        "`/broadcast send Selamat pagi! Ada update penting dari bot.`"
    )

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

    # ===================== SENTIMENT COMMAND =====================

    async def _get_sentiment_text(self, symbol: str = "FOREX") -> str:
        """Ambil sentimen pasar terformat singkat (aman — tidak crash walau gagal)."""
        try:
            result = await self.sentiment.analyze(symbol, use_llm=True)
            return self.sentiment.format_short(result)
        except Exception as e:
            logger.warning(f"Sentiment analysis failed: {e}")
            return ""

    async def sentiment_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk /sentiment - Skor sentimen pasar berbasis berita."""
        if not await self._check_command_rate_limit(update, context):
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        text = update.message.text or ""
        arg = text.replace("/sentiment", "").replace("/senti", "").strip().lower()

        symbol = "FOREX"
        display_name = "Pasar Forex"
        if arg:
            detected, dname = self.chart.get_chart_symbol_from_text(f"chart {arg}")
            if detected:
                symbol = detected
                display_name = dname
            else:
                # Simbol tidak dikenal — tetap analisis FOREX, label jangan menyesatkan
                display_name = f"{arg.upper()} (kategori umum: Forex)"

        result = await self.sentiment.analyze(symbol, use_llm=True)
        report = strip_markdown_asterisks(self.sentiment.format_report(result, display_name))

        await safe_reply_text(
            update.message,
            report,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # ===================== RETAIL SENTIMENT (OANDA) =====================

    @staticmethod
    def _normalize_symbol_input(text: str) -> str:
        """Normalisasi input simbol: 'EUR/USD', 'eurusd', 'eur usd' → 'eurusd'."""
        return (text or "").strip().lower().replace("/", "").replace(" ", "").replace("-", "")

    def _resolve_symbol_from_text(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Resolve teks user → (yahoo_symbol, display_name).

        Urutan coba:
        1. Keyword map chart (gold, emas, eurusd, dxy, sp500, ...).
        2. Reverse-lookup YAHOO_SYMBOLS (semua pair yang dikenal bot).
        3. Direct match di OANDA_SYMBOLS (mis. 'eurgbp' → EURGBP=X, 'cl' → CL=F)
           agar simbol baru hasil perluasan instrumen tetap bisa dipakai.
        """
        symbol, display = self.chart.get_chart_symbol_from_text(f"chart {text}")
        if symbol:
            return symbol, display

        norm = self._normalize_symbol_input(text)
        if not norm:
            return None, None

        # 2) Reverse-lookup YAHOO_SYMBOLS
        for pair, yahoo in YAHOO_SYMBOLS.items():
            if self._normalize_symbol_input(pair) == norm:
                return yahoo, pair.upper()

        # 3) Direct match OANDA_SYMBOLS: 'eurgbp' → 'EURGBP=X', 'cl' → 'CL=F',
        #    'n225' → '^N225' (display diformat dari simbol bila belum ada).
        for yahoo in OANDA_SYMBOLS:
            base = (
                yahoo.replace("=X", "").replace("=F", "")
                .replace("^", "").replace("-USD", "")
            )
            if base.lower() == norm or yahoo.lower() in (f"{norm}=x", f"{norm}=f"):
                display = ChartGenerator._get_display_name(yahoo)
                base_upper = yahoo.replace("=X", "").replace("=F", "").replace("^", "")
                if display == base_upper:
                    # Belum ada nama tampilan khusus — format manual:
                    # EURGBP=X → EUR/GBP, CL=F → CL
                    if yahoo.endswith("=X") and len(base_upper) == 6:
                        display = f"{base_upper[:3]}/{base_upper[3:]}"
                    else:
                        display = base_upper
                return yahoo, display
        return None, None

    async def retail_sentiment_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler untuk /sentimen — sentimen retail trader (OANDA Position/Order Book).

        Data UNIK dari OANDA: rasio posisi LONG/SHORT trader ritel saat ini + cluster
        pending order. Tidak tersedia di Yahoo — ini fitur andalan bot.

        Usage:
            /sentimen          → EUR/USD (default)
            /sentimen eurusd   → pair tertentu
            /sentimen gbpusd   → GBP/USD

        Hanya pair FOREX yang punya data posisi ritel (gold/index/oil/crypto
        tidak memiliki Position/Order Book di OANDA).
        """
        if not await self._check_command_rate_limit(update, context):
            return
        text = update.message.text or ""
        arg = text.replace("/sentimen", "").strip().lower()

        # Default: EUR/USD; atau deteksi dari argumen
        yahoo_symbol = "EURUSD=X"
        display_name = "EUR/USD"
        if arg and arg not in ("help", "bantuan"):
            detected, dname = self._resolve_symbol_from_text(arg)
            if detected:
                yahoo_symbol = detected
                display_name = dname
            else:
                await safe_reply_text(
                    update.message,
                    "❌ Simbol tidak dikenali. Contoh: `/sentimen eurusd`, `/sentimen gbpusd`.",
                    parse_mode="Markdown",
                )
                return

        instrument = self.market.oanda.instrument_for(yahoo_symbol)
        if not instrument:
            await safe_reply_text(
                update.message,
                f"❌ Sentimen retail OANDA tidak tersedia untuk *{display_name}*.\n\n"
                f"Fitur ini khusus instrumen OANDA (mis. EUR/USD, GBP/USD, USD/JPY).",
                parse_mode="Markdown",
            )
            return

        # Position/Order Book OANDA HANYA ada untuk pair forex — gold, index,
        # oil, crypto tidak punya data posisi ritel. Beri tahu user dengan jelas
        # (bukan menyarankan salah sasaran seperti "cek API key").
        if not self.market.oanda.is_forex(instrument):
            await safe_reply_text(
                update.message,
                f"ℹ️ Sentimen retail (Position/Order Book OANDA) hanya tersedia untuk "
                f"*pair forex* — *{display_name}* bukan pair forex, jadi tidak ada "
                f"data posisi ritel.\n\n"
                f"Coba: `/sentimen eurusd`, `/sentimen gbpusd`, `/sentimen usdjpy`.",
                parse_mode="Markdown",
            )
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        try:
            message = await self._format_retail_sentiment_text(instrument, display_name)
        except Exception as e:
            logger.warning(f"Retail sentiment failed for {instrument}: {e}")
            await safe_reply_text(
                update.message,
                f"❌ Sentimen retail untuk *{display_name}* tidak tersedia saat ini.\n\n"
                f"Pastikan `OANDA_API_KEY` terisi di dashboard deploy (token demo gratis: "
                f"https://www.oanda.com/demo-account/tpa/personal_token).",
                parse_mode="Markdown",
            )
            return

        await safe_reply_text(
            update.message,
            message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    async def _format_retail_sentiment_text(self, instrument: str, display_name: str) -> str:
        """
        Format pesan sentimen retail OANDA (Position/Order Book).
        Dipakai /sentimen dan tombol menu. Raise bila data tidak tersedia.
        """
        sent = await asyncio.to_thread(self.market.oanda.get_retail_sentiment, instrument)
        lines = [f"🧠 *SENTIMEN RETAIL {display_name.upper()}*\n"]

        long_ratio = sent.get("long_ratio")
        if long_ratio is not None:
            short_ratio = sent.get("short_ratio")
            # Kontrarian: mayoritas long → potensi koreksi turun (dan sebaliknya)
            if long_ratio > 55:
                note = "Mayoritas trader ritel LONG → risiko koreksi turun (kontrarian)."
            elif long_ratio < 45:
                note = "Mayoritas trader ritel SHORT → potensi rebound naik (kontrarian)."
            else:
                note = "Posisi ritel hampir seimbang — sinyal lemah."
            lines.append(
                f"📊 *Position Book*\n"
                f"• Long: {long_ratio}%  • Short: {short_ratio}%\n"
                f"💡 {note}"
            )

        buy_ratio = sent.get("buy_ratio")
        if buy_ratio is not None:
            sell_ratio = sent.get("sell_ratio")
            bias = "🟢 Bullish" if buy_ratio > 50 else "🔴 Bearish"
            lines.append(
                f"\n📌 *Order Book* (pending order)\n"
                f"• Buy: {buy_ratio}%  • Sell: {sell_ratio}%\n"
                f"💡 Bias pending order: {bias} — cluster order = zona support/resistance potensial."
            )

        lines.append(
            "\n⚠️ Sentimen ritel sering dipakai *kontrarian*: mayoritas retail biasanya "
            "salah di titik balik pasar. Kombinasikan dengan analisis teknikal."
        )
        return "\n".join(lines)

    # ===================== MORNING BRIEF =====================

    async def alert_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /alert - kelola notifikasi event ekonomi otomatis."""
        chat_id = update.effective_chat.id
        text = update.message.text or ""
        arg = text.replace("/alert", "").strip().lower()

        # Simpan daftar subscriber di bot_data agar bisa diakses job scheduler
        subscribers = context.bot_data.setdefault("event_alert_subscribers", set())
        before = set(subscribers)  # salinan: subscribers dimutasi in-place di bawah

        if arg in ("off", "0", "false", "stop", "matikan", "berhenti"):
            subscribers.discard(chat_id)
            await safe_reply_text(update.message, ALERT_OFF_MESSAGE, parse_mode="Markdown")
        else:
            subscribers.add(chat_id)
            await safe_reply_text(update.message, ALERT_ON_MESSAGE, parse_mode="Markdown")

        context.bot_data["event_alert_subscribers"] = subscribers
        if subscribers != before:
            await self._persist_alert_subscribers(context)

    # ===================== PERSISTENSI STATE (Supabase) =====================
    # Subscriber notifikasi event (/alert) dulunya RAM-only — hilang saat bot
    # restart/deploy. Sekarang disimpan ke DB; helper di bawah best-effort
    # (kegagalan DB tidak boleh mengganggu alur chat).

    async def _persist_alert_subscribers(self, context) -> None:
        """Simpan daftar subscriber event saat ini ke Supabase (best-effort)."""
        try:
            subscribers = context.bot_data.get("event_alert_subscribers", set())
            await db.save_event_alert_subscribers_async(subscribers)
        except Exception as e:
            logger.debug(f"Persist event subscribers gagal: {e}")

    async def calendar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /calendar - Kalender Ekonomi."""
        if not await self._check_command_rate_limit(update, context):
            return
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        try:
            # Kalender ekonomi BULAN INI, hanya event high impact
            # + tombol '📊 Analisis Dampak' untuk event yang tampil
            message, kb = await self._build_calendar_reply()
            kwargs = {"parse_mode": "Markdown", "disable_web_page_preview": True}
            if kb:
                kwargs["reply_markup"] = kb
            await safe_reply_text(update.message, message, **kwargs)
        except Exception as e:
            logger.error(f"Calendar error: {e}")
            await safe_reply_text(
                update.message,
                "❌ Gagal memuat kalender ekonomi. Silakan coba lagi nanti.",
            )

    async def _build_overview_message(self, refresh: bool = False) -> str:
        """
        Bangun pesan overview pasar (dipakai perintah /overview & tombol menu).
        Data dari cache (10 menit) → respons instan tanpa menunggu AI.

        Args:
            refresh: Jika True, lewati cache & ambil harga terbaru.

        Returns:
            String pesan siap kirim (tidak pernah raise — fallback aman).
        """
        try:
            summary = await asyncio.to_thread(self.market.get_market_summary, refresh=refresh)
            now_str = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%A, %d %B %Y %H:%M")
            return (
                f"🌍 *MARKET OVERVIEW*\n"
                f"🕐 {now_str} WIB\n\n"
                f"{summary}\n\n"
                f"💡 Tanyakan analisis spesifik (mis. \"analisis eurusd\").\n"
                f"{DISCLAIMER}"
            )
        except Exception as e:
            logger.error(f"Overview error: {e}")
            return "❌ Gagal memuat overview pasar. Silakan coba lagi nanti."

    async def _build_overview_reply(self, refresh: bool = False) -> Tuple[str, InlineKeyboardMarkup]:
        """Bangun pesan /overview + tombol '🔁 Refresh' (dipakai perintah, menu,
        dan tombol refresh — agar harga bisa dimuat ulang tanpa cache 10 menit)."""
        message = await self._build_overview_message(refresh=refresh)
        return message, self._add_refresh_button(None, callback="overview_refresh")

    async def overview_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler untuk perintah /overview - Ringkasan cepat semua instrumen utama.
        Dibaca dari cache (10 menit), jadi responsnya INSTAN tanpa menunggu AI.
        """
        if not await self._check_command_rate_limit(update, context):
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        message, kb = await self._build_overview_reply()
        await safe_reply_text(
            update.message,
            message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=kb,
        )

    async def morning_brief_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /morning - Morning Brief harian."""
        if not await self._check_command_rate_limit(update, context):
            return
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        try:
            brief = await self._generate_morning_brief()
        except Exception as e:
            # JANGAN diam: user harus dapat umpan balik, bukan keheningan.
            logger.exception(f"Morning brief gagal: {e}")
            await safe_reply_text(
                update.message,
                "⚠️ Morning brief tidak dapat dibuat saat ini. "
                "Coba lagi beberapa saat — kalau berulang, cek /status.",
                parse_mode="Markdown",
            )
            return
        await safe_reply_text(
            update.message,
            brief,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # ===================== /RISK (POSITION SIZE) =====================
    # Kalkulator ukuran posisi — murni komputasi, tanpa AI & tanpa biaya.
    # /risk <modal USD> <risiko%> <SL pips> [simbol] [harga_quote]

    RISK_USAGE = (
        "📐 *POSITION SIZE CALCULATOR*\n\n"
        "Hitung ukuran posisi berdasarkan modal, risiko per trade, dan SL.\n\n"
        "Contoh:\n"
        "`/risk 1000 2 20` — modal $1.000, risiko 2%, SL 20 pips (default XAU/USD)\n"
        "`/risk 500 1 15 EUR/USD`\n"
        "`/risk 1000 2 30 USD/JPY 155` — pair ber-quote JPY butuh harga quote\n\n"
        "Simbol didukung: forex mayor, XAU/USD (Gold), XAG/USD (Silver)."
    )

    async def risk_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /risk — position size calculator (edukasi, tanpa AI)."""
        text = update.message.text or ""
        parts = text.replace("/risk", "").strip().split()
        if not parts or parts[0] in ("help", "bantuan"):
            await safe_reply_text(update.message, self.RISK_USAGE, parse_mode="Markdown")
            return
        try:
            balance = float(parts[0])
            risk_pct = float(parts[1]) if len(parts) > 1 else 1.0
            sl_pips = float(parts[2]) if len(parts) > 2 else 20.0
        except (ValueError, IndexError):
            await safe_reply_text(update.message, self.RISK_USAGE, parse_mode="Markdown")
            return
        symbol = parts[3] if len(parts) > 3 else "XAU/USD"
        price_quote = float(parts[4]) if len(parts) > 4 else None
        result = calculate_position_size(balance, risk_pct, sl_pips, symbol, price_quote)
        await safe_reply_text(
            update.message,
            format_risk_result(result),
            parse_mode="Markdown",
        )

    # ===================== /PIVOT (LEVEL KUNCI) =====================
    # Ekspos perhitungan pivot/fib yang SUDAH ada di analysis/indicators.py.

    async def pivot_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /pivot [simbol] — pivot point & level kunci (tanpa AI)."""
        text = update.message.text or ""
        arg = text.replace("/pivot", "").strip()
        yahoo_symbol, display = self._resolve_symbol_from_text(arg or "XAU/USD")
        if not yahoo_symbol:
            await safe_reply_text(
                update.message,
                f"❌ Simbol *{arg or 'XAU/USD'}* tidak dikenali.",
                parse_mode="Markdown",
            )
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        try:
            ohlcv = await asyncio.to_thread(
                self.market.get_ohlcv_history, yahoo_symbol, period="1mo", interval="1d", limit=30
            )
            ind = compute_indicators(ohlcv)
        except Exception as e:
            logger.warning(f"Pivot data error: {e}")
            ind = {}
        price = ind.get("current_price")
        if price is None:
            await safe_reply_text(
                update.message,
                f"❌ Data untuk *{display}* tidak tersedia saat ini. Coba lagi nanti.",
                parse_mode="Markdown",
            )
            return
        levels = format_key_levels(ind)
        ema20, ema50 = ind.get("ema_20"), ind.get("ema_50")
        trend = "📈 Bullish" if (ema20 and ema50 and ema20 > ema50) else "📉 Bearish" if (ema20 and ema50) else "➖"
        rsi = ind.get("rsi")
        rsi_txt = f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI: —"
        msg = (
            f"📐 *PIVOT & LEVEL KUNCI {display}*\n\n"
            f"💰 Harga: {format_price(price)}\n"
            f"{trend} (EMA20 vs EMA50)\n"
            f"📊 {rsi_txt}\n\n"
            f"{levels}\n\n"
            f"⚠️ Edukasi — bukan saran trading."
        )
        await safe_reply_text(update.message, msg, parse_mode="Markdown")

    # ===================== /MAP (MARKET HEATMAP) =====================
    # Ringkasan instrumen utama dalam satu pesan — tanpa AI, data dari cache.

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

    @staticmethod
    def _format_map_row(label: str, ind: Dict) -> str:
        """Satu baris heatmap dari hasil compute_indicators (murni, mudah di-test)."""
        if not ind or ind.get("current_price") is None:
            return f"{label:<10} ❌ data tidak tersedia"
        price = ind["current_price"]
        chg = ind.get("price_5d_change")
        chg_txt = f"{chg:+.2f}%" if chg is not None else "—"
        rsi = ind.get("rsi")
        rsi_txt = f"{rsi:.0f}" if rsi is not None else "—"
        ema20, ema50 = ind.get("ema_20"), ind.get("ema_50")
        if ema20 and ema50:
            arrow = "📈" if ema20 > ema50 else "📉"
        else:
            arrow = "➖"
        return f"{label:<10} {price:>12,.4f}  {chg_txt:>8}  RSI {rsi_txt:>4}  {arrow}"

    async def map_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /map — heatmap instan semua instrumen utama (tanpa AI)."""
        if not await self._check_command_rate_limit(update, context):
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        def _fetch(label: str, key: str) -> str:
            try:
                yahoo = YAHOO_SYMBOLS.get(key)
                if not yahoo:
                    return MarketBot._format_map_row(label, {})
                ohlcv = self.market.get_ohlcv_history(yahoo, period="1mo", interval="1d", limit=30)
                return MarketBot._format_map_row(label, compute_indicators(ohlcv))
            except Exception:
                return MarketBot._format_map_row(label, {})

        rows = await asyncio.gather(
            *(asyncio.to_thread(_fetch, label, key) for label, key in self.MAP_INSTRUMENTS)
        )
        now_str = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%A, %d %B %Y %H:%M")
        msg = (
            f"🗺️ *MARKET HEATMAP*\n"
            f"🕐 {now_str} WIB\n\n"
            f"```\n" + "\n".join(rows) + "\n```\n\n"
            f"RSI >70 overbought • <30 oversold • 5d = perubahan 5 hari.\n"
            f"⚠️ Edukasi — bukan saran trading."
        )
        await safe_reply_text(update.message, msg, parse_mode="Markdown")

    # ===================== TRADING JOURNAL =====================
    # Catatan transaksi per user (butuh Supabase tabel `journal`; tanpa DB
    # command ini menampilkan pesan ramah). Tanpa AI.

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

    @staticmethod
    def _journal_stats(entries: list) -> dict:
        """Rekap statistik journal (murni, mudah di-test)."""
        closed = [e for e in entries if e.get("status") == "closed"]
        wins = [e for e in closed if e.get("result") == "win"]
        losses = [e for e in closed if e.get("result") == "loss"]
        by_pair: Dict[str, dict] = {}
        for e in closed:
            sym = (e.get("symbol") or "?").upper()
            d = by_pair.setdefault(sym, {"wins": 0, "losses": 0, "pnl_pct": 0.0})
            if e.get("result") == "win":
                d["wins"] += 1
            elif e.get("result") == "loss":
                d["losses"] += 1
            try:
                d["pnl_pct"] += float(e.get("pnl_pct") or 0)
            except (TypeError, ValueError):
                pass
        return {
            "total": len(entries),
            "open": sum(1 for e in entries if e.get("status") != "closed"),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed) * 100) if closed else None,
            "total_pnl_pct": sum(
                float(e.get("pnl_pct") or 0) for e in closed
            ),
            "by_pair": by_pair,
        }

    async def journal_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /journal — catatan transaksi per user."""
        text = update.message.text or ""
        parts = text.replace("/journal", "").strip().split()
        if not parts or parts[0] in ("help", "bantuan"):
            await safe_reply_text(update.message, self.JOURNAL_USAGE, parse_mode="Markdown")
            return
        cmd = parts[0].lower()
        user_id = update.effective_user.id
        args = parts[1:]
        if cmd == "add":
            await self._journal_add(update, user_id, args)
        elif cmd == "close":
            await self._journal_close(update, user_id, args)
        elif cmd == "list":
            await self._journal_list(update, user_id)
        elif cmd == "stats":
            await self._journal_stats_reply(update, user_id)
        elif cmd == "del":
            await self._journal_del(update, user_id, args)
        else:
            await safe_reply_text(
                update.message,
                f"❌ Sub-perintah `{cmd}` tidak dikenal.\n\n{self.JOURNAL_USAGE}",
                parse_mode="Markdown",
            )

    async def _journal_add(self, update, user_id: int, args: list):
        if len(args) < 3:
            await safe_reply_text(update.message, self.JOURNAL_USAGE, parse_mode="Markdown")
            return
        symbol_text = args[0]
        direction = args[1].lower()
        if direction not in ("long", "buy", "short", "sell"):
            await safe_reply_text(update.message, "❌ Arah harus `long` atau `short`.", parse_mode="Markdown")
            return
        try:
            entry = float(args[2])
        except ValueError:
            await safe_reply_text(update.message, "❌ Harga entry harus angka.", parse_mode="Markdown")
            return
        try:
            sl = float(args[3]) if len(args) > 3 else None
            tp = float(args[4]) if len(args) > 4 else None
            lot = float(args[5]) if len(args) > 5 else None
        except ValueError:
            await safe_reply_text(update.message, "❌ SL/TP/lot harus angka (atau kosongkan).", parse_mode="Markdown")
            return
        _yahoo, display = self._resolve_symbol_from_text(symbol_text)
        record = {
            "user_id": user_id,
            "symbol": (display or symbol_text).upper(),
            "direction": "long" if direction in ("long", "buy") else "short",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "lot": lot,
            "status": "open",
        }
        ok = await db.add_journal_entry_async(record)
        if ok:
            await safe_reply_text(
                update.message,
                f"✅ Journal tersimpan: *{record['symbol']}* {record['direction'].upper()} @ {format_price(entry)}.",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(
                update.message,
                "❌ Gagal menyimpan — database belum dikonfigurasi? Jalankan `migrations/supabase.sql`.",
                parse_mode="Markdown",
            )

    async def _journal_close(self, update, user_id: int, args: list):
        if not args:
            await safe_reply_text(update.message, "❌ Contoh: `/journal close 12 2410`", parse_mode="Markdown")
            return
        try:
            entry_id = int(args[0])
        except ValueError:
            await safe_reply_text(update.message, "❌ ID harus angka (lihat `/journal list`).", parse_mode="Markdown")
            return
        try:
            exit_price = float(args[1]) if len(args) > 1 else None
        except ValueError:
            await safe_reply_text(update.message, "❌ Harga exit harus angka.", parse_mode="Markdown")
            return
        if exit_price is None:
            await safe_reply_text(
                update.message,
                "❌ Berikan harga exit: `/journal close <id> <harga>`",
                parse_mode="Markdown",
            )
            return
        entries = await db.list_journal_entries_async(user_id, limit=50)
        rec = next((e for e in entries if int(e.get("id", 0)) == entry_id), None)
        if not rec:
            await safe_reply_text(update.message, f"❌ Entri id `{entry_id}` tidak ditemukan.", parse_mode="Markdown")
            return
        if rec.get("status") == "closed":
            await safe_reply_text(update.message, f"ℹ️ Entri id `{entry_id}` sudah ditutup.", parse_mode="Markdown")
            return
        entry = float(rec.get("entry") or 0)
        direction = rec.get("direction", "long")
        pnl_pct = (
            (exit_price - entry) / entry * 100 if direction == "long" else (entry - exit_price) / entry * 100
        )
        result = "win" if pnl_pct >= 0 else "loss"
        ok = await db.close_journal_entry_async(entry_id, user_id, exit_price, result, pnl_pct)
        if ok:
            emoji = "🟢" if result == "win" else "🔴"
            await safe_reply_text(
                update.message,
                f"{emoji} Entri `{entry_id}` ditutup @ {format_price(exit_price)} — "
                f"{result.upper()} ({pnl_pct:+.2f}%)",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(update.message, "❌ Gagal menutup entri (database?).", parse_mode="Markdown")

    async def _journal_list(self, update, user_id: int):
        entries = await db.list_journal_entries_async(user_id, limit=20)
        if not entries:
            await safe_reply_text(
                update.message,
                "📓 Journal masih kosong. Mulai: `/journal add XAU/USD long 2400 2390 2420 0.5`",
                parse_mode="Markdown",
            )
            return
        lines = ["📓 *JOURNAL (20 terakhir)*\n"]
        for e in entries:
            eid = e.get("id")
            sym = (e.get("symbol") or "?").upper()
            direction = e.get("direction", "long").upper()
            entry = format_price(e.get("entry"))
            status = e.get("status", "open")
            if status == "closed":
                result = e.get("result", "?")
                pnl = e.get("pnl_pct")
                pnl_txt = f"({pnl:+.2f}%)" if pnl is not None else ""
                lines.append(f"`{eid}` {sym} {direction} {entry} → {format_price(e.get('exit_price'))} {result.upper()} {pnl_txt}")
            else:
                sl = format_price(e.get("sl")) if e.get("sl") else "—"
                tp = format_price(e.get("tp")) if e.get("tp") else "—"
                lines.append(f"`{eid}` {sym} {direction} {entry} | SL {sl} TP {tp} | 🔓 open")
        await safe_reply_text(update.message, "\n".join(lines), parse_mode="Markdown")

    async def _journal_stats_reply(self, update, user_id: int):
        entries = await db.list_journal_entries_async(user_id, limit=500)
        if not entries:
            await safe_reply_text(update.message, "📓 Journal masih kosong.", parse_mode="Markdown")
            return
        s = self._journal_stats(entries)
        lines = [
            "📊 *STATISTIK JOURNAL*\n",
            f"Total: {s['total']} ({s['open']} open, {s['closed']} closed)",
        ]
        if s["closed"]:
            wr = s["win_rate"]
            lines.append(f"Win rate: {wr:.0f}% ({s['wins']}W / {s['losses']}L)")
            lines.append(f"Total PnL: {s['total_pnl_pct']:+.2f}%\n")
            lines.append("*Per pair:*")
            for sym, d in sorted(s["by_pair"].items(), key=lambda kv: -kv[1]["pnl_pct"]):
                lines.append(
                    f"• {sym}: {d['wins']}W/{d['losses']}L — {d['pnl_pct']:+.2f}%"
                )
        else:
            lines.append("Belum ada posisi yang ditutup.")
        lines.append("\n⚠️ Edukasi — bukan saran trading.")
        await safe_reply_text(update.message, "\n".join(lines), parse_mode="Markdown")

    async def _journal_del(self, update, user_id: int, args: list):
        if not args:
            await safe_reply_text(update.message, "❌ Contoh: `/journal del 12`", parse_mode="Markdown")
            return
        try:
            entry_id = int(args[0])
        except ValueError:
            await safe_reply_text(update.message, "❌ ID harus angka.", parse_mode="Markdown")
            return
        ok = await db.delete_journal_entry_async(entry_id, user_id)
        if ok:
            await safe_reply_text(update.message, f"🗑️ Entri `{entry_id}` dihapus.", parse_mode="Markdown")
        else:
            await safe_reply_text(update.message, "❌ Gagal menghapus (id salah / database?).", parse_mode="Markdown")

    # ===================== MARKET SESSION ALERTS =====================
    # Notifikasi "sesi market buka" (Sydney/Tokyo/London/New York) ke
    # subscriber morning brief — tanpa AI, dedup per (sesi, tanggal).

    async def send_session_alerts(self, application: Application):
        """Kirim alert sesi market baru buka ke subscriber (job terjadwal)."""
        try:
            now_utc = datetime.now(timezone.utc)
            opened = sessions_just_opened(now_utc)
            if not opened:
                return
            subscribers = await db.get_all_subscribers_async()
            if not subscribers:
                return
            # Dedup per (sesi, tanggal) — prune kunci lama agar set tidak membengkak
            today = now_utc.strftime("%Y-%m-%d")
            yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
            sent_keys = set(application.bot_data.get("session_alert_sent", set()))
            sent_keys = {k for k in sent_keys if k.split("|")[-1] in (today, yesterday)}
            to_send = []
            for s in opened:
                key = f"{s.key}|{today}"
                if key not in sent_keys:
                    sent_keys.add(key)
                    to_send.append(s)
            application.bot_data["session_alert_sent"] = sent_keys
            for s in to_send:
                text = format_session_text(s, MORNING_BRIEF_TIMEZONE)
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot,
                            chat_id=chat_id,
                            text=text,
                            parse_mode="Markdown",
                        )
                    except Exception as ex:
                        logger.error(f"Gagal kirim alert sesi ke {chat_id}: {ex}")
        except Exception as e:
            logger.warning(f"Session alert error: {e}")

    async def _generate_morning_brief(self) -> str:
        """
        Generate morning brief dengan data terkini.
        Menggabungkan data pasar, makro, kalender ekonomi, berita, dan AI-generated outlook.

        Defensif penuh: SATU sumber data error TIDAK boleh menggagalkan seluruh
        brief (tiap bagian punya fallback teks), dan kegagalan AI menghasilkan
        placeholder ramah — bukan exception yang membuat /morning tidak merespons.
        """
        # Tanggal harus sesuai zona WIB, bukan waktu server (yang bisa UTC)
        today = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%A, %d %B %Y")

        # Gather data secara parallel — return_exceptions: satu sumber gagal
        # (jaringan Yahoo/FRED/news) tidak membatalkan bagian lain.
        results = await asyncio.gather(
            asyncio.to_thread(self.market.get_market_summary),
            asyncio.to_thread(self.macro.get_macro_summary),
            self.macro.get_economic_calendar(),
            self.news.get_news_summary("FOREX"),
            self._get_sentiment_text("FOREX"),
            return_exceptions=True,
        )
        market_summary, macro_summary, calendar_events, news_summary, sentiment_text = results

        # Normalisasi exception → fallback teks per-bagian
        if isinstance(market_summary, Exception):
            logger.warning(f"Morning brief: market summary gagal: {market_summary}")
            market_summary = "📊 Data pasar tidak tersedia saat ini."
        if isinstance(macro_summary, Exception):
            logger.warning(f"Morning brief: macro summary gagal: {macro_summary}")
            macro_summary = "🏛️ Data makro tidak tersedia saat ini."
        if isinstance(calendar_events, Exception):
            logger.warning(f"Morning brief: kalender ekonomi gagal: {calendar_events}")
            calendar_events = []
        if isinstance(news_summary, Exception):
            logger.warning(f"Morning brief: berita gagal: {news_summary}")
            news_summary = "📰 Berita tidak tersedia saat ini."
        if isinstance(sentiment_text, Exception):
            sentiment_text = ""

        # Format kalender ekonomi untuk morning brief (top 3 high impact)
        try:
            calendar_text = self.macro.format_calendar_text(calendar_events, max_events=3)
        except Exception as e:
            logger.warning(f"Morning brief: format kalender gagal: {e}")
            calendar_text = "📅 Tidak ada event terjadwal yang tersedia."

        # AI-powered outlook & catalysts using multi-agent analysis
        if self.analysis_director:
            try:
                # Gunakan multi-agent untuk analisis yang lebih dalam
                analysis_prompt = self._build_morning_brief_prompt(
                    today, market_summary, macro_summary, calendar_text, news_summary, sentiment_text
                )

                result = await self.analysis_director.analyze(analysis_prompt)

                # Analisis GAGAL (mis. semua AI provider rate-limit) → jangan
                # tampilkan pesan error yang meng-echo prompt morning brief
                # sebagai "outlook" — jatuh ke path legacy yang lebih ringan.
                # getattr: result bisa AnalysisResult (punya .error) atau objek
                # serupa lain (test/fake) yang tidak punya atribut itu.
                if getattr(result, "error", None):
                    logger.warning(
                        f"Multi-agent morning brief gagal: {result.error} — fallback legacy"
                    )
                    raise RuntimeError(f"Multi-agent analysis failed: {result.error}")

                # Extract from analysis result (bersihkan prefix [via ...] + simbol *)
                ai_content = strip_markdown_asterisks(_strip_provider_prefix(result.final_response or ""))
                outlook_part, catalysts_part = self._split_outlook_catalysts(ai_content)

                return MORNING_BRIEF_TEMPLATE.format(
                    date=today,
                    market_summary=market_summary,
                    macro_summary=macro_summary,
                    calendar_summary=calendar_text,
                    news_summary=news_summary,
                    sentiment_summary=sentiment_text or "Sentimen pasar tidak tersedia.",
                    outlook=outlook_part,
                    catalysts=catalysts_part,
                )
            except Exception as e:
                logger.warning(f"Multi-agent morning brief failed: {e}, falling back to legacy")

        # Fallback: legacy single-prompt method — pakai generate_async agar tidak
        # memblokir event loop (generate sync bisa berjalan 60s+ saat provider
        # down). Kegagalan AI → placeholder ramah, bukan exception.
        try:
            outlook_prompt = self._build_morning_brief_prompt(
                today, market_summary, macro_summary, calendar_text, news_summary, sentiment_text
            )
            ai_response = await self.ai.generate_async(
                outlook_prompt, use_cache=True, max_tokens=2048
            )
            ai_content = strip_markdown_asterisks(_strip_provider_prefix(ai_response))
            outlook, catalysts = self._split_outlook_catalysts(ai_content)
        except Exception as e:
            logger.warning(f"Legacy morning brief AI failed: {e}")
            outlook = "Analisis AI tidak tersedia saat ini — data pasar tetap disajikan di atas."
            catalysts = "Coba lagi dalam beberapa menit."

        return MORNING_BRIEF_TEMPLATE.format(
            date=today,
            market_summary=market_summary,
            macro_summary=macro_summary,
            calendar_summary=calendar_text,
            news_summary=news_summary,
            sentiment_summary=sentiment_text or "Sentimen pasar tidak tersedia.",
            outlook=outlook,
            catalysts=catalysts,
        )

    @staticmethod
    def _split_outlook_catalysts(ai_content: str) -> Tuple[str, str]:
        """Pisah konten AI → (outlook, catalysts) TANPA memotong konten.

        Marker `KATALIS UTAMA:` memisahkan dua bagian. Bila marker tidak ada,
        seluruh konten dianggap outlook (catalysts → placeholder).
        """
        ai_content = (ai_content or "").strip()
        if "KATALIS UTAMA" in ai_content:
            sections = ai_content.split("KATALIS UTAMA:")
            outlook = sections[0].replace("OUTLOOK:", "").replace("OUTLOOK", "").strip()
            catalysts = sections[1].strip() if len(sections) > 1 else ""
        else:
            outlook = ai_content
            catalysts = ""
        if not outlook:
            outlook = "Belum ada data analisis untuk hari ini."
        if not catalysts:
            catalysts = "Belum ada katalis utama yang teridentifikasi hari ini."
        return outlook, catalysts

    def _build_morning_brief_prompt(
        self,
        today: str,
        market_summary: str,
        macro_summary: str,
        calendar_text: str,
        news_summary: str,
        sentiment_text: str = "",
    ) -> str:
        """
        Bangun prompt morning brief (dipakai path multi-agent & legacy).

        Konten prompt DIAMBIL dari `prompts/morning_brief.txt` (single source
        of truth) — edit file tersebut untuk mengubah perilaku tanpa mengubah
        kode. Fallback ke template bawaan bila file tidak tersedia.
        """
        sentiment_section = sentiment_text or "Sentimen pasar tidak tersedia."
        return format_prompt(
            "morning_brief",
            DATE=today,
            market_data=market_summary,
            macro_data=macro_summary,
            calendar_data=calendar_text,
            news_data=news_summary,
            sentiment_data=sentiment_section,
        )

    # ===================== REPLY KEYBOARD MENU ACTION =====================
    # Tombol reply keyboard mengirim LABEL sebagai teks pesan. handle_message
    # mendeteksi label → method ini menjalankan aksi yang sama dengan tombol
    # inline menu (callback handle_callback). Aksi yang punya command handler
    # di-reuse langsung agar tidak ada duplikasi logika.

    async def _run_keyboard_menu_action(self, action: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Jalankan aksi tombol reply keyboard (menu di keyboard bawah)."""
        if action == "gold_price":
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            gold_data = await asyncio.to_thread(self.market.get_yahoo_data, "GC=F", period="1wk")
            formatted = self._format_market_data(gold_data)
            await safe_reply_text(
                update.message,
                f"🥇 *HARGA GOLD (XAU/USD)*\n\n{formatted}\n\n{await self.news.get_news_summary('GC=F')}\n{DISCLAIMER}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        elif action == "eurusd":
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            eur_data = await asyncio.to_thread(self.market.get_yahoo_data, "EURUSD=X", period="1wk")
            formatted = self._format_market_data(eur_data)
            await safe_reply_text(
                update.message,
                f"💱 *EUR/USD*\n\n{formatted}\n{DISCLAIMER}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        elif action == "macro":
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            macro_summary = await asyncio.to_thread(self.macro.get_macro_summary)
            await safe_reply_text(
                update.message,
                f"🏛️ *DATA MAKROEKONOMI*\n\n{macro_summary}\n{DISCLAIMER}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        elif action == "sentiment":
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            result = await self.sentiment.analyze("FOREX", use_llm=True)
            report = strip_markdown_asterisks(self.sentiment.format_report(result, "Pasar Forex"))
            await safe_reply_text(
                update.message,
                report,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        elif action == "overview":
            await self.overview_command(update, context)
        elif action == "calendar":
            await self.calendar_command(update, context)
        elif action == "morning":
            await self.morning_brief_command(update, context)
        elif action == "prediksi":
            await self.prediksi_command(update, context)
        elif action == "alert_on":
            # Sama dengan tombol inline alert_on: aktifkan notifikasi event
            chat_id = update.effective_chat.id
            subscribers = context.bot_data.setdefault("event_alert_subscribers", set())
            before = set(subscribers)
            subscribers.add(chat_id)
            context.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before:
                await self._persist_alert_subscribers(context)
            await update.message.reply_text(ALERT_ON_MESSAGE, parse_mode="Markdown")
        elif action == "settings":
            # Menu pengaturan dari reply keyboard — kirim sebagai pesan baru
            message, kb = await self._build_settings_menu(update, context)
            await safe_reply_text(
                update.message,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )
        elif action == "help":
            await self.help_command(update, context)

    # ===================== MESSAGE HANDLER =====================

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler utama untuk semua pesan text dari user.
        Menggunakan multi-agent analysis system (jika enabled) untuk analisis yang lebih dalam.
        """
        user_question = update.message.text
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        # Bersihkan & batasi input user (anti prompt-injection / input raksasa)
        user_question = sanitize_text(user_question, max_length=500)

        logger.info(f"Question from user {user_id}: {user_question[:100]}")

        # Rate limiting
        user_data = context.user_data
        last_msg_time = user_data.get("last_message_time", 0)
        if time.time() - last_msg_time < 2:
            await update.message.reply_text(RATE_LIMIT_MESSAGE, parse_mode="Markdown")
            return

        user_data["last_message_time"] = time.time()
        self.total_questions += 1
        # Aktivitas per-user (in-memory, di-flush batch ke Supabase berkala)
        self._track_user_activity(user_id)

        # ===== REPLY KEYBOARD MENU: label tombol dikirim sebagai teks =====
        # Tombol di keyboard bawah (Reply Keyboard) mengirim label sebagai pesan
        # teks biasa. Kenali label tersebut dan jalankan aksi menu yang sama
        # dengan tombol inline (tanpa pipeline AI — instan).
        menu_action = MENU_KEYBOARD_ACTIONS.get(user_question)
        if menu_action:
            await self._run_keyboard_menu_action(menu_action, update, context)
            return

        # ===== FAST PATH: pertanyaan harga sederhana (tanpa AI) =====
        # Cek harga biasa ("berapa harga eurusd?") dijawab INSTAN dari data
        # cache — user tidak perlu menunggu pipeline multi-agent (5-15 detik).
        fast_hit = detect_fast_price_query(user_question)
        if fast_hit:
            symbol, display_name = fast_hit
            fast_answer = await self._format_fast_price_answer(symbol, display_name)
            if fast_answer:
                add_exchange(user_id, user_question, strip_markdown_asterisks(fast_answer))
                await safe_reply_text(
                    update.message,
                    fast_answer,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=_quick_action_keyboard(fast_hit[0]),
                )
                return
            # Gagal ambil data → lanjut ke pipeline analisis penuh

        # Simbol untuk tombol aksi cepat: deteksi dari pertanyaan, fallback ke
        # konteks percakapan user (agar tombol tetap relevan walau tanpa instrumen
        # eksplisit — mis. follow-up "support-nya berapa?").
        quick_symbol = None
        detected_pairs = self._detect_pairs(user_question)
        if detected_pairs:
            quick_symbol = detected_pairs[0][1]
        else:
            _sym, _name = self.chart.get_chart_symbol_from_text(user_question)
            quick_symbol = _sym
        if not quick_symbol:
            quick_symbol = label_to_symbol(get_context(user_id).get("asset_focus"))

        # Riwayat percakapan user (untuk konteks follow-up)
        history_text = format_history(user_id)

        # Kuota harian per-user — cek SEBELUM pipeline AI. Fast price path di
        # atas tidak dihitung; hanya pertanyaan yang benar-benar memakai AI
        # yang memakai kuota (di-consume tepat sebelum pipeline dijalankan).
        if self._daily_quota_exceeded(user_id):
            await update.message.reply_text(
                f"⏳ Kuota harian *{USER_DAILY_QUOTA}* pertanyaan sudah habis. "
                f"Kembali lagi besok ya 🙏",
                parse_mode="Markdown",
            )
            return

        # Typing indicator
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action="typing",
        )

        # Pesan progress — diedit menjadi jawaban akhir setelah analisis selesai
        progress = None
        try:
            progress = await update.message.reply_text(
                "🔍 Menganalisis pertanyaan Anda... mohon tunggu sebentar."
            )
        except Exception:
            progress = None

        # Jawaban inti (tanpa badge/disclaimer) untuk disimpan ke memory
        core_answer = ""

        try:
            # Pertanyaan ini benar-benar memakai pipeline AI → pakai 1 kuota
            self._consume_daily_quota(user_id)
            if self.analysis_director and ENABLE_MULTI_AGENT:
                # ===== NEW: Multi-Agent Analysis Pipeline =====
                logger.info(f"Using multi-agent analysis for: {user_question[:80]}...")

                # Get OHLCV data if relevant for signal analysis.
                # Pakai riwayat dalam (60 bar, 3 bulan harian) agar indikator
                # teknikal (RSI 14, MACD 26, EMA50, Bollinger) punya cukup data
                # untuk dihitung secara matematis — bukan ditebak LLM.
                # Simbol sudah terdeteksi di atas (quick_symbol, termasuk
                # fallback konteks multi-turn user) — pakai ulang, jangan
                # mendeteksi dua kali.
                ohlcv_data = None
                pair_symbol = quick_symbol
                if pair_symbol:
                    ohlcv_data = await asyncio.to_thread(
                        self.market.get_ohlcv_history, pair_symbol, period="3mo", interval="1d", limit=60
                    )

                # Run multi-agent analysis (dengan konteks percakapan)
                result = await self.analysis_director.analyze(
                    question=user_question,
                    market_data_ohlcv=ohlcv_data,
                    conversation_history=history_text,
                )

                final_message = result.final_response
                core_answer = result.final_response

                # Add agent signature if multiple agents were used
                if len(result.agents_executed) > 1:
                    agent_icons = {
                        "research": "🔍",
                        "signals": "📊",
                        "thesis": "💡",
                        "contradiction": "⚠️",
                        "scenarios": "🔮",
                        "confidence": "📈",
                        "risk_gates": "🛡️",
                    }
                    agent_badges = " | ".join(
                        f"{agent_icons.get(a, '🤖')} {a.title()}" for a in result.agents_executed
                    )
                    final_message += f"\n\n⚡ *Multi-Agent:* {agent_badges}"

                final_message += f"\n{DISCLAIMER}"

            else:
                # ===== LEGACY: Single-prompt method =====
                data_context = await self._gather_context(user_question)
                prompt = self._build_prompt(user_question, data_context, history_text)

                answer = await asyncio.to_thread(
                    self.ai.generate,
                    prompt,
                    max_retries=3,
                    use_cache=True,
                )

                # FACT CHECK: angka harga/level di jawaban dicek terhadap data
                # yang diberikan ke prompt (anti-halusinasi deterministik).
                fact_note = build_fact_check_note(
                    answer, [data_context, user_question, history_text]
                )
                if fact_note:
                    answer += fact_note

                core_answer = answer
                final_message = f"{answer}{DISCLAIMER}"

            # Hapus simbol '*' (markdown bold) dari jawaban agar tidak tampil mentah
            final_message = strip_markdown_asterisks(final_message)

            # Simpan JAWABAN INTI (tanpa badge multi-agent, disclaimer, dan catatan
            # verifikasi data) ke memory agar kuota karakter tidak habis oleh
            # boilerplate dan meta-info tidak jadi konteks pertanyaan berikutnya.
            if core_answer:
                add_exchange(
                    user_id,
                    user_question,
                    strip_markdown_asterisks(strip_fact_check_note(core_answer)),
                )

            # Send response — edit pesan progress menjadi jawaban akhir.
            # Tombol aksi cepat (S/R, Skenario, Bersihkan) memakai konteks
            # multi-turn user sehingga follow-up tidak perlu sebut instrumen lagi.
            if progress is not None:
                await _edit_progress_message(
                    progress,
                    final_message,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=_quick_action_keyboard(quick_symbol),
                )
            else:
                await safe_reply_text(
                    update.message,
                    final_message,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=_quick_action_keyboard(quick_symbol),
                )

        except asyncio.TimeoutError:
            msg = "⏰ Maaf, permintaan timeout. Silakan coba lagi dengan pertanyaan yang lebih spesifik."
            if progress is not None:
                await _edit_progress_message(progress, msg)
            else:
                await safe_reply_text(update.message, msg)
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            msg = f"{ERROR_MESSAGE}\n\nDetail teknis: {str(e)[:100]}"
            if progress is not None:
                await _edit_progress_message(progress, msg, parse_mode="Markdown")
            else:
                await safe_reply_text(update.message, msg, parse_mode="Markdown")

    def _detect_pairs(self, question: str) -> list:
        """Detect forex pairs mentioned in question — dengan ATAU tanpa garis miring
        ('EUR/USD', 'eurusd', maupun 'usd jpy' semuanya dikenali)."""
        q = question.lower()
        detected = []
        for pair, symbol in YAHOO_SYMBOLS.items():
            if pair in q:
                detected.append((pair, symbol))
        if not detected:
            # Tanpa slash: "eurusd" / "usd jpy" → cocokkan tanpa spasi & garis miring
            compact = q.replace(" ", "")
            for pair, symbol in YAHOO_SYMBOLS.items():
                key = pair.replace("/", "")
                if key and key in compact:
                    detected.append((pair, symbol))
        return detected[:3]

    # ===================== LEGACY CONTEXT GATHERING =====================

    async def _gather_context(self, question: str) -> str:
        """
        Kumpulkan data relevan berdasarkan pertanyaan user.
        (Legacy method, used when multi-agent is disabled)
        """
        question_lower = question.lower()
        context_parts = []
        fetch_tasks = []

        # ===== DETECT FOREX/GOLD MENTIONS =====
        detected_pairs = self._detect_pairs(question)

        if detected_pairs:
            async def fetch_pair_data(pair_name, symbol):
                data = await asyncio.to_thread(
                    self.market.get_yahoo_data, symbol, period="1mo"
                )
                return f"📊 *DATA {pair_name.upper()}*:\n" + self._format_market_data(data)

            for pair_name, symbol in detected_pairs[:3]:
                fetch_tasks.append(fetch_pair_data(pair_name, symbol))

        # ===== DETECT MACRO KEYWORDS =====
        detected_macro = []
        for keyword, series_id in FRED_INDICATORS.items():
            if keyword in question_lower:
                detected_macro.append((keyword, series_id))

        if detected_macro:
            async def fetch_macro_data(keyword, series_id):
                data = self.macro.get_fred_data(series_id)
                if "error" not in data:
                    return (
                        f"🏛️ *DATA MAKRO {keyword.upper()}*:\n"
                        f"• Nilai: {data.get('latest_value', 'N/A')}\n"
                        f"• Tanggal: {data.get('latest_date', 'N/A')}\n"
                        f"• Perubahan: {data.get('change', 'N/A')}\n"
                    )
                return ""

            for keyword, series_id in detected_macro[:3]:
                fetch_tasks.append(fetch_macro_data(keyword, series_id))

        # ===== DETECT NEWS/SENTIMENT =====
        news_keywords = ["berita", "news", "sentimen", "hari ini", "headline", "terkini"]
        if any(word in question_lower for word in news_keywords):
            fetch_tasks.append(self.news.get_news_summary("FOREX"))

        # ===== FETCH ALL IN PARALLEL =====
        if fetch_tasks:
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, str) and result:
                    context_parts.append(result)
                elif isinstance(result, Exception):
                    logger.warning(f"Fetch error: {result}")
        else:
            context_parts.append(
                "ℹ️ *CATATAN*: Tidak ada data pasar spesifik yang terdeteksi untuk pertanyaan ini. "
                "Jawab berdasarkan pengetahuan umum."
            )

        return "\n\n".join(context_parts) if context_parts else "Tidak ada data spesifik yang ditemukan."

    async def _format_fast_price_answer(self, symbol: str, display_name: str) -> Optional[str]:
        """
        Format jawaban harga instan dari data cache (tanpa AI call).

        Returns:
            String jawaban siap kirim, atau None jika data tidak tersedia
            (caller akan fallback ke pipeline analisis penuh).
        """
        try:
            data = await asyncio.to_thread(
                self.market.get_yahoo_data, symbol, period="2d", interval="1h"
            )
        except Exception as e:
            logger.warning(f"Fast price fetch failed for {symbol}: {e}")
            return None

        price = data.get("current_price")
        if "error" in data or price is None:
            return None

        change = data.get("change_pct")
        arrow = "🟢" if change and change > 0 else "🔴" if change and change < 0 else "⚪"
        change_str = f"{change:+.2f}%" if change is not None else ""

        # Bid/Ask + spread — tersedia saat data dari OANDA (real-time bid/ask)
        bid = data.get("bid")
        ask = data.get("ask")
        spread_str = ""
        if bid is not None and ask is not None:
            spread = data.get("spread")
            if spread is None:
                spread = round(ask - bid, 8)
            spread_str = (
                f"\n💱 Bid {format_price(bid, symbol)} / Ask {format_price(ask, symbol)} "
                f"(spread {spread})"
            )

        # Rentang 5 sesi terakhir dari OHLCV (untuk konteks singkat)
        ohlcv = data.get("ohlcv", [])
        range_str = ""
        if ohlcv:
            highs = [d.get("high") for d in ohlcv if d.get("high") is not None]
            lows = [d.get("low") for d in ohlcv if d.get("low") is not None]
            if highs and lows:
                range_str = (
                    f"\n📊 Rentang 5 sesi: {format_price(min(lows), symbol)} – "
                    f"{format_price(max(highs), symbol)}"
                )

        # Level kunci instan dari OHLCV (pivot + fibonacci + bias EMA) — dihitung
        # lokal, tanpa AI & tanpa biaya: jawaban harga langsung punya konteks S/R.
        quick = ""
        try:
            ind = compute_indicators(ohlcv)
            if ind:
                quick_lines = []
                rsi = ind.get("rsi")
                if rsi is not None:
                    zone = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "netral"
                    quick_lines.append(f"⚡ RSI(14): {rsi:.1f} ({zone})")
                ema20 = ind.get("ema_20")
                ema50 = ind.get("ema_50")
                if ema20 and ema50:
                    bias = "Bullish" if ema20 > ema50 else "Bearish"
                    quick_lines.append(f"📈 Bias: {bias} (EMA20 {'>' if ema20 > ema50 else '<'} EMA50)")
                levels = format_key_levels(ind)
                if levels:
                    quick_lines.append(levels)
                if quick_lines:
                    quick = "\n\n🔑 *Quick Levels:*\n" + "\n".join(quick_lines)
        except Exception as e:
            logger.debug(f"Quick levels skipped for {symbol}: {e}")

        return (
            f"💱 *{display_name}* — Harga Terkini\n"
            f"{arrow} Harga: *{format_price(price, symbol)}* ({change_str})"
            f"{spread_str}{range_str}{quick}\n\n"
            f"⚡ Jawaban instan dari data pasar real-time.\n"
            f"💡 Kirim \"analisis {display_name.lower()}\" untuk analisis lengkap."
        )

    async def _build_quick_levels_text(self, symbol: str, asset_label: str) -> Optional[str]:
        """Level S/R & target dari data lokal (tanpa AI) untuk tombol aksi cepat."""
        try:
            ohlcv = await asyncio.to_thread(
                self.market.get_ohlcv_history, symbol, period="3mo", interval="1d", limit=60
            )
        except Exception as e:
            logger.warning(f"Quick levels fetch failed for {symbol}: {e}")
            return None
        if not ohlcv:
            return f"❌ Data *{asset_label}* tidak tersedia saat ini."

        ind = compute_indicators(ohlcv)
        lines = [f"🔑 *LEVEL KUNCI {asset_label.upper()}*"]
        price = ind.get("current_price")
        if price is not None:
            lines.append(f"💰 Harga: {format_price(price, symbol)}")
        rsi = ind.get("rsi")
        if rsi is not None:
            zone = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "netral"
            lines.append(f"⚡ RSI(14): {rsi:.1f} ({zone})")
        levels = format_key_levels(ind)
        if levels:
            lines.append(levels)
        else:
            lines.append("ℹ️ Data belum cukup untuk menghitung level kunci.")
        return (
            "\n".join(lines)
            + f"\n\n💡 Lanjut tanya: \"analisis teknikal {asset_label.lower()}\" untuk ulasan lengkap."
        )

    async def _build_scenario_followup(self, user_id: int, symbol: str, asset_label: str) -> Optional[str]:
        """Analisis skenario (bull/bear/base) memakai konteks percakapan user."""
        question = f"Berikan analisis skenario (Bullish/Bearish/Base dengan probabilitas total 100%) untuk {asset_label}."
        history_text = format_history(user_id)
        try:
            ohlcv = await asyncio.to_thread(
                self.market.get_ohlcv_history, symbol, period="3mo", interval="1d", limit=60
            )
        except Exception as e:
            logger.warning(f"Scenario fetch failed for {symbol}: {e}")
            ohlcv = None

        if self.analysis_director and ENABLE_MULTI_AGENT:
            try:
                result = await self.analysis_director.analyze(
                    question=question,
                    market_data_ohlcv=ohlcv or None,
                    conversation_history=history_text,
                )
                content = strip_markdown_asterisks(_strip_provider_prefix(result.final_response or ""))
                if len(content) > 50:
                    add_exchange(user_id, question, content)
                    return f"{content}{DISCLAIMER}"
            except Exception as e:
                logger.warning(f"Scenario follow-up (multi-agent) failed: {e}")

        # Fallback: single-prompt (tanpa multi-agent)
        indicators_str = format_indicators_for_prompt(compute_indicators(ohlcv)) if ohlcv else "Data tidak tersedia."
        prompt = (
            f"Berikan analisis skenario (Bullish/Bearish/Base dengan probabilitas total 100%) "
            f"untuk {asset_label}.\n\n"
            f"{history_text}\n\n"
            f"DATA INDIKATOR:\n{indicators_str}"
        )
        answer = await asyncio.to_thread(self.ai.generate, prompt, max_retries=3, use_cache=True)
        content = strip_markdown_asterisks(_strip_provider_prefix(answer))
        # FACT CHECK: skenario memakai indikator lokal sebagai data pembanding
        fact_note = build_fact_check_note(content, [indicators_str, question])
        if fact_note:
            content += fact_note
        add_exchange(user_id, question, strip_fact_check_note(content))
        return f"{content}{DISCLAIMER}"

    def _format_market_data(self, data: Dict) -> str:
        """Format data market untuk dimasukkan ke prompt."""
        parts = []
        price = data.get("current_price")
        change = data.get("change_pct")
        ohlcv = data.get("ohlcv", [])

        if price is not None:
            arrow = "🟢" if change and change > 0 else "🔴" if change and change < 0 else "⚪"
            change_str = f"{change:+.2f}%" if change is not None else "N/A"
            parts.append(f"{arrow} Harga: {format_price(price, data.get('symbol', ''))} ({change_str})")

        # Spread bid/ask — hanya tersedia dari OANDA (real-time bid/ask)
        bid = data.get("bid")
        ask = data.get("ask")
        if bid is not None and ask is not None:
            spread = data.get("spread")
            if spread is None:
                spread = round(ask - bid, 8)
            symbol_for_price = data.get("symbol", "")
            parts.append(
                f"💱 Bid/Ask: {format_price(bid, symbol_for_price)} / "
                f"{format_price(ask, symbol_for_price)} (spread {spread})"
            )

        if ohlcv:
            # Rentang 5 sesi SEBENARNYA (bukan bar terakhir) — dari window 5 bar
            window = ohlcv[-5:]
            highs = [d.get("high") for d in window if d.get("high") is not None]
            lows = [d.get("low") for d in window if d.get("low") is not None]
            if highs and lows:
                parts.append(f"📈 High 5d: {max(highs)}")
                parts.append(f"📉 Low 5d: {min(lows)}")

        if data.get("high_52w"):
            parts.append(f"🏆 High 52w: {data['high_52w']}")
        if data.get("low_52w"):
            parts.append(f"🔻 Low 52w: {data['low_52w']}")

        return "\n".join(parts)

    def _build_prompt(self, question: str, context: str, conversation_history: str = "") -> str:
        """
        Bangun prompt untuk AI dengan data konteks.
        (Legacy method, used when multi-agent is disabled)

        Konten prompt DIAMBIL dari file `prompts/*.txt` (single source of
        truth) — pemilihan template mengikuti INTENT pertanyaan:

          market_analysis      → analisis pasar/teknikal (default)
          macro_explanation    → pertanyaan data makro (cpi, nfp, fed, gdp, ...)
          technical_analysis   → pertanyaan korelasi antar instrumen

        Fallback ke template bawaan bila file .txt tidak tersedia.
        """
        current_time = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%Y-%m-%d %H:%M WIB")

        # Analisis intent pertanyaan untuk prompt yang lebih relevan
        q = question.lower()
        intent_instruction = ""
        template = "market_analysis"

        if any(kw in q for kw in ["teknikal", "support", "resistance", "rsi", "macd", "chart", "trend"]):
            intent_instruction = "Fokus pada analisis teknikal: level support/resistance, indikator, dan trend."
        elif any(kw in q for kw in ["nfp", "cpi", "inflasi", "gdp", "fed", "suku bunga", "tenaga kerja"]):
            intent_instruction = "Fokus pada data fundamental: dampak data makroekonomi ke pasar."
            template = "macro_explanation"
        elif any(kw in q for kw in ["berita", "news", "sentimen", "headline"]):
            intent_instruction = "Fokus pada berita terkini dan sentimen pasar yang relevan."
        elif any(kw in q for kw in ["korelasi", "hubungan", "dampak", "pengaruh"]):
            intent_instruction = "Fokus pada hubungan/korelasi antar instrumen yang ditanyakan."
            template = "technical_analysis"
        elif any(kw in q for kw in ["apa itu", "pengertian", "definisi", "bagaimana", "belajar"]):
            intent_instruction = "Fokus pada edukasi: jelaskan konsep dengan bahasa sederhana dan berikan contoh."
        elif any(kw in q for kw in ["bandingkan", "perbedaan", "vs", "versus"]):
            intent_instruction = "Fokus pada perbandingan: berikan perbedaan dan persamaan yang jelas."
        elif any(kw in q for kw in ["prediksi", "akan", "ramalan", "perkiraan", "kemana"]):
            intent_instruction = "Berikan analisis prospek dengan skenario yang mungkin terjadi. Jangan memberikan kepastian."
        elif any(kw in q for kw in ["risiko", "bahaya", "waspada", "volatilitas"]):
            intent_instruction = "Fokus pada identifikasi dan penjelasan risiko pasar."
        elif any(kw in q for kw in ["harga", "price", "berapa", "rate", "kurs", "naik", "turun"]):
            intent_instruction = "Fokus pada harga terkini, perubahan, dan konteks pergerakan."

        history_section = ""
        if conversation_history:
            history_section = (
                f"\n=== PERCAKAPAN SEBELUMNYA (gunakan jika pertanyaan follow-up) ===\n"
                f"{conversation_history}\n"
                f"=== AKHIR PERCAKAPAN ===\n"
            )

        # Deteksi instrumen yang dibahas (best-effort). Tidak memakai state
        # instance — aman dipanggil dari test tanpa __init__ penuh.
        instrument = "Pasar"
        try:
            pairs = self._detect_pairs(question)
            if pairs:
                instrument = pairs[0][0].upper()
            else:
                _sym, dname = ChartGenerator.get_chart_symbol_from_text(question)
                if dname:
                    instrument = dname
        except Exception as e:
            logger.debug(f"Instrument detection skipped: {e}")

        return format_prompt(
            template,
            QUESTION=question,
            USER_QUESTION=question,
            CONTEXT=context,
            CONVERSATION_HISTORY=history_section,
            CURRENT_TIME=current_time,
            INTENT_INSTRUCTION=intent_instruction,
            INSTRUMENT=instrument,
            INSTRUMENTS=instrument,
        )

    # ===================== CALLBACK QUERY HANDLER =====================

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk inline keyboard callbacks."""
        query = update.callback_query
        await query.answer()

        data = query.data

        if data == "morning":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            try:
                brief = await self._generate_morning_brief()
            except Exception as e:
                logger.exception(f"Morning brief (callback) gagal: {e}")
                brief = "⚠️ Morning brief tidak dapat dibuat saat ini. Coba lagi beberapa saat."
            await safe_edit_message_text(
                query,
                brief,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "prediksi":
            # Tombol menu '🎯 Prediksi News' — win rate prediksi XAU/USD
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            try:
                message = await self._build_prediksi_message(limit=10)
            except Exception as e:
                logger.warning(f"Prediksi (menu) gagal: {e}")
                message = (
                    "🎯 *PREDIKSI NEWS — XAU/USD*\n\n"
                    "❌ Statistik prediksi tidak tersedia saat ini. Coba lagi beberapa saat."
                )
            await safe_edit_message_text(
                query,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "overview":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            message, kb = await self._build_overview_reply()
            await safe_edit_message_text(
                query,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )

        elif data == "overview_refresh":
            # Muat ulang harga PAKSA (bypass cache 10 menit) lalu edit pesan
            try:
                message, kb = await self._build_overview_reply(refresh=True)
                await safe_edit_message_text(
                    query,
                    message,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=kb,
                )
            except Exception as e:
                logger.error(f"Overview refresh callback error: {e}")
                await safe_edit_message_text(
                    query,
                    "❌ Gagal memuat ulang overview pasar. Silakan coba lagi nanti.",
                )

        elif data == "gold_price":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            gold_data = await asyncio.to_thread(self.market.get_yahoo_data, "GC=F", period="1wk")
            formatted = self._format_market_data(gold_data)
            await safe_edit_message_text(
                query,
                f"🥇 *HARGA GOLD (XAU/USD)*\n\n{formatted}\n\n{await self.news.get_news_summary('GC=F')}\n{DISCLAIMER}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "eurusd":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            eur_data = await asyncio.to_thread(self.market.get_yahoo_data, "EURUSD=X", period="1wk")
            formatted = self._format_market_data(eur_data)
            await safe_edit_message_text(
                query,
                f"💱 *EUR/USD*\n\n{formatted}\n{DISCLAIMER}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "macro":
            macro_summary = await asyncio.to_thread(self.macro.get_macro_summary)
            await safe_edit_message_text(
                query,
                f"🏛️ *DATA MAKROEKONOMI*\n\n{macro_summary}\n{DISCLAIMER}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "calendar":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            try:
                # Kalender ekonomi BULAN INI + tombol analisis dampak per event
                message, kb = await self._build_calendar_reply()
                kwargs = {"parse_mode": "Markdown", "disable_web_page_preview": True}
                if kb:
                    kwargs["reply_markup"] = kb
                await safe_edit_message_text(query, message, **kwargs)
            except Exception as e:
                logger.error(f"Calendar callback error: {e}")
                await safe_edit_message_text(
                    query,
                    "❌ Gagal memuat kalender ekonomi. Silakan coba lagi nanti.",
                )

        elif data == "calendar_refresh":
            # Muat ulang kalender PAKSA (bypass cache 10 menit) lalu edit pesan
            try:
                message, kb = await self._build_calendar_reply(refresh=True)
                kwargs = {"parse_mode": "Markdown", "disable_web_page_preview": True}
                if kb:
                    kwargs["reply_markup"] = kb
                await safe_edit_message_text(query, message, **kwargs)
            except Exception as e:
                logger.error(f"Calendar refresh callback error: {e}")
                await safe_edit_message_text(
                    query,
                    "❌ Gagal memuat ulang kalender. Silakan coba lagi nanti.",
                )

        elif data.startswith("aft:"):
            await self._handle_calendar_aftermath_button(query, data)

        elif data == "sentiment":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            result = await self.sentiment.analyze("FOREX", use_llm=True)
            report = strip_markdown_asterisks(self.sentiment.format_report(result, "Pasar Forex"))
            await safe_edit_message_text(
                query,
                report,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "sentimen_retail":
            # Tombol menu: sentimen retail OANDA untuk EUR/USD (default)
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            try:
                message = await self._format_retail_sentiment_text("EUR_USD", "EUR/USD")
            except Exception as e:
                logger.warning(f"Retail sentiment (menu) gagal: {e}")
                message = (
                    "❌ Sentimen retail belum tersedia saat ini.\n\n"
                    "Pastikan `OANDA_API_KEY` terisi di dashboard deploy (token demo gratis: "
                    "https://www.oanda.com/demo-account/tpa/personal_token)."
                )
            await safe_edit_message_text(
                query,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "alert_on":
            # Tombol menu: aktifkan notifikasi event ekonomi (setara /alert on)
            chat_id = update.effective_chat.id
            subscribers = context.bot_data.setdefault("event_alert_subscribers", set())
            before = set(subscribers)
            subscribers.add(chat_id)
            context.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before:
                await self._persist_alert_subscribers(context)
            await query.message.reply_text(ALERT_ON_MESSAGE, parse_mode="Markdown")

        elif data == "subscribe":
            chat_id = update.effective_chat.id
            subscribed = await db.add_subscriber_async(chat_id)
            if subscribed:
                await query.message.reply_text(
                    "🎉 Berhasil berlangganan Morning Brief harian!\n"
                    "Setiap pagi kamu akan menerima ringkasan pasar otomatis. 🌅"
                )
            else:
                await query.message.reply_text(
                    "❌ Gagal berlangganan. Database mungkin belum dikonfigurasi "
                    "(SUPABASE_URL / SUPABASE_KEY)."
                )

        elif data == "unsubscribe":
            chat_id = update.effective_chat.id
            removed = await db.remove_subscriber_async(chat_id)
            if removed:
                await query.message.reply_text(
                    "👋 Berhasil berhenti berlangganan Morning Brief.\n"
                    "Kirim /start lalu klik tombol 🔔 untuk berlangganan lagi."
                )
            else:
                await query.message.reply_text(
                    "⚠️ Kamu belum terdaftar sebagai subscriber Morning Brief."
                )

        elif data == "settings":
            # Buka menu pengaturan (semua yang bisa diatur dalam satu tempat)
            message, kb = await self._build_settings_menu(update, context)
            await safe_edit_message_text(
                query,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )

        elif data == "settings_alert":
            # Toggle notifikasi event (sama dengan /alert on|off)
            chat_id = update.effective_chat.id
            subscribers = context.bot_data.setdefault("event_alert_subscribers", set())
            before = set(subscribers)
            if chat_id in subscribers:
                subscribers.discard(chat_id)
                feedback = "🔔 Alert Event *dimatikan*."
            else:
                subscribers.add(chat_id)
                feedback = "🔔 Alert Event *diaktifkan*."
            context.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before:
                await self._persist_alert_subscribers(context)
            message, kb = await self._build_settings_menu(update, context)
            await safe_edit_message_text(
                query,
                f"{feedback}\n\n{message}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )

        elif data == "settings_brief":
            # Toggle langganan morning brief (sama dengan /subscribe|/unsubscribe)
            chat_id = update.effective_chat.id
            try:
                subscribed = await db.is_subscribed_async(chat_id)
            except Exception as e:
                logger.debug(f"Cek status brief gagal: {e}")
                subscribed = False
            if subscribed:
                removed = await db.remove_subscriber_async(chat_id)
                feedback = "🌅 Langganan Morning Brief *dihentikan*." if removed else "❌ Gagal mengubah langganan."
            else:
                added = await db.add_subscriber_async(chat_id)
                feedback = "🌅 Langganan Morning Brief *diaktifkan*." if added else "❌ Gagal mengubah langganan."
            message, kb = await self._build_settings_menu(update, context)
            await safe_edit_message_text(
                query,
                f"{feedback}\n\n{message}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )

        elif data == "settings_clear":
            # Hapus konteks percakapan (sama dengan /clear)
            user_id = query.from_user.id
            clear(user_id)
            message, kb = await self._build_settings_menu(update, context)
            await safe_edit_message_text(
                query,
                f"🧹 *Konteks percakapan dibersihkan.*\n\n{message}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )

        elif data == "menu":
            # Kembali ke menu utama (re-render welcome + menu inline)
            await safe_edit_message_text(
                query,
                WELCOME_MESSAGE,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=_main_menu_inline_keyboard(),
            )

        elif data.startswith("qa:"):
            # ===== QUICK ACTIONS — memakai konteks multi-turn user =====
            # Callback_data: "qa:sr", "qa:scenario", "qa:clear", atau
            # "qa:<aksi>:<simbol>" (simbol ter-embed agar tombol lama tetap
            # bekerja untuk instrumen yang benar walau konteks sudah berubah).
            parts = data.split(":", 2)
            action = parts[1] if len(parts) > 1 else ""
            embedded_symbol = parts[2] if len(parts) > 2 else None
            user_id = query.from_user.id

            # Buang tombol segera (aksi sedang diproses)
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass

            if action == "clear":
                clear(user_id)
                await query.message.reply_text(
                    "🧹 *Konteks percakapan dibersihkan.* Mulai dari nol! 😊",
                    parse_mode="Markdown",
                )
                return

            # Simbol: dari tombol (jika ter-embed), else dari konteks percakapan
            if embedded_symbol:
                symbol = embedded_symbol
                display_label = ChartGenerator._get_display_name(symbol)
            else:
                display_label = get_context(user_id).get("asset_focus")
                if not display_label:
                    await query.message.reply_text(
                        "ℹ️ Belum ada konteks aset. Tanyakan dulu, misalnya: "
                        "\"analisis teknikal eurusd\" atau \"harga gold\".\n\n"
                        "Setelah itu tombol aksi cepat ini bisa dipakai. 👍",
                        parse_mode="Markdown",
                    )
                    return
                symbol = label_to_symbol(display_label)
                if not symbol:
                    await query.message.reply_text(
                        f"ℹ️ Instrumen *{display_label}* belum mendukung aksi cepat ini.",
                        parse_mode="Markdown",
                    )
                    return

            if action == "sr":
                text = await self._build_quick_levels_text(symbol, display_label)
            elif action == "scenario":
                try:
                    await context.bot.send_chat_action(
                        chat_id=update.effective_chat.id, action="typing"
                    )
                except Exception:
                    pass
                text = await self._build_scenario_followup(user_id, symbol, display_label)
            else:
                text = None

            if text:
                await query.message.reply_text(
                    text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await query.message.reply_text(
                    "❌ Gagal memuat data. Silakan coba lagi sebentar lagi.",
                    parse_mode="Markdown",
                )
            return

        elif data == "help":
            await safe_edit_message_text(
                query,
                HELP_MESSAGE,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

    # ===================== ECONOMIC EVENT ALERTS (Scheduled) =====================

    def _get_alert_subscribers(self, application: Application) -> set:
        """Dapatkan daftar chat_id yang subscribe notifikasi event."""
        return set(application.bot_data.get("event_alert_subscribers", set()))

    async def send_scheduled_event_digest(self, application: Application):
        """
        Kirim digest harian event ekonomi HIGH-IMPACT hari ini ke semua subscriber.
        Dipanggil scheduler setiap pagi (ECONOMIC_ALERT_DIGEST_HOUR).
        """
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            events = await self.macro.get_economic_calendar()
            tz_wib = ZoneInfo(MORNING_BRIEF_TIMEZONE)
            today = datetime.now(tz_wib).date()

            # Filter event HIGH impact yang rilis hari ini
            high_today = []
            for e in events:
                if e.get("impact") != "high":
                    continue
                dt = e.get("_dt_utc")
                if dt and dt.tzinfo is not None and dt.astimezone(tz_wib).date() == today:
                    high_today.append(e)

            if not high_today:
                message = (
                    f"📅 *EVENT EKONOMI HARI INI*\n📆 {today.strftime('%A, %d %B %Y')}\n\n"
                    f"✅ Tidak ada rilis data high-impact terjadwal hari ini."
                )
            else:
                lines = [
                    "📅 *EVENT EKONOMI HIGH-IMPACT HARI INI*",
                    f"📆 {today.strftime('%A, %d %B %Y')}\n",
                ]
                for e in sorted(high_today, key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc)):
                    lines.append(f"{e.get('impact_label', '🔥 HIGH')} {e.get('country_emoji', '')} *{e.get('event', '')}*")
                    lines.append(f"   🕐 {e.get('time', '')}")
                lines.append("")
                lines.append("⚡ Pengingat akan dikirim menjelang jam rilis.")
                message = "\n".join(lines)

            # Tombol '📊 Analisis Dampak' untuk event hari ini (ketuk → analisis)
            kb = self._build_calendar_aftermath_buttons(high_today)
            if kb:
                message += "\n\n📊 *Ketuk tombol event untuk analisis dampak.*"

            kwargs_send = {"parse_mode": "Markdown"}
            if kb:
                kwargs_send["reply_markup"] = kb
            for chat_id in list(subscribers):
                try:
                    await safe_send_message(
                        application.bot,
                        chat_id=chat_id,
                        text=message,
                        **kwargs_send,
                    )
                except Exception as e:
                    logger.error(f"Gagal kirim digest event ke {chat_id}: {e}")
                    if "Forbidden" in str(e):
                        subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
        except Exception as e:
            logger.error(f"Event digest error: {e}")

    async def check_event_reminders(self, application: Application):
        """
        Kirim reminder event HIGH-IMPACT yang akan rilis dalam X jam ke depan.
        Dipanggil job scheduler secara berkala. Dedup via event_alert_notified.
        """
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            events = await self.macro.get_economic_calendar()
            now_utc = datetime.now(timezone.utc)
            lead = timedelta(hours=ECONOMIC_ALERT_LEAD_HOURS)

            notified = set(application.bot_data.get("event_alert_notified", set()))
            before_notified = set(notified)

            # Prune key yang sudah lewat agar set tidak membengkak tanpa batas
            notified = {k for k in notified if k.split("|")[-1] >= (now_utc - timedelta(days=7)).isoformat()}

            new_keys = []
            for e in events:
                if e.get("impact") != "high":
                    continue
                dt = e.get("_dt_utc")
                if not dt or dt.tzinfo is None:  # skip jika tanpa waktu yang valid
                    continue
                if now_utc < dt <= now_utc + lead:
                    key = f"{e.get('event')}|{dt.isoformat()}"
                    if key in notified:
                        continue
                    notified.add(key)
                    new_keys.append(e)

            # Persist dedup dulu agar tidak terkirim dobel walau ada error saat kirim
            # (best-effort ke Supabase — hanya jika ada perubahan).
            application.bot_data["event_alert_notified"] = notified
            if notified != before_notified:
                try:
                    await db.save_event_alert_notified_async(notified)
                except Exception as e:
                    logger.debug(f"Persist event_alert_notified gagal: {e}")

            for e in new_keys:
                # Tombol '📊 Analisis Dampak' untuk event ini
                kb = self._build_calendar_aftermath_buttons([e], max_buttons=1)
                hint = "\n\n📊 *Ketuk tombol di bawah untuk analisis dampak.*" if kb else ""
                message = (
                    f"⏰ *REMINDER EVENT EKONOMI*\n\n"
                    f"{e.get('impact_label', '🔥 HIGH')} {e.get('country_emoji', '')} *{e.get('event', '')}*\n"
                    f"🕐 {e.get('time', '')}\n\n"
                    f"⚠️ Rilis dalam ±{ECONOMIC_ALERT_LEAD_HOURS} jam — bersiap untuk volatilitas!"
                    f"{hint}"
                )
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot,
                            chat_id=chat_id,
                            text=message,
                            parse_mode="Markdown",
                            reply_markup=kb,
                        )
                    except Exception as ex:
                        logger.error(f"Gagal kirim reminder event ke {chat_id}: {ex}")
                        if "Forbidden" in str(ex):
                            # User block bot / keluar — hapus dari daftar subscriber
                            subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
        except Exception as e:
            logger.error(f"Event reminder error: {e}")

    # ===================== EVENT AFTERMATH (POST-RELEASE ANALYSIS) =====================
    # Notifikasi otomatis SETELAH event high-impact rilis: angka Actual vs Forecast/
    # Previous + interpretasi statis + analisis AI dampaknya ke DXY. Dedup persisten
    # (bot_data + Supabase event_reports) agar tidak terkirim dobel, termasuk setelah
    # restart/deploy.

    EVENT_AFTERMATH_DEDUP_TTL_DAYS = 7

    @staticmethod
    def _aftermath_key(event: Dict) -> str:
        """Kunci dedup stabil per event (nama + waktu rilis UTC)."""
        dt = event.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc)
        return f"{event.get('event')}|{dt.isoformat()}"

    @staticmethod
    def _search_aftermath_events(events: List[Dict], query: Optional[str]) -> List[Dict]:
        """
        Cari event di kalender yang cocok dengan kata kunci (case-insensitive).
        Dipakai perintah /aftermath <event> — memilih kandidat terbaik.

        Peringkat (skor lebih tinggi = lebih dulu):
        1. +10 bila event sudah rilis (ada angka Actual) — analisis lebih bermakna
        2. +2 bila kata kunci cocok utuh di salah satu kata nama event
        3. +1 bila hanya substring biasa
        4. Tie-break: event paling baru lebih dulu

        Returns:
            List[Dict] — terurut peringkat (index 0 = kandidat terbaik).
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        ranked = []
        for e in events or []:
            name = (e.get("event") or "").lower()
            if not name or q not in name:
                continue
            words = name.split()
            if (
                name == q
                or name.startswith(q)
                or any(w == q or w.startswith(q) for w in words)
            ):
                score = 2
            else:
                score = 1
            if e.get("actual") not in (None, ""):
                score += 10
            dt = e.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc)
            ranked.append((score, dt.timestamp(), e))
        # Skor turun → yang paling baru lebih dulu pada skor sama
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [e for _, _, e in ranked]

    @staticmethod
    def _collect_aftermath_events(events: List[Dict], now_utc, lookback_hours: float) -> List[Dict]:
        """
        Pilih event HIGH-IMPACT yang sudah rilis dalam jendela lookback (jam).
        Murni & mudah di-test (tanpa I/O).

        Returns:
            List[Dict] — terurut dari paling baru.
        """
        cutoff = now_utc - timedelta(hours=lookback_hours)
        out = []
        for e in events or []:
            if e.get("impact") != "high":
                continue
            dt = e.get("_dt_utc")
            if not dt or dt.tzinfo is None:  # event tanpa waktu valid tidak bisa dinilai
                continue
            if cutoff <= dt <= now_utc:
                out.append(e)
        out.sort(
            key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return out

    @staticmethod
    def _fmt_ev_value(v) -> str:
        """
        Format nilai event (angka/string) atau '—' bila kosong.
        Float dibulatkan ramah: 250.0 → "250" (NFP), 3.0 → "3.0" (CPI),
        2.9 → "2.9" — agar angka makro tetap terbaca presisi.
        """
        if v is None or v == "":
            return "—"
        if isinstance(v, float):
            if v == int(v):
                return str(int(v)) if abs(v) >= 10 else f"{v:.1f}"
            return f"{v:g}"
        return str(v)

    @staticmethod
    def _format_event_numbers(event: Dict) -> str:
        """Actual/Forecast/Previous dalam satu baris ringkas dengan satuan."""
        unit = event.get("unit", "")
        return (
            f"📊 *Actual:* {MarketBot._fmt_ev_value(event.get('actual'))}{unit}  |  "
            f"*Forecast:* {MarketBot._fmt_ev_value(event.get('estimate'))}{unit}  |  "
            f"*Previous:* {MarketBot._fmt_ev_value(event.get('prev'))}{unit}"
        )

    @staticmethod
    def _static_event_interpretation(event: Dict) -> str:
        """
        Interpretasi arah DXY berbasis aturan — fallback saat AI tidak tersedia
        (dan dasar yang selalu ditampilkan). Membandingkan Actual vs Forecast
        untuk event utama AS; event non-AS dinilai via pasangan mata uang.
        """
        actual = event.get("actual")
        forecast = event.get("estimate")
        name = (event.get("event") or "").lower()
        country = (event.get("country") or "").upper()

        def _diff(a, f):
            try:
                return float(a) - float(f)
            except (TypeError, ValueError):
                return None

        # ---- Event non-AS: dampak lewat pasangan mata uang ----
        if country and country != "US":
            if actual in (None, "") or forecast in (None, ""):
                return (
                    f"Event di luar AS ({country}) — dampak ke DXY lewat pasangan mata uang. "
                    "Nilai aktual belum tersedia untuk perbandingan."
                )
            diff = _diff(actual, forecast)
            if diff is None:
                return f"Event di luar AS ({country}) — pantau pasangan mata uang terkait."
            if diff == 0:
                return f"Data {country} sesuai ekspektasi — dampak ke DXY cenderung terbatas."
            if diff > 0:
                return (
                    f"Data {country} DI ATAS ekspektasi → mata uang {country} berpotensi "
                    "menguat → DXY berpotensi TURUN."
                )
            return (
                f"Data {country} DI BAWAH ekspektasi → mata uang {country} berpotensi "
                "melemah → DXY berpotensi NAIK."
            )

        # ---- Event AS ----
        if actual in (None, "") or forecast in (None, ""):
            return "Nilai aktual belum tersedia — bandingkan dengan ekspektasi pasar bila sudah rilis."
        diff = _diff(actual, forecast)
        if diff is None:
            return "Angka aktual tidak bisa dibandingkan dengan ekspektasi."

        # FOMC / keputusan suku bunga: arah ditentukan oleh PERUBAHAN rate
        # (Actual vs Previous), bukan vs forecast — diproses lebih dulu.
        if "fed" in name or "fomc" in name or "rate decision" in name:
            prev = event.get("prev")
            try:
                pv = float(prev) if prev not in (None, "") else None
                if pv is not None:
                    av = float(actual)
                    if av > pv:
                        return (
                            f"Actual {MarketBot._fmt_ev_value(actual)} vs Previous "
                            f"{MarketBot._fmt_ev_value(prev)} — suku bunga NAIK (hawkish) → DXY cenderung NAIK."
                        )
                    if av < pv:
                        return (
                            f"Actual {MarketBot._fmt_ev_value(actual)} vs Previous "
                            f"{MarketBot._fmt_ev_value(prev)} — suku bunga TURUN (dovish) → DXY cenderung TURUN."
                        )
                    return (
                        f"Actual {MarketBot._fmt_ev_value(actual)} vs Previous "
                        f"{MarketBot._fmt_ev_value(prev)} — suku bunga dipertahankan — arah DXY tergantung nada statement & guidance."
                    )
                return "Keputusan Fed — arah DXY tergantung guidance/statement."
            except (TypeError, ValueError):
                return "Keputusan Fed — arah DXY tergantung guidance/statement."

        # Actual PERSIS sesuai ekspektasi — data sudah "harga-in" pasar, arah
        # dampak biasanya terbatas (penting: JANGAN dianggap lebih rendah/tinggi).
        if diff == 0:
            return f"Actual sesuai ekspektasi ({forecast}). Dampak ke DXY cenderung terbatas — fokus pada data sekunder (revisi, detail komponen)."

        if diff > 0:
            surprise = "DI ATAS ekspektasi"
        else:
            surprise = "DI BAWAH ekspektasi"

        if "cpi" in name or "inflasi" in name or "ppi" in name or "harga produsen" in name:
            hint = (
                "Data inflasi lebih tinggi dari ekspektasi → Fed cenderung hawkish → DXY cenderung NAIK."
                if diff > 0 else
                "Data inflasi lebih rendah dari ekspektasi → Fed cenderung dovish → DXY cenderung TURUN."
            )
        elif "non-farm" in name or "payroll" in name or "gdp" in name or "retail" in name:
            hint = (
                "Data ekonomi lebih kuat dari ekspektasi → dolar menguat → DXY cenderung NAIK."
                if diff > 0 else
                "Data ekonomi lebih lemah dari ekspektasi → dolar melemah → DXY cenderung TURUN."
            )
        elif "unemployment" in name or "pengangguran" in name or "claims" in name:
            # Inverse: pengangguran/klaim lebih RENDAH = pasar tenaga kerja kuat
            hint = (
                "Angka pengangguran/klaim lebih rendah dari ekspektasi → pasar tenaga kerja kuat → DXY cenderung NAIK."
                if diff < 0 else
                "Angka pengangguran/klaim lebih tinggi dari ekspektasi → pasar tenaga kerja melemah → DXY cenderung TURUN."
            )
        else:
            hint = (
                "Data di atas ekspektasi cenderung menguatkan USD → DXY berpotensi NAIK."
                if diff > 0 else
                "Data di bawah ekspektasi cenderung melemahkan USD → DXY berpotensi TURUN."
            )

        return f"Actual {surprise} vs Forecast ({forecast}). {hint}"

    async def _build_aftermath_message(self, event: Dict, market_line: str, manual: bool = False) -> str:
        """Bangun pesan analisis aftermath: angka + interpretasi statis + analisis AI.

        Args:
            manual: True saat dipanggil perintah /aftermath (judul berbeda).
        """
        event_name = event.get("event", "Event Ekonomi")
        title = "🎯 *ANALISIS DAMPAK EVENT*" if manual else "🔥 *AFTERMATH EVENT EKONOMI*"
        header = (
            f"{title}\n"
            f"{event.get('country_emoji', '')} *{event_name}*\n"
            f"🕐 {event.get('time', '')}\n\n"
            f"{self._format_event_numbers(event)}\n"
            f"💱 *Kondisi Pasar:* {market_line}\n"
        )

        static = self._static_event_interpretation(event)

        ai_section = ""
        try:
            prompt = format_prompt(
                "event_aftermath",
                EVENT_NAME=event_name,
                COUNTRY=event.get("country", "US"),
                TIME=event.get("time", ""),
                IMPACT_LABEL=event.get("impact_label", "🔥 HIGH"),
                ACTUAL=self._fmt_ev_value(event.get("actual")),
                FORECAST=self._fmt_ev_value(event.get("estimate")),
                PREV=self._fmt_ev_value(event.get("prev")),
                UNIT=event.get("unit", ""),
                DXY_DATA=market_line,
            )
            ai_text = await asyncio.to_thread(
                self.ai.generate, prompt, max_tokens=700, use_cache=True
            )
            if ai_text and "error" not in ai_text.lower():
                ai_text = strip_markdown_asterisks(_strip_provider_prefix(ai_text))
                ai_section = f"\n📰 *Analisis:*\n{ai_text}\n"
        except Exception as e:
            logger.warning(f"Aftermath AI analysis failed: {e}")

        if not ai_section:
            ai_section = f"\n📰 *Interpretasi:*\n{static}\n"

        # Section prediksi bot (XAU/USD) — tampil bila event ini punya prediksi
        # tercatat (fitur /prediksi): arah prediksi + hasil benar/salah + pergerakan.
        pred_section = ""
        try:
            if getattr(self, "news_preds", None) is not None:
                await asyncio.to_thread(self.news_preds.ensure_loaded)
                record = self.news_preds.get_prediction(self._aftermath_key(event))
                if record:
                    pred_section = self._format_prediction_section(record)
        except Exception as e:
            logger.debug(f"Section prediksi aftermath gagal: {e}")

        return header + ai_section + pred_section + f"\n{DISCLAIMER}"

    async def _build_market_line(self) -> str:
        """
        Konteks pasar satu baris: DXY + Gold + EUR/USD untuk analisis aftermath.
        Data di-cache data layer (TTL pendek), jadi biaya request kecil.
        Tidak pernah raise — selalu mengembalikan string minimal.
        """
        market_line = "DXY: tidak tersedia"
        try:
            dxy = await asyncio.to_thread(
                self.market.get_yahoo_data, "DX-Y.NYB", period="2d", interval="1h", ohlcv_limit=1
            )
            if "error" not in dxy and dxy.get("current_price") is not None:
                p = dxy["current_price"]
                c = dxy.get("change_pct")
                arrow = "🟢" if c and c > 0 else "🔴" if c and c < 0 else "⚪"
                c_str = f"{c:+.2f}%" if c is not None else ""
                market_line = f"DXY: {format_price(p, 'DX-Y.NYB')} {arrow} {c_str}"
        except Exception as e:
            logger.warning(f"DXY fetch gagal: {e}")
        for sym, label in (("GC=F", "Gold"), ("EURUSD=X", "EUR/USD")):
            try:
                data = await asyncio.to_thread(
                    self.market.get_yahoo_data, sym, period="2d", interval="1h", ohlcv_limit=1
                )
                if "error" not in data and data.get("current_price") is not None:
                    market_line += f"  |  {label}: {format_price(data['current_price'], sym)}"
            except Exception:
                pass
        return market_line

    async def check_event_aftermath(self, application: Application):
        """
        Job berkala: kirim analisis dampak event high-impact yang BARU SAJA rilis.
        Dipanggil scheduler (main.py) setiap ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES.
        Dedup via bot_data + Supabase (event_reports) agar tidak dobel, termasuk
        setelah restart/deploy.
        """
        if not EVENT_AFTERMATH_ENABLED:
            return
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            events = await self.macro.get_economic_calendar()
            now_utc = datetime.now(timezone.utc)

            candidates = self._collect_aftermath_events(events, now_utc, EVENT_AFTERMATH_LOOKBACK_HOURS)
            # Hanya laporkan event yang ANGKA ACTUAL-nya sudah tersedia — notifikasi
            # tanpa actual hanya placeholder & nilainya rendah. Event tanpa actual
            # akan dicek lagi di run berikutnya (dedup hanya menandai yang terkirim).
            candidates = [
                e for e in candidates if e.get("actual") not in (None, "")
            ]
            if not candidates:
                return

            # Dedup: gabung set memori + persisten (Supabase), lalu prune 7 hari
            reported = set(application.bot_data.get("event_aftermath_reported", set()))
            try:
                reported |= await db.get_reported_events_async()
            except Exception as e:
                logger.debug(f"event_reports load gagal: {e}")
            cutoff_ts = (now_utc - timedelta(days=self.EVENT_AFTERMATH_DEDUP_TTL_DAYS)).isoformat()
            reported = {k for k in reported if k.split("|")[-1] >= cutoff_ts}

            new_events = []
            for e in candidates:
                key = self._aftermath_key(e)
                if key in reported:
                    continue
                reported.add(key)
                new_events.append(e)
                # Batasi analisis per run: tiap event memanggil AI (budget sampai
                # AI_MAX_TOTAL_WAIT_SECONDS). Sisanya menunggu run berikutnya.
                if len(new_events) >= 3:
                    break
            application.bot_data["event_aftermath_reported"] = reported

            if not new_events:
                return

            # Konteks pasar SEKALI per run (DXY + Gold + EUR/USD)
            market_line = await self._build_market_line()

            for e in new_events:
                try:
                    message = await self._build_aftermath_message(e, market_line)
                except Exception as ex:
                    logger.warning(f"Aftermath message gagal untuk {e.get('event')}: {ex}")
                    message = (
                        f"🔥 *AFTERMATH EVENT EKONOMI*\n{e.get('country_emoji', '')} "
                        f"*{e.get('event', '')}*\n🕐 {e.get('time', '')}\n\n"
                        f"{self._format_event_numbers(e)}\n\n"
                        f"💱 Kondisi Pasar: {market_line}\n\n"
                        f"📰 {self._static_event_interpretation(e)}\n\n{DISCLAIMER}"
                    )
                # Tombol '📊 Analisis Dampak' — ketuk untuk lihat detail/ulangi analisis
                kb = self._build_calendar_aftermath_buttons([e], max_buttons=1)
                kwargs_send = {"parse_mode": "Markdown"}
                if kb:
                    kwargs_send["reply_markup"] = kb
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot, chat_id=chat_id, text=message, **kwargs_send
                        )
                    except Exception as ex:
                        logger.error(f"Gagal kirim aftermath ke {chat_id}: {ex}")
                        if "Forbidden" in str(ex):
                            subscribers.discard(chat_id)
                # Persist dedup (best-effort — kegagalan tidak menggagalkan kirim)
                try:
                    await db.save_reported_event_async(self._aftermath_key(e))
                except Exception as ex:
                    logger.debug(f"save_reported_event gagal: {ex}")

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
        except Exception as e:
            logger.error(f"Event aftermath error: {e}")

    # ===================== NEWS PREDICTION (XAU/USD) =====================
    # Prediksi arah emas (naik/turun) sebelum event ekonomi high-impact rilis
    # (dikirim NEWS_PREDICTION_LEAD_MINUTES menit sebelum jadwal), lalu setelah
    # rilis + NEWS_PREDICTION_SETTLE_MINUTES menit AI menilai benar/salah/flat.
    # Dedup via NewsPredictionStore (event_key unik). Dikirim ke subscriber /alert.
    # Riwayat & win rate dilihat via /prediksi.

    GOLD_PREDICTION_SYMBOL = "GC=F"

    PREDICTION_USAGE = (
        "🎯 *PREDIKSI NEWS — XAU/USD (GOLD)*\n\n"
        "Bot memprediksi arah emas (*naik*/turun) 5 menit sebelum event ekonomi "
        "high-impact rilis (NFP, CPI, FOMC, GDP, dll), lalu AI menilai benar/salah "
        "setelah rilis. Notifikasi dikirim ke subscriber `/alert`.\n\n"
        "`/prediksi` — statistik win rate + 10 prediksi terakhir\n"
        "`/prediksi history` — riwayat 25 prediksi terakhir\n"
        "`/prediksi help` — bantuan ini"
    )

    @staticmethod
    def _parse_ai_direction(text: Optional[str]) -> Optional[str]:
        """Ambil kata pertama 'naik'/'turun' dari output AI (awali satu kata)."""
        if not text:
            return None
        first = text.strip().split()[0].strip(".!?:;\"'()[]-–—*_#").lower()
        return first if first in ("naik", "turun") else None

    @staticmethod
    def _parse_ai_verdict(text: Optional[str]) -> Optional[str]:
        """Ambil kata pertama 'benar'/'salah'/'flat' dari output AI."""
        if not text:
            return None
        first = text.strip().split()[0].strip(".!?:;\"'()[]-–—*_#").lower()
        return first if first in ("benar", "salah", "flat") else None

    @staticmethod
    def _rule_based_gold_direction(event: Dict) -> Tuple[str, str]:
        """
        Fallback arah emas berbasis aturan (saat AI tidak tersedia/gagal).
        Mengembalikan (direction, alasan). Arah DXY diestimasi dari Forecast vs
        Previous ala _static_event_interpretation; emas umumnya berkorelasi
        terbalik dengan DXY dalam reaksi jangka pendek.
        """
        name = (event.get("event") or "").lower()
        forecast = event.get("estimate")
        prev = event.get("prev")
        try:
            fv = float(forecast) if forecast not in (None, "") else None
            pv = float(prev) if prev not in (None, "") else None
        except (TypeError, ValueError):
            fv = pv = None

        # Keputusan suku bunga: arah tidak bisa diestimasi dari angka
        if "fed" in name or "fomc" in name or "rate decision" in name:
            return (
                "naik",
                "Keputusan suku bunga Fed menentukan arah lewat nada statement & "
                "guidance — di tengah ketidakpastian emas cenderung didukung "
                "permintaan safe-haven.",
            )
        if fv is None or pv is None:
            return (
                "naik",
                "Ekspektasi pasar belum tersedia untuk perbandingan — perkiraan "
                "default: emas didukung status safe-haven.",
            )

        diff = fv - pv
        if "cpi" in name or "inflasi" in name or "ppi" in name:
            if diff > 0:
                return (
                    "turun",
                    f"Forecast inflasi {fv} di atas previous {pv} → ekspektasi inflasi "
                    "lebih tinggi → yield & USD berpotensi naik → emas cenderung turun.",
                )
            return (
                "naik",
                f"Forecast inflasi {fv} di bawah previous {pv} → tekanan inflasi mereda "
                "→ ekspektasi dovish → emas cenderung naik.",
            )
        if (
            "non-farm" in name
            or "payroll" in name
            or "gdp" in name
            or "retail" in name
            or "durable" in name
            or "manufaktur" in name
        ):
            if diff > 0:
                return (
                    "turun",
                    f"Forecast {fv} di atas previous {pv} → ekonomi diprediksi lebih "
                    "kuat → USD menguat → emas cenderung turun.",
                )
            return (
                "naik",
                f"Forecast {fv} di bawah previous {pv} → ekonomi diprediksi melambat → "
                "USD melemah → emas cenderung naik.",
            )
        if "unemployment" in name or "pengangguran" in name or "claims" in name:
            if diff < 0:
                return (
                    "turun",
                    f"Forecast pengangguran/klaim {fv} di bawah previous {pv} → pasar "
                    "tenaga kerja kuat → USD menguat → emas cenderung turun.",
                )
            return (
                "naik",
                f"Forecast pengangguran/klaim {fv} di atas previous {pv} → pasar tenaga "
                "kerja melemah → USD melemah → emas cenderung naik.",
            )
        if diff > 0:
            return (
                "turun",
                f"Forecast {fv} di atas previous {pv} → ekspektasi USD lebih kuat → emas "
                "cenderung turun.",
            )
        return (
            "naik",
            f"Forecast {fv} di bawah previous {pv} → ekspektasi USD lebih lemah → emas "
            "cenderung naik.",
        )

    @staticmethod
    def _compute_rule_result(
        predicted: str,
        pred_price: Optional[float],
        now_price: Optional[float],
        min_move_pct: float = 0.05,
    ) -> Optional[Dict]:
        """
        Hasil evaluasi berbasis aturan (dasar sebelum AI menilai).
        Returns {"result", "actual_direction", "move_pct"} atau None bila harga
        tidak tersedia (prediksi tetap pending — coba lagi run berikutnya).
        """
        if pred_price is None or now_price is None or pred_price <= 0:
            return None
        move_pct = (now_price - pred_price) / pred_price * 100.0
        if abs(move_pct) < min_move_pct:
            return {"result": "flat", "actual_direction": "flat", "move_pct": move_pct}
        if move_pct > 0:
            return {
                "result": "benar" if predicted == "naik" else "salah",
                "actual_direction": "naik",
                "move_pct": move_pct,
            }
        return {
            "result": "benar" if predicted == "turun" else "salah",
            "actual_direction": "turun",
            "move_pct": move_pct,
        }

    async def _fetch_gold_price(self) -> Optional[float]:
        """Harga emas (XAU/USD) saat ini — tidak pernah raise."""
        try:
            data = await asyncio.to_thread(
                self.market.get_yahoo_data,
                self.GOLD_PREDICTION_SYMBOL,
                period="2d",
                interval="1h",
                ohlcv_limit=1,
            )
            if "error" not in data and data.get("current_price") is not None:
                return float(data["current_price"])
        except Exception as e:
            logger.warning(f"Gold price fetch gagal: {e}")
        return None

    async def _create_news_prediction(
        self, event_key: str, event: Dict, market_line: str, gold_price: Optional[float]
    ) -> Optional[dict]:
        """Buat prediksi (AI dulu, fallback aturan) & simpan ke store."""
        direction, reasoning = self._rule_based_gold_direction(event)
        try:
            prompt = format_prompt(
                "news_prediction",
                EVENT_NAME=event.get("event", "Event Ekonomi"),
                COUNTRY=event.get("country", "US"),
                TIME=event.get("time", ""),
                FORECAST=self._fmt_ev_value(event.get("estimate")),
                PREV=self._fmt_ev_value(event.get("prev")),
                UNIT=event.get("unit", ""),
                MARKET_LINE=market_line,
                GOLD_PRICE=format_price(gold_price, "GC=F") if gold_price else "tidak tersedia",
            )
            ai_text = await asyncio.to_thread(
                self.ai.generate, prompt, max_tokens=300, use_cache=True
            )
            parsed = self._parse_ai_direction(ai_text)
            if parsed:
                direction = parsed
                lines = (ai_text or "").strip().splitlines()
                extra = "\n".join(lines[1:]).strip()
                if extra:
                    reasoning = extra
        except Exception as e:
            logger.warning(f"AI prediksi gagal untuk {event.get('event')}: {e}")

        return self.news_preds.add_prediction(
            event_key=event_key,
            event_name=event.get("event", "Event Ekonomi"),
            event_time=event.get("time", ""),
            event_dt_utc=event.get("_dt_utc"),
            country=event.get("country", ""),
            country_emoji=event.get("country_emoji", ""),
            direction=direction,
            price_at_prediction=gold_price,
            reasoning=reasoning,
            market_line=market_line,
            actual=event.get("actual"),
            forecast=event.get("estimate"),
            prev=event.get("prev"),
            unit=event.get("unit", ""),
        )

    @staticmethod
    def _format_prediction_section(record: dict) -> str:
        """Section 🎯 Prediksi Bot untuk pesan aftermath (dari record store)."""
        arrow = "📈 naik" if record.get("direction") == "naik" else "📉 turun"
        if record.get("status") == "settled" and record.get("result"):
            res = record.get("result")
            icon = {"benar": "✅", "salah": "❌", "flat": "➖"}.get(res, "➖")
            move = record.get("move_pct")
            move_str = f"{move:+.2f}%" if move is not None else "—"
            return f"\n🎯 *Prediksi Bot:* {arrow} → {icon} {res} (pergerakan {move_str})\n"
        return f"\n🎯 *Prediksi Bot:* {arrow} — ⏳ hasil belum dievaluasi\n"

    def _format_prediction_message(self, record: dict) -> str:
        arrow = "📈 naik" if record.get("direction") == "naik" else "📉 turun"
        price = record.get("price_at_prediction")
        price_str = format_price(price, "GC=F") if price else "—"
        lines = [
            "🎯 *PREDIKSI NEWS — XAU/USD*\n",
            f"{record.get('country_emoji', '')} *{record.get('event_name', 'Event Ekonomi')}*\n",
            f"🕐 {record.get('event_time', '')}\n",
            f"Prediksi emas: *{arrow}*\n",
            f"💰 Harga saat ini: {price_str}\n",
        ]
        if record.get("reasoning"):
            lines.append(f"💡 *Alasan:* {record.get('reasoning')}\n")
        lines.append(
            f"⏳ Hasil dievaluasi ±{NEWS_PREDICTION_SETTLE_MINUTES} menit setelah rilis.\n"
        )
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    def _format_verdict_message(self, record: dict) -> str:
        result = record.get("result")
        if result == "benar":
            head = "✅ *PREDIKSI BENAR*"
        elif result == "salah":
            head = "❌ *PREDIKSI SALAH*"
        else:
            head = "➖ *PREDIKSI FLAT*"
        arrow = "📈 naik" if record.get("direction") == "naik" else "📉 turun"
        price_pred = record.get("price_at_prediction")
        price_now = record.get("price_after")
        move = record.get("move_pct")
        move_str = f"{move:+.2f}%" if move is not None else "—"
        p_pred = format_price(price_pred, "GC=F") if price_pred else "—"
        p_now = format_price(price_now, "GC=F") if price_now else "—"
        lines = [
            f"{head}\n",
            f"{record.get('country_emoji', '')} *{record.get('event_name', 'Event Ekonomi')}*\n",
            f"🕐 {record.get('event_time', '')}\n",
            f"Prediksi: *{arrow}*\n",
            f"💰 Harga: {p_pred} → sekarang {p_now} ({move_str})\n",
        ]
        if record.get("result_reasoning"):
            lines.append(f"📊 *Evaluasi:* {record.get('result_reasoning')}\n")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    async def check_news_predictions(self, application: Application):
        """
        Job berkala: buat & kirim prediksi arah emas untuk event high-impact yang
        akan rilis dalam NEWS_PREDICTION_LEAD_MINUTES menit. Dedup via store.
        Dikirim ke subscriber /alert.
        """
        if not NEWS_PREDICTION_ENABLED:
            return
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            await asyncio.to_thread(self.news_preds.ensure_loaded)
            events = await self.macro.get_economic_calendar()
        except Exception as e:
            logger.error(f"News prediction calendar error: {e}")
            return

        now_utc = datetime.now(timezone.utc)
        lead = timedelta(minutes=NEWS_PREDICTION_LEAD_MINUTES)
        candidates = []
        for e in events:
            if e.get("impact") != "high":
                continue
            dt = e.get("_dt_utc")
            if not dt or dt.tzinfo is None:
                continue
            if now_utc < dt <= now_utc + lead:
                key = self._aftermath_key(e)
                if self.news_preds.get_prediction(key):
                    continue
                candidates.append((key, e))
            if len(candidates) >= NEWS_PREDICTION_MAX_PER_RUN:
                break

        if not candidates:
            return

        # Konteks pasar SEKALI per run (DXY + Gold + EUR/USD) + harga emas
        market_line = await self._build_market_line()
        gold_price = await self._fetch_gold_price()

        for key, e in candidates:
            if gold_price is None:
                # Tanpa harga acuan, prediksi tidak bisa dievaluasi nanti —
                # lewati & coba lagi di run berikutnya (event masih dalam jendela).
                logger.warning(f"Lewati prediksi {e.get('event')}: harga emas tidak tersedia.")
                continue
            try:
                record = await self._create_news_prediction(key, e, market_line, gold_price)
            except Exception as ex:
                logger.warning(f"Buat prediksi gagal untuk {e.get('event')}: {ex}")
                continue
            if not record:
                continue
            # Persist dulu (best-effort) agar restart tidak membuat prediksi dobel
            try:
                await db.save_news_prediction_async(record)
            except Exception as ex:
                logger.debug(f"save_news_prediction gagal: {ex}")
            message = self._format_prediction_message(record)
            for chat_id in list(subscribers):
                try:
                    await safe_send_message(
                        application.bot, chat_id=chat_id, text=message, parse_mode="Markdown"
                    )
                except Exception as ex:
                    logger.error(f"Gagal kirim prediksi ke {chat_id}: {ex}")
                    if "Forbidden" in str(ex):
                        subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)

    async def settle_news_predictions(self, application: Application):
        """
        Job berkala: evaluasi prediksi yang event-nya sudah lewat
        NEWS_PREDICTION_SETTLE_MINUTES menit. AI menilai benar/salah/flat dengan
        konteks: pergerakan harga + Actual vs Forecast + berita. Kirim hasil ke
        subscriber /alert & simpan ke store + Supabase.
        """
        if not NEWS_PREDICTION_ENABLED:
            return
        # Settlement adalah operasi data (win rate /prediksi) — tetap berjalan
        # walau tidak ada subscriber; hanya pengiriman pesan yang di-gate subscriber.
        subscribers = self._get_alert_subscribers(application)
        before_subscribers = set(subscribers)

        try:
            await asyncio.to_thread(self.news_preds.ensure_loaded)
        except Exception as e:
            logger.warning(f"News prediction load gagal: {e}")
            return

        pending = self.news_preds.get_pending(settle_minutes=NEWS_PREDICTION_SETTLE_MINUTES)
        if not pending:
            return

        gold_price = await self._fetch_gold_price()
        market_line = await self._build_market_line()

        settled_in_run = 0
        for record in pending:
            if settled_in_run >= NEWS_PREDICTION_MAX_PER_RUN:
                break  # budget AI per run — sisanya di run berikutnya
            try:
                updated = await self._evaluate_news_prediction(
                    record, gold_price, market_line
                )
            except Exception as ex:
                logger.warning(
                    f"Evaluasi prediksi gagal {record.get('event_name')}: {ex}"
                )
                continue
            if not updated or updated.get("status") != "settled":
                continue  # harga belum tersedia — coba lagi run berikutnya
            settled_in_run += 1
            # Persist hasil dulu (best-effort) agar restart tidak menilai ulang
            try:
                await db.save_news_prediction_async(updated)
            except Exception as ex:
                logger.debug(f"save_news_prediction (settle) gagal: {ex}")
            if not subscribers:
                continue
            message = self._format_verdict_message(updated)
            for chat_id in list(subscribers):
                try:
                    await safe_send_message(
                        application.bot, chat_id=chat_id, text=message, parse_mode="Markdown"
                    )
                except Exception as ex:
                    logger.error(f"Gagal kirim verdict ke {chat_id}: {ex}")
                    if "Forbidden" in str(ex):
                        subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)

    async def _evaluate_news_prediction(
        self, record: dict, gold_price: Optional[float], market_line: str
    ) -> Optional[dict]:
        """
        Evaluasi satu prediksi: aturan dulu (harga), lalu AI menilai dengan
        konteks lengkap. Mengembalikan record terselesaikan, atau None bila
        harga tidak tersedia (tetap pending).
        """
        rule = self._compute_rule_result(
            record.get("direction") or "naik",
            record.get("price_at_prediction"),
            gold_price,
            NEWS_PREDICTION_MIN_MOVE_PCT,
        )
        if rule is None:
            return None

        result = rule["result"]
        actual_direction = rule["actual_direction"]
        move_pct = rule["move_pct"]
        reasoning = (
            f"Harga emas bergerak {move_pct:+.2f}% dari "
            f"{format_price(record.get('price_at_prediction'), 'GC=F')} ke "
            f"{format_price(gold_price, 'GC=F')}. Prediksi: {record.get('direction')}."
        )

        try:
            unit = record.get("unit", "")
            numbers = (
                f"Actual: {self._fmt_ev_value(record.get('actual'))}{unit} | "
                f"Forecast: {self._fmt_ev_value(record.get('forecast'))}{unit} | "
                f"Previous: {self._fmt_ev_value(record.get('prev'))}{unit}"
            )
            news_summary = ""
            try:
                news_summary = await self.news.get_news_summary("GC=F")
            except Exception as ex:
                logger.debug(f"News summary gagal: {ex}")
            prompt = format_prompt(
                "news_prediction_verdict",
                DIRECTION=record.get("direction", "naik"),
                DIRECTION_LABEL=record.get("direction", "naik"),
                REASONING=record.get("reasoning", ""),
                PRICE_AT_PREDICTION=format_price(record.get("price_at_prediction"), "GC=F"),
                MARKET_LINE_AT_PREDICTION=record.get("market_line", "tidak tersedia"),
                PRICE_NOW=format_price(gold_price, "GC=F"),
                MOVE_PCT=f"{move_pct:+.2f}%",
                MOVE_ABS=f"{abs(move_pct):.2f}%",
                ACTUAL_VS_FORECAST=numbers,
                NEWS=(news_summary or "")[:600],
                MIN_MOVE_PCT=f"{NEWS_PREDICTION_MIN_MOVE_PCT}%",
            )
            ai_text = await asyncio.to_thread(
                self.ai.generate, prompt, max_tokens=300, use_cache=True
            )
            parsed = self._parse_ai_verdict(ai_text)
            if parsed:
                result = parsed
                lines = (ai_text or "").strip().splitlines()
                extra = "\n".join(lines[1:]).strip()
                if extra:
                    reasoning = f"{reasoning}\n{extra}"
        except Exception as e:
            logger.warning(f"AI verdict gagal: {e}")

        return self.news_preds.settle(
            event_key=record["event_key"],
            result=result,
            actual_direction=actual_direction,
            price_after=gold_price,
            move_pct=move_pct,
            reasoning=reasoning,
        )

    async def _build_prediksi_message(self, limit: int = 10) -> str:
        """
        Bangun pesan win rate & riwayat prediksi news (XAU/USD).

        Dipakai /prediksi command DAN tombol menu '🎯 Prediksi News' agar
        keduanya konsisten (satu sumber logika).
        """
        try:
            await asyncio.to_thread(self.news_preds.ensure_loaded)
        except Exception as e:
            logger.warning(f"News predictions load gagal: {e}")

        stats = self.news_preds.get_stats()
        recent = self.news_preds.get_recent(limit)

        if stats["total"] == 0:
            return (
                "🎯 *PREDIKSI NEWS — XAU/USD*\n\n"
                "Belum ada prediksi tercatat. Prediksi otomatis dibuat 5 menit "
                "sebelum event ekonomi high-impact rilis dan dikirim ke subscriber "
                "`/alert`.\n\nAktifkan notifikasi: `/alert`\nBantuan: `/prediksi help`"
            )

        wr = stats["win_rate"]
        wr_str = f"{wr:.1f}%" if wr is not None else "— (belum ada hasil)"
        lines = [
            "🎯 *WIN RATE PREDIKSI NEWS — XAU/USD*\n",
            f"📊 Total prediksi: *{stats['total']}*",
            f"✅ Benar: *{stats['benar']}*",
            f"❌ Salah: *{stats['salah']}*",
            f"➖ Flat: *{stats['flat']}*",
            f"🏆 Win rate: *{wr_str}*\n",
        ]
        if recent:
            lines.append(f"*{len(recent)} Prediksi Terakhir:*")
            icons = {"benar": "✅", "salah": "❌", "flat": "➖", "pending": "⏳"}
            for i, r in enumerate(recent, 1):
                arrow = "📈 naik" if r.get("direction") == "naik" else "📉 turun"
                res = r.get("result") or "pending"
                name = (r.get("event_name") or "")[:38]
                lines.append(
                    f"{i}. {r.get('country_emoji', '')} *{name}* — {arrow} {icons.get(res, '⏳')} {res}"
                )
            lines.append("\n`/prediksi history` — riwayat lebih panjang")

        return "\n".join(lines)

    async def prediksi_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /prediksi — win rate & riwayat prediksi news (XAU/USD)."""
        text = update.message.text or ""
        arg = text.replace("/prediksi", "").strip().lower()

        if arg in ("help", "bantuan"):
            await safe_reply_text(update.message, self.PREDICTION_USAGE, parse_mode="Markdown")
            return

        limit = 25 if arg in ("history", "riwayat") else 10
        message = await self._build_prediksi_message(limit)
        await safe_reply_text(update.message, message, parse_mode="Markdown")

    # ===================== /AFTERMATH (MANUAL EVENT ANALYSIS) =====================

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

    async def aftermath_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler /aftermath <event> — analisis dampak event ekonomi secara manual.

        Mencari event di kalender (14 hari terakhir → besok), memilih kandidat
        terbaik (yang sudah rilis diutamakan, paling baru), lalu memakai mesin
        aftermath yang sama dengan notifikasi otomatis (angka + interpretasi + AI).
        """
        text = update.message.text or ""
        arg = text.replace("/aftermath", "").strip().lower()

        if not arg or arg in ("help", "bantuan"):
            await safe_reply_text(update.message, self.AFTERMATH_USAGE, parse_mode="Markdown")
            return

        # Rate limit HANYA untuk analisis (bukan pesan usage yang ringan).
        if not await self._check_command_rate_limit(update, context):
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        try:
            tz_wib = ZoneInfo(MORNING_BRIEF_TIMEZONE)
            today = datetime.now(tz_wib).date()
            from_date = (today - timedelta(days=self.AFTERMATH_SEARCH_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            to_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            events = await self.macro.get_economic_calendar(from_date=from_date, to_date=to_date)
        except Exception as e:
            logger.error(f"Aftermath calendar fetch error: {e}")
            await safe_reply_text(
                update.message,
                "❌ Gagal memuat kalender ekonomi. Silakan coba lagi nanti.",
            )
            return

        matches = self._search_aftermath_events(events, arg)
        if not matches:
            await safe_reply_text(
                update.message,
                f"❌ Tidak ada event yang cocok dengan *\"{arg}\"* dalam 14 hari terakhir.\n\n"
                f"Contoh: `/aftermath nfp`, `/aftermath cpi`, `/aftermath fomc`, "
                f"`/aftermath gdp`, `/aftermath unemployment`.",
                parse_mode="Markdown",
            )
            return

        event = matches[0]
        note = ""
        if len(matches) > 1:
            note = (
                f"ℹ️ {len(matches)} kandidat cocok — ditampilkan yang paling baru: "
                f"*{event.get('event')}*.\n\n"
            )

        message = await self._build_aftermath_for_event(event)

        await safe_reply_text(
            update.message,
            f"{note}{message}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # ===================== CALENDAR AFTERMATH BUTTONS =====================

    @staticmethod
    def _event_short_id(event: Dict) -> str:
        """ID pendek stabil per event — payload callback tombol kalender (≤64 byte)."""
        key = MarketBot._aftermath_key(event)
        return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def _short_event_label(name: str) -> str:
        """Label tombol pendek untuk nama event yang panjang (mis. 'NFP', 'CPI')."""
        n = name or ""
        for kw, short in (
            ("Non-Farm Payrolls", "NFP"),
            ("Fed Funds Rate", "FOMC"),
            ("Unemployment Rate", "UNEMP"),
            ("Initial Jobless Claims", "CLAIMS"),
            ("Inflation Rate", "CPI"),
            ("CPI", "CPI"),
            ("PPI", "PPI"),
            ("GDP", "GDP"),
            ("Retail Sales", "RETAIL"),
            ("Consumer Confidence", "CONF"),
            ("Trade Balance", "TRADE"),
            ("ISM", "ISM"),
            ("PMI", "PMI"),
        ):
            if kw in n:
                return short
        # Fallback: potong kata-kata pertama (maks ±14 karakter)
        words = n.split()
        if not words:
            return "EVENT"
        label = ""
        for w in words:
            if len(label) + len(w) + 1 > 14:
                break
            label = f"{label} {w}" if label else w
        return label.upper() or "EVENT"

    def _build_calendar_aftermath_buttons(self, events: List[Dict], max_buttons: int = 15, numbered: bool = False) -> Optional[InlineKeyboardMarkup]:
        """
        Keyboard '📊 Analisis Dampak' untuk SEMUA event high-impact yang tampil
        (urutan sama dengan daftar kalender, 3 tombol per baris agar ringkas).
        None bila tidak ada event. Hanya high-impact yang diberi tombol
        (konsisten dengan matching di callback).

        Args:
            numbered: Jika True, label tombol diberi nomor urut yang sama dengan
                daftar kalender (dipakai /calendar agar mudah dipetakan).
        """
        picked = [e for e in (events or []) if e.get("impact") == "high"][:max_buttons]
        if not picked:
            return None
        rows: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        used_labels = set()
        for i, e in enumerate(picked, start=1):
            label = self._short_event_label(e.get("event", ""))
            if label in used_labels:
                label = f"{label} {len(used_labels) + 1}"  # dedupe (mis. CPI MoM vs CPI YoY)
            used_labels.add(label)
            text = f"📊 {i}·{label}" if numbered else f"📊 {label}"
            row.append(
                InlineKeyboardButton(
                    text,
                    callback_data=f"aft:{self._event_short_id(e)}",
                )
            )
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    async def _build_aftermath_for_event(self, event: Dict) -> str:
        """Bangun pesan analisis dampak lengkap untuk satu event (angka + pasar + AI).
        Tidak pernah raise — fallback ke interpretasi statis bila AI/gagal."""
        try:
            market_line = await self._build_market_line()
            return await self._build_aftermath_message(event, market_line, manual=True)
        except Exception as e:
            logger.warning(f"Aftermath gagal untuk {event.get('event')}: {e}")
            return (
                f"🎯 *ANALISIS DAMPAK EVENT*\n{event.get('country_emoji', '')} "
                f"*{event.get('event', '')}*\n🕐 {event.get('time', '')}\n\n"
                f"{self._format_event_numbers(event)}\n\n"
                f"📰 {self._static_event_interpretation(event)}\n\n{DISCLAIMER}"
            )

    def _add_refresh_button(self, kb: Optional[InlineKeyboardMarkup], callback: str = "calendar_refresh") -> InlineKeyboardMarkup:
        """Tambahkan baris tombol '🔁 Refresh' di bawah keyboard (kalender/overview).
        Selalu ada — agar halaman bisa dimuat ulang tanpa mengetik ulang perintah."""
        rows = list(kb.inline_keyboard) if kb else []
        rows.append([InlineKeyboardButton("🔁 Refresh", callback_data=callback)])
        return InlineKeyboardMarkup(rows)

    async def _build_calendar_reply(self, refresh: bool = False) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        """Bangun isi pesan /calendar + tombol analisis dampak & refresh
        (dipakai /calendar, tombol menu kalender, dan '🔁 Refresh')."""
        events = await self.macro.get_economic_calendar_month(refresh=refresh)
        # numbered=True: event berindeks (1., 2., ...) agar mudah dipetakan ke
        # tombol '📊 Analisis Dampak' (tombol memakai nomor yang sama).
        calendar_text = self.macro.format_calendar_text(
            events, max_events=15, only_high=True, numbered=True
        )
        message = f"{calendar_text}\n{DISCLAIMER}"
        displayed = [e for e in events if e.get("impact") == "high"][:15]
        aft_kb = self._build_calendar_aftermath_buttons(displayed, numbered=True)
        if aft_kb:
            message = f"{calendar_text}\n\n📊 *Ketuk tombol event untuk analisis dampak.*\n{DISCLAIMER}"
        return message, self._add_refresh_button(aft_kb)

    async def _handle_calendar_aftermath_button(self, query, data: str):
        """Tombol '📊 Analisis Dampak' pada pesan /calendar → kirim analisis event.
        Mencocokkan ulang via ID pendek (kalender di-cache, jadi stabil)."""
        target = data.split(":", 1)[1] if ":" in data else ""
        if not target:
            await safe_edit_message_text(query, "⚠️ Tombol tidak valid. Kirim /calendar lagi.")
            return
        try:
            # Jendela lebar agar mencakup semua sumber tombol: kalender bulan ini,
            # digest (hari ini), dan reminder (maks +7 hari / lintas bulan).
            tz_wib = ZoneInfo(MORNING_BRIEF_TIMEZONE)
            today = datetime.now(tz_wib).date()
            month_start = today.replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            from_date = min(month_start, today - timedelta(days=14)).strftime("%Y-%m-%d")
            to_date = max(month_end, today + timedelta(days=7)).strftime("%Y-%m-%d")
            events = await self.macro.get_economic_calendar(from_date=from_date, to_date=to_date)
        except Exception as e:
            logger.error(f"Aftermath button calendar fetch error: {e}")
            await safe_edit_message_text(
                query, "❌ Gagal memuat kalender ekonomi. Silakan coba lagi nanti."
            )
            return
        event = None
        for e in events or []:
            if e.get("impact") != "high":
                continue
            if self._event_short_id(e) == target:
                event = e
                break
        if event is None:
            await safe_edit_message_text(
                query,
                "⚠️ Event tidak ditemukan — kalender mungkin sudah berganti bulan. "
                "Kirim /calendar untuk daftar terbaru.",
            )
            return
        message = await self._build_aftermath_for_event(event)
        await safe_reply_text(
            query.message,
            message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # ===================== SCHEDULED MORNING BRIEF =====================

    async def send_scheduled_morning_brief(self, application: Application):
        """
        Kirim morning brief ke semua chat yang terdaftar.
        Dipanggil oleh scheduler setiap pagi.
        """
        logger.info("Sending scheduled morning brief...")

        brief = await self._generate_morning_brief()

        # Gabungkan chat_ids dari ENV dan Database
        env_chat_ids = []
        if MORNING_BRIEF_CHAT_IDS:
            env_chat_ids = [int(x.strip()) for x in MORNING_BRIEF_CHAT_IDS.split(",") if x.strip()]
            
        db_chat_ids = await db.get_all_subscribers_async()
        
        # Buat unique list
        chat_ids = list(set(env_chat_ids + db_chat_ids))
        
        if not chat_ids:
            logger.info("No subscribers found for morning brief.")
            return

        for chat_id in chat_ids:
            try:
                await safe_send_message(
                    application.bot,
                    chat_id=chat_id,
                    text=brief,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                logger.info(f"Morning brief sent to chat {chat_id}")
            except Exception as e:
                logger.error(f"Failed to send morning brief to {chat_id}: {e}")
