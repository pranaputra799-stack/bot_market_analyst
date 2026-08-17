from bot.messages import (
    ERROR_MESSAGE,
    RATE_LIMIT_MESSAGE,
    DISCLAIMER,
    format_price,
    ALERT_ON_MESSAGE,
)
from utils.chart_generator import ChartGenerator
from telegram.ext import (
    ContextTypes,
)
from typing import Dict, Optional
from config.settings import (
    MORNING_BRIEF_TIMEZONE,
    ENABLE_MULTI_AGENT,
    USER_DAILY_QUOTA,
)
from config.providers import YAHOO_SYMBOLS, FRED_INDICATORS
from telegram import (
    Update,
)
from data.conversation_memory import format_history, add_exchange, get_context
import asyncio
from analysis.fact_check import build_fact_check_note, strip_fact_check_note
from analysis.indicators import compute_indicators, format_key_levels, format_indicators_for_prompt
from datetime import datetime
from prompts.loader import format_prompt
from utils.validators import sanitize_text
import time
import logging

from bot.handlers_utils import (
    MENU_KEYBOARD_ACTIONS,
    _edit_progress_message,
    _quick_action_keyboard,
    _strip_provider_prefix,
    detect_fast_price_query,
    label_to_symbol,
    safe_reply_text,
    strip_markdown_asterisks,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)

class MessageFlowMixin:
    """Alur pesan — handle_message, deteksi pair, fast price, penyusunan prompt."""

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

        # ===== COT CONTEXT (posisi institusional CFTC) =====
        # Bila user bertanya soal COT/posisi institusional, kumpulkan data COT
        # (cache mingguan, download fallback) dan suntikkan ke pipeline AI agar
        # analisis didasarkan data sungguhan — bukan pengetahuan umum model.
        cot_context = ""
        if self._is_cot_question(user_question):
            try:
                cot_context = await self._get_cot_context_for_question(user_question)
                if cot_context:
                    logger.info(f"COT context disuntikkan untuk user {user_id}")
            except Exception as e:
                logger.debug(f"COT context gagal dimuat: {e}")

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
                    extra_context=cot_context,
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
                if cot_context:
                    data_context = (
                        f"{data_context}\n\n"
                        f"📊 *DATA COT (POSISI INSTITUSIONAL CFTC):*\n{cot_context}"
                    )
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
