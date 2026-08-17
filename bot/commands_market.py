from bot.messages import (
    DISCLAIMER,
    format_price,
    ALERT_ON_MESSAGE,
    ALERT_OFF_MESSAGE,
)
from utils.chart_generator import ChartGenerator
from telegram.ext import (
    ContextTypes,
)
from typing import Dict, Optional, Tuple
from telegram import (
    Update,
    InlineKeyboardMarkup,
)
from config.settings import (
    MORNING_BRIEF_TIMEZONE,
)
from config.providers import YAHOO_SYMBOLS, OANDA_SYMBOLS
import asyncio
from utils.risk_calculator import calculate_position_size, format_risk_result
from analysis.indicators import compute_indicators, format_key_levels
from datetime import datetime
from data.database import db
import logging

from bot.handlers_utils import (
    label_to_symbol,
    safe_reply_text,
    strip_markdown_asterisks,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)

class MarketCommandsMixin:
    """Command analisis pasar — sentiment, kalender, overview, morning brief, risk, pivot, map."""

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
    async def _persist_alert_subscribers(self, context) -> None:
        """Simpan daftar subscriber event saat ini ke Supabase (best-effort).

        Kegagalan di-log WARNING (bukan debug): persist yang gagal = langganan
        alert hilang saat bot restart (Render free tier spin-down) → user harus
        mengaktifkan ulang. Silent-fail di sini pernah jadi support issue.
        """
        try:
            subscribers = context.bot_data.get("event_alert_subscribers", set())
            ok = await db.save_event_alert_subscribers_async(subscribers)
            if not ok:
                logger.warning(
                    "Persist event subscribers GAGAL (Supabase tidak tersedia/"
                    "gagal) — alert user akan hilang saat restart. Cek "
                    "SUPABASE_URL (harus https://...) & tabel event_alert_subscribers."
                )
        except Exception as e:
            logger.warning(f"Persist event subscribers gagal: {e}")
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
        """Handler untuk perintah /morning - Morning Brief harian.

        Personalisasi: bila user punya watchlist, brief difokuskan ke instrumen
        favoritnya (tetap 1 panggilan AI — data global disajikan sebagai konteks).
        """
        if not await self._check_command_rate_limit(update, context):
            return
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        # Watchlist user → fokus brief (best-effort; [] = brief global)
        try:
            watchlist = await db.get_watchlist_async(update.effective_user.id)
        except Exception as e:
            logger.debug(f"Watchlist load untuk /morning gagal: {e}")
            watchlist = []

        try:
            brief = await self._generate_morning_brief(watchlist=watchlist)
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
    async def _fetch_map_rows(self, instruments) -> list:
        """Fetch satu baris heatmap per instrumen (label, yahoo_symbol) secara paralel.

        Dipakai /map (daftar default) dan /map watchlist (daftar user).
        """
        def _fetch(label: str, key: str) -> str:
            try:
                yahoo = YAHOO_SYMBOLS.get(key) or key
                if not yahoo:
                    return MarketCommandsMixin._format_map_row(label, {})
                ohlcv = self.market.get_ohlcv_history(yahoo, period="1mo", interval="1d", limit=30)
                return MarketCommandsMixin._format_map_row(label, compute_indicators(ohlcv))
            except Exception:
                return MarketCommandsMixin._format_map_row(label, {})

        return await asyncio.gather(
            *(asyncio.to_thread(_fetch, label, key) for label, key in instruments)
        )

    async def map_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /map [watchlist] — heatmap instan instrumen (tanpa AI).

        /map          → semua instrumen utama (default)
        /map watchlist → hanya pair di watchlist user (reuse logika yang sama)
        """
        if not await self._check_command_rate_limit(update, context):
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        text = update.message.text or ""
        arg = text.replace("/map", "", 1).strip().lower()

        if arg == "watchlist":
            # Varian watchlist: resolve tiap label tersimpan → (label, yahoo)
            try:
                labels = await db.get_watchlist_async(update.effective_user.id)
            except Exception as e:
                logger.debug(f"Watchlist load untuk /map gagal: {e}")
                labels = []
            if not labels:
                await safe_reply_text(
                    update.message,
                    "👁️ Watchlistmu masih kosong. Tambahkan dulu: `/watchlist add gold`\n\n"
                    "Atau ketik `/map` untuk heatmap semua instrumen.",
                    parse_mode="Markdown",
                )
                return
            instruments = []
            for label in labels:
                yahoo = label_to_symbol(label)
                if yahoo is None:
                    _detected, _d = self._resolve_symbol_from_text(label)
                    yahoo = _detected
                instruments.append((label, yahoo or ""))
            title = "🗺️ *HEATMAP WATCHLIST*"
        else:
            instruments = self.MAP_INSTRUMENTS
            title = "🗺️ *MARKET HEATMAP*"

        rows = await self._fetch_map_rows(instruments)
        now_str = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%A, %d %B %Y %H:%M")
        msg = (
            f"{title}\n"
            f"🕐 {now_str} WIB\n\n"
            f"```\n" + "\n".join(rows) + "\n```\n\n"
            "RSI >70 overbought • <30 oversold • 5d = perubahan 5 hari.\n"
            "⚠️ Edukasi — bukan saran trading."
        )
        await safe_reply_text(update.message, msg, parse_mode="Markdown")
