"""
Telegram Bot Handlers - Semua handler untuk perintah dan pesan dari user.
Menggunakan python-telegram-bot v20.x.
Now with multi-agent analysis system from MarketLens.
"""
import asyncio
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
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore
from config.providers import YAHOO_SYMBOLS, FRED_INDICATORS
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

        # Keyboard untuk quick actions
        keyboard = [
            [
                InlineKeyboardButton("🌅 Morning Brief", callback_data="morning"),
                InlineKeyboardButton("📊 Harga Gold", callback_data="gold_price"),
            ],
            [
                InlineKeyboardButton("💱 EUR/USD", callback_data="eurusd"),
                InlineKeyboardButton("🏛️ Data Makro", callback_data="macro"),
            ],
            [
                InlineKeyboardButton("📈 Chart EUR/USD", callback_data="chart_eurusd"),
                InlineKeyboardButton("📈 Chart Gold", callback_data="chart_gold"),
            ],
            [
                InlineKeyboardButton("🌍 Overview Pasar", callback_data="overview"),
                InlineKeyboardButton("🧠 Sentimen Pasar", callback_data="sentiment"),
            ],
            [
                InlineKeyboardButton("📅 Kalender Ekonomi", callback_data="calendar"),
                InlineKeyboardButton("❓ Bantuan", callback_data="help"),
            ],
            [
                InlineKeyboardButton("🔔 Langganan Brief", callback_data="subscribe"),
                InlineKeyboardButton("🔕 Berhenti", callback_data="unsubscribe"),
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

    async def calendar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /calendar - Kalender Ekonomi."""
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        try:
            # Kalender ekonomi BULAN INI, hanya event high impact
            events = await self.macro.get_economic_calendar_month()
            calendar_text = self.macro.format_calendar_text(events, max_events=15, only_high=True)

            message = f"{calendar_text}\n{DISCLAIMER}"
            await safe_reply_text(
                update.message,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
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
        Best practice: role jelas, alur berpikir, guardrail anti-halusinasi,
        format output eksplisit, dan larangan markdown (*).
        """
        sentiment_section = sentiment_text or "Sentimen pasar tidak tersedia."
        return (
            f"ROLE: Anda adalah analis pasar senior yang menyusun briefing pagi untuk "
            f"trader retail Indonesia yang sibuk.\n\n"
            f"Hari ini tanggal {today}.\n\n"
            f"ALUR BERPIKIR:\n"
            f"1) Tinjau data di bawah.\n"
            f"2) Tentukan prospek EUR/USD, Gold (XAU/USD), dan DXY hari ini.\n"
            f"3) Identifikasi katalis & risiko hari ini — khususnya dari KALENDER EKONOMI.\n"
            f"4) Tulis OUTLOOK (3-4 kalimat) dan KATALIS UTAMA (3-4 poin).\n\n"
            f"PENTING: Jangan mengarang event ekonomi. Hanya sebutkan event yang benar-benar ada "
            f"di KALENDER EKONOMI. Jika tidak ada event terjadwal, tulis 'Tidak ada rilis data besar hari ini'.\n\n"
            f"PANDUAN MEMBACA KALENDER: Setiap event punya 3 nilai berbeda — 'Forecast' = "
            f"ekspektasi/konsensus pasar, 'Previous' = nilai rilis sebelumnya, 'Actual' = nilai yang "
            f"sudah rilis (hanya ada untuk event bertanda 'Sudah rilis'; event bertanda 'Belum rilis' "
            f"tidak punya Actual). Nilai yang meleset jauh antara Actual vs Forecast adalah katalis "
            f"kuat hari itu — sorot jika ada.\n\n"
            f"DATA PASAR:\n{market_summary}\n\n"
            f"DATA MAKRO:\n{macro_summary}\n\n"
            f"KALENDER EKONOMI:\n{calendar_text}\n\n"
            f"BERITA:\n{news_summary}\n\n"
            f"SENTIMEN PASAR (skor -1 s/d +1):\n{sentiment_section}\n\n"
            f"Gunakan skor sentimen sebagai konteks tambahan — jangan dijadikan satu-satunya dasar.\n\n"
            f"FORMAT JAWABAN (tanpa simbol * / markdown):\n"
            f"OUTLOOK:\n[prospek singkat EUR/USD, Gold, dan DXY hari ini — 3-4 kalimat]\n\n"
            f"KATALIS UTAMA:\n[3-4 katalis/level/risiko yang perlu diwaspadai hari ini]\n\n"
            f"Jawab dalam Bahasa Indonesia. JANGAN gunakan simbol * atau **."
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
                ohlcv_data = None
                detected_pairs = self._detect_pairs(user_question)
                if detected_pairs:
                    pair_symbol = detected_pairs[0][1]
                else:
                    # Fallback: deteksi simbol via chart keyword map
                    # (mencakup "gold", "emas", "bitcoin", "dxy", dll)
                    _sym, _name = self.chart.get_chart_symbol_from_text(user_question)
                    pair_symbol = _sym
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
        """Detect forex pairs mentioned in question."""
        question_lower = question.lower()
        detected = []
        for pair, symbol in YAHOO_SYMBOLS.items():
            if pair in question_lower:
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
            f"{range_str}{quick}\n\n"
            f"⚡ Jawaban instan dari data pasar (delay 15-20 mnt).\n"
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
        """
        current_time = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%Y-%m-%d %H:%M WIB")

        # Analisis intent pertanyaan untuk prompt yang lebih relevan
        q = question.lower()
        intent_instruction = ""

        if any(kw in q for kw in ["teknikal", "support", "resistance", "rsi", "macd", "chart", "trend"]):
            intent_instruction = "Fokus pada analisis teknikal: level support/resistance, indikator, dan trend."
        elif any(kw in q for kw in ["nfp", "cpi", "inflasi", "gdp", "fed", "suku bunga", "tenaga kerja"]):
            intent_instruction = "Fokus pada data fundamental: dampak data makroekonomi ke pasar."
        elif any(kw in q for kw in ["berita", "news", "sentimen", "headline"]):
            intent_instruction = "Fokus pada berita terkini dan sentimen pasar yang relevan."
        elif any(kw in q for kw in ["korelasi", "hubungan", "dampak", "pengaruh"]):
            intent_instruction = "Fokus pada hubungan/korelasi antar instrumen yang ditanyakan."
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

        return f"""ROLE:
Anda adalah analis pasar keuangan senior (forex, gold, makroekonomi) yang menjawab trader retail Indonesia. Target pembaca sibuk — utamakan angka, tren, dan implikasi.

Waktu saat ini: {current_time}

=== DATA PASAR & MAKRO TERKINI (GUNAKAN SEBAGAI REFERENSI) ===
{context}
=== AKHIR DATA ==={history_section}

PERTANYAAN USER:
"{question}"

ALUR BERPIKIR (internal):
1. Pahami intent pertanyaan.
2. Ambil data relevan dari konteks; jangan mengarang angka.
3. Susun jawaban: inti jawaban dulu (BLUF) → penjelasan singkat → kesimpulan.

INSTRUKSI PENTING:
- Jawab HANYA pertanyaan di atas, jangan membahas topik lain.
- Gunakan data di atas hanya jika relevan dengan pertanyaan.
- Jika pertanyaan tidak berkaitan dengan data tersedia, jawab berdasarkan pengetahuan umum.
- {intent_instruction}
- JANGAN mengarang harga, tanggal, atau jadwal rilis data ekonomi yang tidak ada di data.
- Maksimal 350 kata. Gunakan Bahasa Indonesia yang santai tapi profesional.
- JANGAN gunakan simbol markdown (*, **, _, #) — gunakan emoji, angka, dan baris baru.
- Akhiri dengan disclaimer bahwa ini analisis edukasi, bukan rekomendasi trading.

JAWABAN:"""

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

        elif data.startswith("chart_"):
            symbol = data.replace("chart_", "") + "=X"
            await self._send_chart(query, symbol, update.effective_chat.id, context)

        elif data == "calendar":
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            try:
                # Kalender ekonomi BULAN INI, hanya event high impact
                events = await self.macro.get_economic_calendar_month()
                calendar_text = self.macro.format_calendar_text(events, max_events=15, only_high=True)
                message = f"{calendar_text}\n{DISCLAIMER}"
                await safe_edit_message_text(
                    query,
                    message,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Calendar callback error: {e}")
                await safe_edit_message_text(
                    query,
                    "❌ Gagal memuat kalender ekonomi. Silakan coba lagi nanti.",
                )

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
