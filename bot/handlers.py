"""
Telegram Bot Handlers - Semua handler untuk perintah dan pesan dari user.
Menggunakan python-telegram-bot v20.x.
Now with multi-agent analysis system from MarketLens.
"""
import asyncio
import logging
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
from utils.chart_generator import ChartGenerator
from analysis.director import AnalysisDirector
from analysis.monitoring import metrics
from bot.messages import (
    WELCOME_MESSAGE,
    HELP_MESSAGE,
    ABOUT_MESSAGE,
    STATUS_MESSAGE_TEMPLATE,
    ERROR_MESSAGE,
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
            for chunk in split_long_text(text):
                result = await _reply_chunk(message, chunk, parse_mode, kwargs)
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


def _strip_provider_prefix(text: str) -> str:
    """
    Hapus prefix '[via Provider] 🤖' dari response AI (ditambahkan engine
    untuk request tanpa system_override) agar konten analisis bersih.
    """
    if "[via" in text:
        parts = text.split("\n\n", 1)
        return parts[1] if len(parts) > 1 else text
    return text


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
        
        # Simpan/update user ke database
        db.upsert_user(user.id, user.username, user.first_name)

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
                InlineKeyboardButton("📅 Kalender Ekonomi", callback_data="calendar"),
                InlineKeyboardButton("❓ Bantuan", callback_data="help"),
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

        ai_status = get_ai_providers_status(self.ai)
        data_status = get_data_sources_status(self.market, self.macro, self.news)
        cache_stats = cache.get_stats()
        analysis_status = get_analysis_engine_status()

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
        
        if db.is_subscribed(chat_id):
            await safe_reply_text(update.message, "✅ Anda sudah berlangganan Morning Brief.")
            return
            
        if db.add_subscriber(chat_id):
            await safe_reply_text(update.message, "🎉 Berhasil! Anda sekarang akan menerima Morning Brief setiap pagi.")
        else:
            await safe_reply_text(update.message, "❌ Gagal mendaftar langganan. Database mungkin belum dikonfigurasi.")

    async def unsubscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler untuk perintah /unsubscribe - Berhenti berlangganan Morning Brief."""
        chat_id = update.effective_chat.id
        
        if not db.is_subscribed(chat_id):
            await safe_reply_text(update.message, "⚠️ Anda memang belum berlangganan Morning Brief.")
            return
            
        if db.remove_subscriber(chat_id):
            await safe_reply_text(update.message, "👋 Berhasil berhenti langganan Morning Brief.")
        else:
            await safe_reply_text(update.message, "❌ Gagal membatalkan langganan. Database mungkin belum dikonfigurasi.")

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
            self.market.get_yahoo_data, symbol, period=period, interval=interval
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

        # Generate candlestick chart, fallback ke line chart
        chart_url = self.chart.build_candlestick_chart(ohlcv, symbol)
        if not chart_url:
            # Fallback: line chart sederhana
            prices = [d.get("close", 0) for d in ohlcv]
            labels = [d.get("date", "")[-5:] for d in ohlcv]
            chart_url = self.chart.build_line_chart(prices, labels, symbol)

        # Buat caption
        arrow = "🟢" if change and change > 0 else "🔴" if change and change < 0 else "⚪"
        change_str = f"{change:+.2f}%" if change is not None else ""
        caption = (
            f"📈 *{display_name}*\n"
            f"{arrow} Harga: *{format_price(current_price, symbol)}* {change_str}\n"
            f"📊 Periode: {period} ({interval})\n"
        )

        # Kirim via URL (Telegram akan download sendiri)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=chart_url,
            caption=caption,
            parse_mode="Markdown",
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
            events = await self.macro.get_economic_calendar()
            calendar_text = self.macro.format_calendar_text(events, max_events=10)

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
        market_summary, macro_summary, calendar_events, news_summary = await asyncio.gather(
            asyncio.to_thread(self.market.get_market_summary),
            asyncio.to_thread(self.macro.get_macro_summary),
            self.macro.get_economic_calendar(),
            self.news.get_news_summary("FOREX"),
        )

        # Format kalender ekonomi untuk morning brief (top 3 high impact)
        calendar_text = self.macro.format_calendar_text(calendar_events, max_events=3)

        # AI-powered outlook & catalysts using multi-agent analysis
        if self.analysis_director:
            try:
                # Gunakan multi-agent untuk analisis yang lebih dalam
                analysis_prompt = (
                    f"Hari ini tanggal {today}. Berdasarkan data pasar berikut, buatlah "
                    f"OUTLOOK dan KATALIS UTAMA untuk hari ini dalam 3-4 kalimat per bagian.\n\n"
                    f"PENTING: Jangan mengarang event ekonomi. Hanya sebutkan event yang benar-benar ada "
                    f"di KALENDER EKONOMI di bawah. Jika tidak ada event terjadwal, tulis 'Tidak ada rilis data besar hari ini'.\n\n"
                    f"DATA PASAR:\n{market_summary}\n\n"
                    f"DATA MAKRO:\n{macro_summary}\n\n"
                    f"KALENDER EKONOMI:\n{calendar_text}\n\n"
                    f"BERITA:\n{news_summary}\n\n"
                    f"Format jawaban:\n"
                    f"OUTLOOK:\n[outlook singkat untuk EUR/USD, Gold, dan DXY hari ini]\n\n"
                    f"KATALIS UTAMA:\n[3-4 katalis utama yang perlu diwaspadai hari ini]"
                )

                result = await self.analysis_director.analyze(analysis_prompt)

                # Extract from analysis result (bersihkan prefix [via ...])
                ai_content = _strip_provider_prefix(result.final_response or "")

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
                    outlook=outlook_part,
                    catalysts=catalysts_part,
                )
            except Exception as e:
                logger.warning(f"Multi-agent morning brief failed: {e}, falling back to legacy")

        # Fallback: legacy single-prompt method
        outlook_prompt = (
            f"Hari ini tanggal {today}. Berdasarkan data pasar berikut, buatlah "
            f"OUTLOOK dan KATALIS UTAMA untuk hari ini dalam 3-4 kalimat per bagian.\n\n"
            f"PENTING: Jangan mengarang event ekonomi. Hanya sebutkan event yang benar-benar ada "
            f"di KALENDER EKONOMI di bawah. Jika tidak ada event terjadwal, tulis 'Tidak ada rilis data besar hari ini'.\n\n"
            f"DATA PASAR:\n{market_summary}\n\n"
            f"DATA MAKRO:\n{macro_summary}\n\n"
            f"KALENDER EKONOMI:\n{calendar_text}\n\n"
            f"BERITA:\n{news_summary}\n\n"
            f"Format jawaban:\n"
            f"OUTLOOK:\n[outlook singkat untuk EUR/USD, Gold, dan DXY hari ini]\n\n"
            f"KATALIS UTAMA:\n[3-4 katalis utama yang perlu diwaspadai hari ini, beri perhatian ekstra pada rilis data di KALENDER EKONOMI]"
        )

        ai_response = self.ai.generate(outlook_prompt, use_cache=True, max_tokens=4096)

        # Parse AI response (bersihkan prefix [via ...])
        ai_content = _strip_provider_prefix(ai_response)

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
            outlook=outlook,
            catalysts=catalysts,
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

        logger.info(f"Question from user {user_id}: {user_question[:100]}")

        # Rate limiting
        user_data = context.user_data
        last_msg_time = user_data.get("last_message_time", 0)
        if time.time() - last_msg_time < 2:
            await update.message.reply_text(
                "⏳ Mohon tunggu sebentar sebelum mengirim pertanyaan berikutnya...",
            )
            return

        user_data["last_message_time"] = time.time()
        self.total_questions += 1

        # Typing indicator
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action="typing",
        )

        try:
            if self.analysis_director and ENABLE_MULTI_AGENT:
                # ===== NEW: Multi-Agent Analysis Pipeline =====
                logger.info(f"Using multi-agent analysis for: {user_question[:80]}...")

                # Get OHLCV data if relevant for signal analysis
                ohlcv_data = None
                detected_pairs = self._detect_pairs(user_question)
                if detected_pairs:
                    pair_symbol = detected_pairs[0][1]
                    market_data = await asyncio.to_thread(
                        self.market.get_yahoo_data, pair_symbol, period="1mo", interval="1d"
                    )
                    if "error" not in market_data:
                        ohlcv_data = market_data.get("ohlcv", [])

                # Run multi-agent analysis
                result = await self.analysis_director.analyze(
                    question=user_question,
                    market_data_ohlcv=ohlcv_data,
                )

                final_message = result.final_response

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
                prompt = self._build_prompt(user_question, data_context)

                answer = await asyncio.to_thread(
                    self.ai.generate,
                    prompt,
                    max_retries=3,
                    use_cache=True,
                )

                final_message = f"{answer}{DISCLAIMER}"

            # Send response
            await safe_reply_text(
                update.message,
                final_message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        except asyncio.TimeoutError:
            await safe_reply_text(
                update.message,
                "⏰ Maaf, permintaan timeout. Silakan coba lagi dengan pertanyaan yang lebih spesifik.",
            )
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            await safe_reply_text(
                update.message,
                f"{ERROR_MESSAGE}\n\nDetail teknis: {str(e)[:100]}",
                parse_mode="Markdown",
            )

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
            parts.append(f"📈 High 5d: {ohlcv[-1].get('high', 'N/A')}")
            parts.append(f"📉 Low 5d: {ohlcv[-1].get('low', 'N/A')}")

        if data.get("high_52w"):
            parts.append(f"🏆 High 52w: {data['high_52w']}")
        if data.get("low_52w"):
            parts.append(f"🔻 Low 52w: {data['low_52w']}")

        return "\n".join(parts)

    def _build_prompt(self, question: str, context: str) -> str:
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

        return f"""Waktu saat ini: {current_time}

=== DATA PASAR & MAKRO TERKINI (GUNAKAN SEBAGAI REFERENSI) ===
{context}
=== AKHIR DATA ===

PERTANYAAN USER:
"{question}"

INSTRUKSI PENTING:
- Jawab HANYA pertanyaan di atas, jangan membahas topik lain.
- Gunakan data di atas hanya jika relevan dengan pertanyaan.
- Jika pertanyaan tidak berkaitan dengan data tersedia, jawab berdasarkan pengetahuan umum.
- {intent_instruction}
- Struktur jawaban: langsung ke inti jawaban → penjelasan singkat → kesimpulan.
- Maksimal 350 kata. Gunakan Bahasa Indonesia yang santai tapi profesional.
- Gunakan emoji secukupnya agar mudah dibaca.
- Akhiri dengan disclaimer bahwa ini analisis edukasi.

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
                events = await self.macro.get_economic_calendar()
                calendar_text = self.macro.format_calendar_text(events, max_events=10)
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
            
        db_chat_ids = db.get_all_subscribers()
        
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
