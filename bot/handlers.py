"""
Telegram Bot Handlers - Semua handler untuk perintah dan pesan dari user.
Menggunakan python-telegram-bot v20.x.
Now with multi-agent analysis system from MarketLens.
"""
import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config.settings import (
    TELEGRAM_TOKEN,
    MORNING_BRIEF_HOUR,
    MORNING_BRIEF_MINUTE,
    MORNING_BRIEF_TIMEZONE,
    MORNING_BRIEF_CHAT_IDS,
    ENABLE_MULTI_AGENT,
    ECONOMIC_ALERT_LEAD_HOURS,
    EVENT_AFTERMATH_ENABLED,
    EVENT_AFTERMATH_LOOKBACK_HOURS,
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
from data.conversation_memory import format_history, add_exchange, get_context, clear
from utils.chart_generator import ChartGenerator
from analysis.director import AnalysisDirector
from analysis.indicators import compute_indicators, format_key_levels, format_indicators_for_prompt
from utils.validators import sanitize_text
from prompts.loader import format_prompt
from analysis.monitoring import metrics
from analysis.sentiment import SentimentAnalyzer
from bot.messages import (
    WELCOME_MESSAGE,
    HELP_MESSAGE,
    ABOUT_MESSAGE,
    STATUS_MESSAGE_TEMPLATE,
    ERROR_MESSAGE,
    RATE_LIMIT_MESSAGE,
    DISCLAIMER,
    CHART_HELP_TEXT,
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
        if "too long" in str(e).lower():
            if query.message is not None:
                logger.info(f"Edit message too long ({len(text)} chars), replying with parts instead...")
                result = None
                for chunk in split_long_text(text):
                    result = await _reply_chunk(query.message, chunk, parse_mode, kwargs)
                return result
            # Tidak ada message untuk di-reply (mis. inline mode) — biarkan error asli
            raise
        if any(err in str(e).lower() for err in ["parse", "entity", "entities"]):
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


def _quick_action_keyboard(symbol: Optional[str] = None):
    """Keyboard aksi cepat — simbol ter-embed di callback agar tombol lama
    tetap bekerja untuk instrumen yang benar walau konteks sudah berubah."""
    sr_data = f"qa:sr:{symbol}" if symbol else "qa:sr"
    scenario_data = f"qa:scenario:{symbol}" if symbol else "qa:scenario"
    chart_data = f"qa:chart:{symbol}" if symbol else "qa:chart"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 S/R & Target", callback_data=sr_data),
            InlineKeyboardButton("🔮 Skenario", callback_data=scenario_data),
        ],
        [
            InlineKeyboardButton("📈 Chart", callback_data=chart_data),
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


# ===================== PRICE ALERTS =====================
# Alert harga per-user: bot memantau harga target dan mengirim notifikasi saat
# tersentuh. Disimpan di bot_data (in-memory). Diperiksa berkala oleh job
# scheduler (PRICE_ALERT_CHECK_MINUTES).
PRICE_ALERT_MAX_PER_USER = 10
PRICE_ALERT_MAX_TOTAL = 150

PRICE_ALERT_USAGE = (
    "🔔 *ALERT HARGA*\n\n"
    "Bot akan mengirim notifikasi saat harga menyentuh target yang kamu pasang.\n\n"
    "Contoh:\n"
    "`/pa eurusd 1.0900` — notifikasi saat EUR/USD *naik* ke 1.0900\n"
    "`/pa gold 2350` — notifikasi saat Gold *turun* ke 2350 (di bawah harga sekarang)\n\n"
    "Kelola:\n"
    "`/pa list` — daftar alert kamu\n"
    "`/pa del <id>` — hapus satu alert\n"
    "`/pa clear` — hapus semua alert kamu\n\n"
    "⚠️ Alert tersimpan sementara di memori bot — hilang saat bot restart."
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
        self.start_time = time.time()
        self.total_questions = 0

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

    # ===================== COMMAND HANDLERS =====================

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /start."""
        user = update.effective_user
        logger.info(f"User {user.id} ({user.first_name}) started the bot")
        
        # Simpan/update user ke database (async — jangan blokir event loop)
        await db.upsert_user_async(user.id, user.username, user.first_name)

        # Keyboard menu — dikelompokkan per tema, 2 kolom agar rapi di layar HP
        keyboard = [
            # 📊 Pasar
            [
                InlineKeyboardButton("🥇 Harga Gold", callback_data="gold_price"),
                InlineKeyboardButton("💱 EUR/USD", callback_data="eurusd"),
            ],
            [
                InlineKeyboardButton("🌍 Overview Pasar", callback_data="overview"),
                InlineKeyboardButton("🧠 Sentimen Retail", callback_data="sentimen_retail"),
            ],
            [
                InlineKeyboardButton("🏛️ Data Makro", callback_data="macro"),
                InlineKeyboardButton("📰 Sentimen Pasar", callback_data="sentiment"),
            ],
            # 📈 Chart
            [
                InlineKeyboardButton("📈 Chart Gold", callback_data="chart_gold"),
                InlineKeyboardButton("📈 Chart EUR/USD", callback_data="chart_eurusd"),
            ],
            [
                InlineKeyboardButton("📈 Chart DXY", callback_data="chart_dxy"),
                InlineKeyboardButton("📈 Chart BTC", callback_data="chart_btc"),
            ],
            # 🔔 Notifikasi & Jadwal
            [
                InlineKeyboardButton("🌅 Morning Brief", callback_data="morning"),
                InlineKeyboardButton("📅 Kalender", callback_data="calendar"),
            ],
            [
                InlineKeyboardButton("🔔 Alert Event", callback_data="alert_on"),
                InlineKeyboardButton("🎯 Alert Harga", callback_data="pa_usage"),
            ],
            [
                InlineKeyboardButton("👀 Watchlist", callback_data="watch_list"),
                InlineKeyboardButton("📜 Riwayat Harga", callback_data="riwayat_usage"),
            ],
            # ⚙️ Lainnya
            [
                InlineKeyboardButton("❓ Bantuan", callback_data="help"),
                InlineKeyboardButton("🔔 Langganan Brief", callback_data="subscribe"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await safe_reply_text(
            update.message,
            WELCOME_MESSAGE,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

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

    # ===================== CHART COMMAND =====================

    async def chart_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /chart - Generate grafik harga."""
        user_text = update.message.text
        chat_id = update.effective_chat.id

        # Parse symbol dari teks
        symbol, display_name = self.chart.get_chart_symbol_from_text(user_text)

        if not symbol:
            # Kirim petunjuk jika tidak ada simbol yang dikenali
            await safe_reply_text(
                update.message,
                CHART_HELP_TEXT,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")

        try:
            await self._generate_and_send_chart(chat_id, symbol, display_name, context)
        except Exception as e:
            logger.error(f"Chart error for {symbol}: {e}")
            await safe_reply_text(
                update.message,
                f"❌ Gagal membuat chart untuk *{display_name}*. Silakan coba lagi nanti.",
                parse_mode="Markdown",
            )

    async def _send_chart(self, query, symbol: str, chat_id: int, context):
        """Kirim chart dari callback inline keyboard."""
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        display_name = ChartGenerator._get_display_name(symbol)

        try:
            await self._generate_and_send_chart(chat_id, symbol, display_name, context)
        except Exception as e:
            logger.error(f"Chart callback error for {symbol}: {e}")
            await safe_edit_message_text(
                query,
                f"❌ Gagal membuat chart untuk *{display_name}*.",
                parse_mode="Markdown",
            )

    async def _generate_and_send_chart(self, chat_id: int, symbol: str, display_name: str, context):
        """
        Generate chart dari data Yahoo Finance dan kirim ke Telegram.
        """
        # Ambil data OHLCV - period disesuaikan dengan instrumen
        if symbol in ("GC=F", "SI=F"):
            period = "1mo"
            interval = "1d"
        elif symbol in ("BTC-USD", "ETH-USD"):
            period = "1mo"
            interval = "1d"
        else:
            period = "5d"
            interval = "1h"

        data = await asyncio.to_thread(
            self.market.get_yahoo_data,
            symbol,
            period=period,
            interval=interval,
            ohlcv_limit=60,
        )

        if "error" in data or not data.get("ohlcv"):
            await safe_send_message(
                context.bot,
                chat_id=chat_id,
                text=f"❌ Data harga untuk *{display_name}* tidak tersedia saat ini.",
                parse_mode="Markdown",
            )
            return

        ohlcv = data.get("ohlcv", [])
        current_price = data.get("current_price")
        change = data.get("change_pct")

        chart_path = None
        try:
            # Generate candlestick chart LOKAL (matplotlib), fallback ke line chart
            chart_path = self.chart.build_candlestick_chart(ohlcv, symbol)
            if not chart_path:
                # Fallback: line chart sederhana
                prices = [d.get("close", 0) for d in ohlcv]
                labels = [d.get("date", "")[-5:] for d in ohlcv]
                chart_path = self.chart.build_line_chart(prices, labels, symbol)

            if not chart_path:
                await safe_send_message(
                    context.bot,
                    chat_id=chat_id,
                    text=f"❌ Gagal membuat grafik untuk *{display_name}*. Silakan coba lagi nanti.",
                    parse_mode="Markdown",
                )
                return

            # Buat caption
            arrow = "🟢" if change and change > 0 else "🔴" if change and change < 0 else "⚪"
            change_str = f"{change:+.2f}%" if change is not None else ""

            # Lampirkan level kunci (pivot + fib) pada caption chart
            levels_line = ""
            try:
                ind = compute_indicators(ohlcv)
                lv = format_key_levels(ind)
                if lv:
                    levels_line = f"\n{lv}"
            except Exception:
                pass

            caption = (
                f"📈 *{display_name}*\n"
                f"{arrow} Harga: *{format_price(current_price, symbol)}* {change_str}\n"
                f"📊 Periode: {period} ({interval})"
                f"{levels_line}\n"
            )

            # Kirim file PNG langsung ke Telegram (tanpa layanan chart eksternal)
            with open(chart_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error(f"Chart generation/send error for {symbol}: {e}", exc_info=True)
            await safe_send_message(
                context.bot,
                chat_id=chat_id,
                text=f"❌ Gagal membuat grafik untuk *{display_name}*. Silakan coba lagi nanti.",
                parse_mode="Markdown",
            )
        finally:
            # Bersihkan file temp chart
            if chart_path:
                try:
                    os.remove(chart_path)
                except OSError:
                    pass

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
        upper = norm.upper()
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

    # ===================== WATCHLIST (/watch) =====================

    WATCH_USAGE = (
        "👀 *WATCHLIST*\n\n"
        "Simpan instrumen favorit kamu — bot mencatat riwayat harganya otomatis "
        "setiap 30 menit dan riwayat itu bisa dilihat kapan pun.\n\n"
        "Contoh:\n"
        "`/watch add eurusd` — tambah EUR/USD\n"
        "`/watch add gold` — tambah XAU/USD\n"
        "`/watch list` — lihat daftar\n"
        "`/watch del eurusd` — hapus satu\n"
        "`/watch clear` — hapus semua"
    )

    async def watch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /watch — kelola watchlist per user (persisten di Supabase)."""
        text = update.message.text or ""
        arg = text.replace("/watch", "").strip().lower()
        chat_id = update.effective_chat.id

        if not arg or arg in ("help", "bantuan"):
            await safe_reply_text(update.message, self.WATCH_USAGE, parse_mode="Markdown")
            return

        # ===== list =====
        if arg == "list":
            items = await db.get_watchlist_async(chat_id)
            if not items:
                await safe_reply_text(
                    update.message,
                    "👀 *Watchlist kamu kosong.*\n\nContoh: `/watch add eurusd`",
                    parse_mode="Markdown",
                )
                return
            lines = ["👀 *Watchlist kamu:*"]
            for it in items:
                label = it.get("label") or it.get("symbol", "")
                lines.append(f"• {label}")
            lines.append("\nHapus: `/watch del <simbol>` | Riwayat: `/riwayat <simbol>`")
            await safe_reply_text(update.message, "\n".join(lines), parse_mode="Markdown")
            return

        # ===== clear =====
        if arg == "clear":
            items = await db.get_watchlist_async(chat_id)
            ok = True
            for it in items:
                ok = await db.remove_watch_async(chat_id, it.get("symbol", "")) and ok
            await safe_reply_text(
                update.message,
                "🧹 Watchlist kamu sudah dikosongkan." if ok else "⚠️ Gagal membersihkan watchlist (cek database).",
            )
            return

        # ===== del <simbol> =====
        if arg.startswith("del "):
            symbol_text = arg[4:].strip()
            symbol, display_name = self._resolve_symbol_from_text(symbol_text)
            if not symbol:
                await safe_reply_text(
                    update.message, "❌ Simbol tidak dikenali. Contoh: `/watch del eurusd`."
                )
                return
            if await db.remove_watch_async(chat_id, symbol):
                await safe_reply_text(
                    update.message,
                    f"🗑️ *{display_name}* dihapus dari watchlist.",
                    parse_mode="Markdown",
                )
            else:
                await safe_reply_text(
                    update.message,
                    "⚠️ Gagal menghapus (cek apakah database sudah dikonfigurasi, atau "
                    "simbol memang tidak ada di watchlist kamu).",
                )
            return

        # ===== add <simbol> (default bila arg bukan perintah) =====
        symbol, display_name = self._resolve_symbol_from_text(arg)
        if not symbol:
            await safe_reply_text(
                update.message,
                "❌ Simbol tidak dikenali. Contoh: `/watch add eurusd`, `/watch add gold`, "
                "`/watch add eurgbp`.",
                parse_mode="Markdown",
            )
            return

        items = await db.get_watchlist_async(chat_id)
        if any(it.get("symbol") == symbol for it in items):
            await safe_reply_text(
                update.message,
                f"✅ *{display_name}* sudah ada di watchlist kamu.",
                parse_mode="Markdown",
            )
            return
        if len(items) >= 10:
            await safe_reply_text(
                update.message, "⚠️ Maksimal 10 instrumen per watchlist. Hapus dulu dengan `/watch del`."
            )
            return

        if await db.add_watch_async(chat_id, symbol, display_name):
            await safe_reply_text(
                update.message,
                f"✅ *{display_name}* ditambahkan ke watchlist!\n\n"
                f"Bot mulai mencatat riwayat harganya otomatis. Cek kapan pun: `/riwayat {arg}`.",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(
                update.message,
                "❌ Gagal menyimpan watchlist. Pastikan database Supabase sudah dikonfigurasi "
                "(`SUPABASE_URL` & `SUPABASE_KEY`) dan tabel `watchlist` sudah dibuat "
                "(lihat migrations/supabase.sql).",
            )

    # ===================== RIWAYAT HARGA (/riwayat) =====================

    RIWAYAT_USAGE = (
        "📜 *RIWAYAT HARGA*\n\n"
        "Menampilkan snapshot harga yang dicatat bot (setiap 30 menit) untuk "
        "instrumen yang ada di watchlist.\n\n"
        "`/riwayat eurusd` — riwayat EUR/USD\n"
        "`/riwayat gold` — riwayat XAU/USD\n"
        "`/riwayat btc` — riwayat Bitcoin"
    )

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /riwayat — tampilkan riwayat harga tersimpan (dari Supabase)."""
        text = update.message.text or ""
        arg = text.replace("/riwayat", "").replace("/history", "").strip().lower()

        if not arg or arg in ("help", "bantuan"):
            await safe_reply_text(
                update.message,
                self.RIWAYAT_USAGE,
                parse_mode="Markdown",
            )
            return

        symbol, display_name = self._resolve_symbol_from_text(arg)
        if not symbol:
            await safe_reply_text(
                update.message, "❌ Simbol tidak dikenali. Contoh: `/riwayat eurusd`, `/riwayat gold`."
            )
            return

        rows = await db.get_price_history_async(symbol, limit=48)
        if not rows:
            await safe_reply_text(
                update.message,
                f"📜 Belum ada riwayat harga untuk *{display_name}*.\n\n"
                f"Tambahkan ke watchlist dulu: `/watch add {arg}` — bot mulai mencatat "
                f"harga setiap 30 menit.",
                parse_mode="Markdown",
            )
            return

        # rows terbaru dulu (order desc) → balik agar kronologis
        rows = list(reversed(rows))
        prices = [float(r["price"]) for r in rows if r.get("price") is not None]
        if not prices:
            await safe_reply_text(update.message, "⚠️ Data riwayat tidak valid.")
            return

        first = prices[0]
        last = prices[-1]
        pct = round((last - first) / first * 100, 2) if first else 0.0
        arrow = "🟢" if pct > 0 else "🔴" if pct < 0 else "⚪"

        lines = [
            f"📜 *RIWAYAT HARGA {display_name.upper()}*\n",
            f"{arrow} Rentang: {format_price(first, symbol)} → {format_price(last, symbol)} "
            f"({pct:+.2f}%)\n",
        ]

        # Sampel maksimal 12 titik (kronologis) agar pesan ringkas
        step = max(1, len(rows) // 12)
        for r in rows[::step]:
            try:
                ts = r["created_at"][:16].replace("T", " ")
            except (KeyError, TypeError):
                ts = ""
            p = r.get("price")
            if p is None:
                continue
            bid = r.get("bid")
            ask = r.get("ask")
            detail = f"  Bid {format_price(bid, symbol)} / Ask {format_price(ask, symbol)}" if (
                bid is not None and ask is not None
            ) else ""
            lines.append(f"`{ts}`  {format_price(p, symbol)}{detail}")

        lines.append(
            "\n💡 Snapshot dicatat setiap 30 menit untuk instrumen di watchlist. "
            "Untuk chart penuh: `/chart " + arg + "`"
        )
        await safe_reply_text(
            update.message,
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # ===================== PRICE HISTORY RECORDER (job) =====================

    async def record_price_history(self, application: Application):
        """
        Job berkala: simpan snapshot harga untuk semua simbol di semua watchlist.
        Dipanggil scheduler (main.py) setiap 30 menit. Data di-cache oleh data
        layer (OANDA real-time TTL 30 dtk) sehingga biaya request sangat kecil.
        """
        symbols = await db.get_all_watched_symbols_async()
        if not symbols:
            return
        logger.info(f"Recording price history for {len(symbols)} watched symbols...")

        results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    self.market.get_yahoo_data, s, period="1d", interval="1h", ohlcv_limit=1
                )
                for s in symbols
            ],
            return_exceptions=True,
        )
        saved = 0
        for symbol, data in zip(symbols, results):
            if isinstance(data, Exception):
                logger.warning(f"Price history fetch failed for {symbol}: {data}")
                continue
            price = data.get("current_price")
            if price is None or "error" in data:
                continue
            ok = await db.save_price_snapshot_async(
                symbol,
                price=float(price),
                change_pct=data.get("change_pct"),
                bid=data.get("bid"),
                ask=data.get("ask"),
            )
            if ok:
                saved += 1
        if saved:
            logger.info(f"Price history: {saved}/{len(symbols)} snapshots saved")

    # ===================== MORNING BRIEF =====================

    async def alert_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /alert - kelola notifikasi event ekonomi otomatis."""
        chat_id = update.effective_chat.id
        text = update.message.text or ""
        arg = text.replace("/alert", "").strip().lower()

        # Simpan daftar subscriber di bot_data agar bisa diakses job scheduler
        subscribers = context.bot_data.setdefault("event_alert_subscribers", set())

        if arg in ("off", "0", "false", "stop", "matikan", "berhenti"):
            subscribers.discard(chat_id)
            await safe_reply_text(update.message, ALERT_OFF_MESSAGE, parse_mode="Markdown")
        else:
            subscribers.add(chat_id)
            await safe_reply_text(update.message, ALERT_ON_MESSAGE, parse_mode="Markdown")

        context.bot_data["event_alert_subscribers"] = subscribers

    # ===================== PRICE ALERTS =====================

    async def price_alert_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Kelola alert harga: /pa <simbol> <harga> | /pa list | /pa del <id> | /pa clear.
        Arah trigger otomatis ditentukan dari harga saat ini vs target.
        """
        text = update.message.text or ""
        arg = text.replace("/pa", "", 1).strip()
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        alerts = list(context.bot_data.get("price_alerts", []))

        if not arg or arg.lower() in ("help", "bantuan"):
            await safe_reply_text(update.message, PRICE_ALERT_USAGE, parse_mode="Markdown")
            return

        if arg.lower() == "list":
            mine = [a for a in alerts if a.get("user_id") == user_id]
            if not mine:
                await safe_reply_text(
                    update.message,
                    "🔔 *Alert harga:* belum ada.\n\nContoh: `/pa eurusd 1.0900`",
                    parse_mode="Markdown",
                )
                return
            lines = ["🔔 *Alert harga kamu:*"]
            for a in mine:
                direction = "naik ke" if a.get("direction") == "above" else "turun ke"
                lines.append(
                    f"• `{a['id']}` — {a.get('display_name', a.get('symbol'))} → {direction} "
                    f"{format_price(a['target'], a.get('symbol', ''))}"
                )
            lines.append("\nHapus: `/pa del <id>`")
            await safe_reply_text(update.message, "\n".join(lines), parse_mode="Markdown")
            return

        if arg.lower() == "clear":
            context.bot_data["price_alerts"] = [
                a for a in alerts if a.get("user_id") != user_id
            ]
            await safe_reply_text(update.message, "🧹 Semua alert harga kamu sudah dihapus.")
            return

        if arg.lower().startswith("del "):
            try:
                alert_id = int(arg.split()[1])
            except (IndexError, ValueError):
                await safe_reply_text(
                    update.message, "Gunakan: `/pa del <id>` (lihat daftar via `/pa list`)."
                )
                return
            before = len(alerts)
            context.bot_data["price_alerts"] = [
                a for a in alerts
                if not (a.get("user_id") == user_id and a.get("id") == alert_id)
            ]
            removed = before - len(context.bot_data["price_alerts"])
            if removed:
                await safe_reply_text(update.message, f"🗑️ Alert `{alert_id}` dihapus.")
            else:
                await safe_reply_text(update.message, f"⚠️ Alert `{alert_id}` tidak ditemukan.")
            return

        # Tambah alert baru: /pa <simbol> <harga target>
        parsed = self._parse_price_alert_args(arg)
        if not parsed:
            await safe_reply_text(update.message, PRICE_ALERT_USAGE, parse_mode="Markdown")
            return
        symbol, display_name, target = parsed

        try:
            data = await asyncio.to_thread(
                self.market.get_yahoo_data, symbol, period="1d", interval="1h"
            )
        except Exception as e:
            logger.warning(f"Price alert fetch failed for {symbol}: {e}")
            data = {}
        current = data.get("current_price")
        if current is None or "error" in data:
            await safe_reply_text(
                update.message,
                f"❌ Data harga *{display_name}* tidak tersedia. Coba simbol lain.",
                parse_mode="Markdown",
            )
            return

        mine_count = len([a for a in alerts if a.get("user_id") == user_id])
        if mine_count >= PRICE_ALERT_MAX_PER_USER:
            await safe_reply_text(
                update.message,
                f"⚠️ Maksimal {PRICE_ALERT_MAX_PER_USER} alert per user. "
                f"Hapus dulu dengan `/pa del <id>`.",
                parse_mode="Markdown",
            )
            return
        if len(alerts) >= PRICE_ALERT_MAX_TOTAL:
            await safe_reply_text(
                update.message, "⚠️ Kuota alert bot sedang penuh. Coba lagi nanti."
            )
            return

        # Arah trigger: harga sekarang < target → tunggu NAIK; sebaliknya → tunggu TURUN
        direction = "below" if current >= target else "above"
        alert_id = context.bot_data.get("price_alert_next_id", 1)
        context.bot_data["price_alert_next_id"] = alert_id + 1
        alerts.append({
            "id": alert_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "symbol": symbol,
            "display_name": display_name,
            "target": target,
            "direction": direction,
            "created": time.time(),
        })
        context.bot_data["price_alerts"] = alerts

        arrow = "🟢 naik ke" if direction == "above" else "🔴 turun ke"
        await safe_reply_text(
            update.message,
            f"🔔 *Alert harga aktif!*\n\n"
            f"{display_name} → {arrow} *{format_price(target, symbol)}*\n"
            f"💱 Harga sekarang: {format_price(current, symbol)}\n\n"
            f"Saya akan kabari saat target tersentuh. 📣\n"
            f"Lihat: `/pa list` | Hapus: `/pa del {alert_id}`",
            parse_mode="Markdown",
        )

    @staticmethod
    def _normalize_separators(s: str, sep: str) -> str:
        """
        Normalisasi pemisah ribuan/desimal pada string angka.

        - `sep` diikuti tepat 3 digit (dan bagian depan bukan "0.") → pemisah
          RIBUAN ("2,350" → "2350", "1.234.567" → "1234567").
        - Selain itu → pemisah DESIMAL ("2,35" → "2.35", "1.0900" → "1.09").
        """
        head, _, tail = s.partition(sep)
        groups = tail.split(sep)
        all_thousands = (
            bool(head)
            and not head.startswith("0")
            and all(len(g) == 3 for g in groups)
        )
        if all_thousands:
            return s.replace(sep, "")
        return s.replace(sep, ".")

    @staticmethod
    def _parse_price_target(raw: str) -> Optional[float]:
        """
        Parse angka target harga dengan toleransi format Indonesia & internasional:

        - "2,350"   → 2350.0   (koma diikuti 3 digit = ribuan, gaya Eropa)
        - "2.350"   → 2350.0   (titik diikuti 3 digit = ribuan, gaya Indonesia)
        - "1,234,567" → 1234567 (ribuan bertingkat)
        - "2,35"    → 2.35     (koma desimal gaya Indonesia)
        - "1.0900"  → 1.09     (titik desimal gaya internasional)
        - "2.350,50" → 2350.5  (titik ribuan + koma desimal)
        - "0,500" / "0.500" → 0.5 (nilai di bawah 1 selalu desimal)
        """
        s = raw.strip().replace(" ", "").replace("_", "")
        if not s:
            return None
        if "," in s and "." in s:
            # Kedua pemisah hadir — deteksi mana yang ribuan via aturan 3-digit:
            # "2.350,50" → 2350.5 (gaya Indonesia) | "1,000.50" → 1000.5 (gaya AS)
            if MarketBot._sep_is_thousands(s, "."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "").replace(".", ".")
        elif "," in s:
            s = MarketBot._normalize_separators(s, ",")
        elif "." in s:
            s = MarketBot._normalize_separators(s, ".")
        try:
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _sep_is_thousands(s: str, sep: str) -> bool:
        """True bila `sep` pertama di s bertindak sebagai pemisah RIBUAN
        (diikuti tepat 3 digit, lalu bukan digit lagi): '.' di "2.350,50",
        ',' di "1,000.50", tetapi False untuk '.' di "1.0900" (4 digit)."""
        idx = s.find(sep)
        if idx < 0:
            return False
        tail = s[idx + 1: idx + 4]
        after = s[idx + 4: idx + 5] if len(s) > idx + 4 else ""
        return len(tail) == 3 and tail.isdigit() and not after.isdigit()

    @staticmethod
    def _parse_price_alert_args(arg: str):
        """Parse '/pa eurusd 1.0900' → (symbol, display_name, target) atau None."""
        parts = arg.strip().split()
        if len(parts) < 2:
            return None
        target = MarketBot._parse_price_target(parts[-1])
        if target is None:
            return None
        symbol_text = " ".join(parts[:-1])
        symbol, display_name = ChartGenerator.get_chart_symbol_from_text(f"chart {symbol_text}")
        if not symbol:
            return None
        return symbol, display_name, target

    @staticmethod
    def _evaluate_price_alerts(alerts: List[Dict], prices: Dict[str, float]):
        """
        Evaluasi alert terhadap harga terkini (murni, tanpa I/O — mudah di-test).

        Returns:
            (triggered, remaining) — triggered berisi alert + harga saat terpicu.
        """
        triggered: List[Dict] = []
        remaining: List[Dict] = []
        for alert in alerts:
            price = prices.get(alert.get("symbol"))
            if price is None:
                remaining.append(alert)  # harga belum tersedia — cek lagi nanti
                continue
            target = alert.get("target")
            if alert.get("direction") == "above" and price >= target:
                triggered.append({**alert, "current_price": price})
            elif alert.get("direction") == "below" and price <= target:
                triggered.append({**alert, "current_price": price})
            else:
                remaining.append(alert)
        return triggered, remaining

    async def check_price_alerts(self, application: Application):
        """
        Job scheduler: cek semua alert harga & kirim notifikasi yang terpenuhi.
        Dipanggil berkala oleh job 'price_alerts' di main.py.
        """
        alerts = list(application.bot_data.get("price_alerts", []))
        if not alerts:
            return

        # Ambil harga terkini per simbol unik SECARA PARALEL (data di-cache oleh
        # data layer; tiap fetch jalan di thread agar tidak memblokir event loop).
        symbol_list = list({a.get("symbol") for a in alerts if a.get("symbol")})
        results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    self.market.get_yahoo_data, s, period="1d", interval="1h"
                )
                for s in symbol_list
            ],
            return_exceptions=True,
        )
        prices: Dict[str, float] = {}
        for symbol, data in zip(symbol_list, results):
            if isinstance(data, dict):
                price = data.get("current_price")
                if price is not None and "error" not in data:
                    prices[symbol] = float(price)
            else:
                logger.warning(f"Price alert check failed for {symbol}: {data}")

        triggered, remaining = self._evaluate_price_alerts(alerts, prices)
        for alert in triggered:
            try:
                emoji = "🟢" if alert["direction"] == "above" else "🔴"
                msg = (
                    f"🎯 *ALERT HARGA TERCAPAI!*\n\n"
                    f"{alert['display_name']} sekarang "
                    f"{format_price(alert['current_price'], alert['symbol'])} "
                    f"(target {format_price(alert['target'], alert['symbol'])}).\n\n"
                    f"{emoji} Kirim `/chart {alert['symbol']}` untuk grafiknya."
                )
                await application.bot.send_message(
                    chat_id=alert["chat_id"], text=msg, parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Price alert notify failed: {e}")
        application.bot_data["price_alerts"] = remaining

    async def calendar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /calendar - Kalender Ekonomi."""
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

    async def _build_overview_message(self) -> str:
        """
        Bangun pesan overview pasar (dipakai perintah /overview & tombol menu).
        Data dari cache (10 menit) → respons instan tanpa menunggu AI.

        Returns:
            String pesan siap kirim (tidak pernah raise — fallback aman).
        """
        try:
            summary = await asyncio.to_thread(self.market.get_market_summary)
            now_str = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%A, %d %B %Y %H:%M")
            return (
                f"🌍 *MARKET OVERVIEW*\n"
                f"🕐 {now_str} WIB\n\n"
                f"{summary}\n\n"
                f"💡 Kirim /chart <simbol> untuk grafik, atau tanyakan analisis "
                f"spesifik (mis. \"analisis eurusd\").\n"
                f"{DISCLAIMER}"
            )
        except Exception as e:
            logger.error(f"Overview error: {e}")
            return "❌ Gagal memuat overview pasar. Silakan coba lagi nanti."

    async def overview_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler untuk perintah /overview - Ringkasan cepat semua instrumen utama.
        Dibaca dari cache (10 menit), jadi responsnya INSTAN tanpa menunggu AI.
        """
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        message = await self._build_overview_message()
        await safe_reply_text(
            update.message,
            message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    async def morning_brief_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /morning - Morning Brief harian."""
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        brief = await self._generate_morning_brief()
        await safe_reply_text(
            update.message,
            brief,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    async def _generate_morning_brief(self) -> str:
        """
        Generate morning brief dengan data terkini.
        Menggabungkan data pasar, makro, kalender ekonomi, berita, dan AI-generated outlook.
        """
        # Tanggal harus sesuai zona WIB, bukan waktu server (yang bisa UTC)
        today = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%A, %d %B %Y")

        # Gather data secara parallel
        market_summary, macro_summary, calendar_events, news_summary, sentiment_text = await asyncio.gather(
            asyncio.to_thread(self.market.get_market_summary),
            asyncio.to_thread(self.macro.get_macro_summary),
            self.macro.get_economic_calendar(),
            self.news.get_news_summary("FOREX"),
            self._get_sentiment_text("FOREX"),
        )

        # Format kalender ekonomi untuk morning brief (top 3 high impact)
        calendar_text = self.macro.format_calendar_text(calendar_events, max_events=3)

        # AI-powered outlook & catalysts using multi-agent analysis
        if self.analysis_director:
            try:
                # Gunakan multi-agent untuk analisis yang lebih dalam
                analysis_prompt = self._build_morning_brief_prompt(
                    today, market_summary, macro_summary, calendar_text, news_summary, sentiment_text
                )

                result = await self.analysis_director.analyze(analysis_prompt)

                # Extract from analysis result (bersihkan prefix [via ...] + simbol *)
                ai_content = strip_markdown_asterisks(_strip_provider_prefix(result.final_response or ""))

                # Parse sections TANPA memotong konten (jangan hard-truncate 400/700 char)
                if "KATALIS UTAMA" in ai_content:
                    sections = ai_content.split("KATALIS UTAMA:")
                    outlook_part = sections[0].replace("OUTLOOK:", "").replace("OUTLOOK", "").strip()
                    catalysts_part = sections[1].strip() if len(sections) > 1 else ""
                else:
                    # Tidak ada marker: gunakan seluruh konten sebagai outlook
                    outlook_part = ai_content.strip()
                    catalysts_part = ""

                if not outlook_part:
                    outlook_part = "Belum ada data analisis untuk hari ini."
                if not catalysts_part:
                    catalysts_part = "Belum ada katalis utama yang teridentifikasi hari ini."

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

        # Fallback: legacy single-prompt method
        outlook_prompt = self._build_morning_brief_prompt(
            today, market_summary, macro_summary, calendar_text, news_summary, sentiment_text
        )

        ai_response = self.ai.generate(outlook_prompt, use_cache=True, max_tokens=4096)

        # Parse AI response (bersihkan prefix [via ...] + simbol *)
        ai_content = strip_markdown_asterisks(_strip_provider_prefix(ai_response))

        # Split into outlook and catalysts
        sections = ai_content.split("KATALIS UTAMA:")
        outlook = sections[0].replace("OUTLOOK:", "").strip() if sections else "Data belum tersedia"
        catalysts = sections[1].strip() if len(sections) > 1 else "Belum ada katalis utama yang teridentifikasi hari ini."

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

                core_answer = answer
                final_message = f"{answer}{DISCLAIMER}"

            # Hapus simbol '*' (markdown bold) dari jawaban agar tidak tampil mentah
            final_message = strip_markdown_asterisks(final_message)

            # Simpan JAWABAN INTI (tanpa badge multi-agent & disclaimer) ke memory
            # agar kuota karakter tidak habis oleh boilerplate.
            if core_answer:
                add_exchange(user_id, user_question, strip_markdown_asterisks(core_answer))

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
        add_exchange(user_id, question, content)
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
            brief = await self._generate_morning_brief()
            await safe_edit_message_text(
                query,
                brief,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        elif data == "overview":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            message = await self._build_overview_message()
            await safe_edit_message_text(
                query,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
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

        elif data == "chart_eurusd":
            await self._send_chart(query, "EURUSD=X", update.effective_chat.id, context)

        elif data == "chart_gold":
            await self._send_chart(query, "GC=F", update.effective_chat.id, context)

        elif data == "chart_dxy":
            await self._send_chart(query, "DX-Y.NYB", update.effective_chat.id, context)

        elif data == "chart_btc":
            await self._send_chart(query, "BTC-USD", update.effective_chat.id, context)

        elif data.startswith("chart_"):
            symbol = data.replace("chart_", "") + "=X"
            await self._send_chart(query, symbol, update.effective_chat.id, context)

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
            subscribers.add(chat_id)
            context.bot_data["event_alert_subscribers"] = subscribers
            await query.message.reply_text(ALERT_ON_MESSAGE, parse_mode="Markdown")

        elif data == "pa_usage":
            # Tombol menu: cara pakai alert harga
            await query.message.reply_text(PRICE_ALERT_USAGE, parse_mode="Markdown")

        elif data == "watch_list":
            # Tombol menu: tampilkan watchlist user (atau cara pakai bila kosong)
            chat_id = update.effective_chat.id
            items = await db.get_watchlist_async(chat_id)
            if not items:
                await query.message.reply_text(self.WATCH_USAGE, parse_mode="Markdown")
            else:
                lines = ["👀 *Watchlist kamu:*"]
                for it in items:
                    label = it.get("label") or it.get("symbol", "")
                    lines.append(f"• {label}")
                lines.append("\nTambah: `/watch add eurusd` | Riwayat: `/riwayat <simbol>`")
                await query.message.reply_text("\n".join(lines), parse_mode="Markdown")

        elif data == "riwayat_usage":
            # Tombol menu: cara pakai riwayat harga
            await query.message.reply_text(self.RIWAYAT_USAGE, parse_mode="Markdown")

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
            elif action == "chart":
                await self._send_chart(query, symbol, update.effective_chat.id, context)
                return
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
                    f"📅 *EVENT EKONOMI HIGH-IMPACT HARI INI*",
                    f"📆 {today.strftime('%A, %d %B %Y')}\n",
                ]
                for e in sorted(high_today, key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc)):
                    lines.append(f"{e.get('impact_label', '🔥 HIGH')} {e.get('country_emoji', '')} *{e.get('event', '')}*")
                    lines.append(f"   🕐 {e.get('time', '')}")
                lines.append("")
                lines.append("⚡ Pengingat akan dikirim menjelang jam rilis.")
                message = "\n".join(lines)

            for chat_id in list(subscribers):
                try:
                    await safe_send_message(
                        application.bot,
                        chat_id=chat_id,
                        text=message,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(f"Gagal kirim digest event ke {chat_id}: {e}")
                    if "Forbidden" in str(e):
                        subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
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

        try:
            events = await self.macro.get_economic_calendar()
            now_utc = datetime.now(timezone.utc)
            lead = timedelta(hours=ECONOMIC_ALERT_LEAD_HOURS)

            notified = set(application.bot_data.get("event_alert_notified", set()))

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
            application.bot_data["event_alert_notified"] = notified

            for e in new_keys:
                message = (
                    f"⏰ *REMINDER EVENT EKONOMI*\n\n"
                    f"{e.get('impact_label', '🔥 HIGH')} {e.get('country_emoji', '')} *{e.get('event', '')}*\n"
                    f"🕐 {e.get('time', '')}\n\n"
                    f"⚠️ Rilis dalam ±{ECONOMIC_ALERT_LEAD_HOURS} jam — bersiap untuk volatilitas!"
                )
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot,
                            chat_id=chat_id,
                            text=message,
                            parse_mode="Markdown",
                        )
                    except Exception as ex:
                        logger.error(f"Gagal kirim reminder event ke {chat_id}: {ex}")
                        if "Forbidden" in str(ex):
                            # User block bot / keluar — hapus dari daftar subscriber
                            subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
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

        return header + ai_section + f"\n{DISCLAIMER}"

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
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot, chat_id=chat_id, text=message, parse_mode="Markdown"
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
        except Exception as e:
            logger.error(f"Event aftermath error: {e}")

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

    @staticmethod
    def _pick_aftermath_buttons(events: List[Dict], max_buttons: int = 5, window_days: int = 14) -> List[Dict]:
        """
        Pilih event untuk tombol '📊 Analisis Dampak' di kalender — hanya yang
        paling relevan sekarang: rilis dalam window_days terakhir ATAU akan rilis
        dalam 7 hari ke depan. Diurutkan yang paling dekat dengan waktu sekarang.
        """
        now = datetime.now(timezone.utc)
        lo = now - timedelta(days=window_days)
        hi = now + timedelta(days=7)
        picked = []
        for e in events or []:
            dt = e.get("_dt_utc")
            if not dt or dt.tzinfo is None:
                continue
            if not (lo <= dt <= hi):
                continue
            picked.append((abs((dt - now).total_seconds()), e))
        picked.sort(key=lambda x: x[0])
        return [e for _, e in picked[:max_buttons]]

    def _build_calendar_aftermath_buttons(self, events: List[Dict], max_buttons: int = 5) -> Optional[InlineKeyboardMarkup]:
        """
        Keyboard '📊 Analisis Dampak' untuk event yang tampil di /calendar
        (2 tombol per baris). None bila tidak ada event relevan.
        Hanya event high-impact yang diberi tombol (konsisten dengan matching).
        """
        picked = self._pick_aftermath_buttons(
            [e for e in (events or []) if e.get("impact") == "high"],
            max_buttons=max_buttons,
        )
        if not picked:
            return None
        rows: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        used_labels = set()
        for e in picked:
            label = self._short_event_label(e.get("event", ""))
            if label in used_labels:
                label = f"{label} {len(used_labels) + 1}"  # dedupe (mis. CPI MoM vs CPI YoY)
            used_labels.add(label)
            row.append(
                InlineKeyboardButton(
                    f"📊 {label}",
                    callback_data=f"aft:{self._event_short_id(e)}",
                )
            )
            if len(row) == 2:
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

    async def _build_calendar_reply(self) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        """Bangun isi pesan /calendar + tombol analisis dampak (dipakai /calendar
        dan tombol menu kalender)."""
        events = await self.macro.get_economic_calendar_month()
        calendar_text = self.macro.format_calendar_text(events, max_events=15, only_high=True)
        message = f"{calendar_text}\n{DISCLAIMER}"
        displayed = [e for e in events if e.get("impact") == "high"][:15]
        kb = self._build_calendar_aftermath_buttons(displayed)
        return message, kb

    async def _handle_calendar_aftermath_button(self, query, data: str):
        """Tombol '📊 Analisis Dampak' pada pesan /calendar → kirim analisis event.
        Mencocokkan ulang via ID pendek (kalender di-cache, jadi stabil)."""
        target = data.split(":", 1)[1] if ":" in data else ""
        if not target:
            await safe_edit_message_text(query, "⚠️ Tombol tidak valid. Kirim /calendar lagi.")
            return
        try:
            events = await self.macro.get_economic_calendar_month()
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
