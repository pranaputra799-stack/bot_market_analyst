from bot.messages import (
    WELCOME_MESSAGE,
    HELP_MESSAGE,
    DISCLAIMER,
    ALERT_ON_MESSAGE,
)
from utils.chart_generator import ChartGenerator
from telegram.ext import (
    ContextTypes,
)
from telegram import (
    Update,
)
import asyncio
from data.conversation_memory import get_context, clear
from data.cot import resolve_instrument
from data.database import db
import logging

from bot.handlers_utils import (
    _main_menu_inline_keyboard,
    label_to_symbol,
    safe_edit_message_text,
    strip_markdown_asterisks,
)

logger = logging.getLogger(__name__)

class CallbackFlowMixin:
    """Alur callback — handle_callback (tombol inline & keyboard menu)."""

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
            # Personalisasi: brief fokus ke watchlist user (best-effort)
            try:
                watchlist = await db.get_watchlist_async(query.from_user.id)
            except Exception as e:
                logger.debug(f"Watchlist load untuk brief (callback) gagal: {e}")
                watchlist = []
            try:
                brief = await self._generate_morning_brief(watchlist=watchlist)
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

        elif data == "settings_watchlist":
            # Buka submenu kelola watchlist (lihat + tombol hapus per instrumen)
            message, kb = await self._build_watchlist_menu(update)
            await safe_edit_message_text(
                query,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )

        elif data.startswith("wl_rm:"):
            # Hapus satu instrumen dari watchlist (tombol di submenu settings)
            symbol = data.split(":", 1)[1]
            user_id = query.from_user.id
            ok = await db.remove_watchlist_symbol_async(user_id, symbol)
            feedback = (
                f"🗑️ *{symbol}* dihapus dari watchlist."
                if ok else
                "❌ Gagal menghapus (database belum dikonfigurasi?)."
            )
            message, kb = await self._build_watchlist_menu(update)
            await safe_edit_message_text(
                query,
                f"{feedback}\n\n{message}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=kb,
            )

        elif data.startswith("cot:"):
            # Quick action instrumen COT (tombol di pesan /cot): tampilkan
            # laporan untuk instrumen itu — logika identik dengan /cot <simbol>.
            alias = data.split(":", 1)[1]
            config = resolve_instrument(alias)
            if not config:
                await safe_edit_message_text(
                    query,
                    f"❌ Instrumen *{alias}* tidak dikenali di COT.",
                    parse_mode="Markdown",
                )
                return
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing",
            )
            try:
                message = await self._cot_report_text(config)
            except Exception as e:
                logger.warning(f"COT quick action {alias} gagal: {e}")
                message = (
                    f"❌ Gagal memuat laporan COT untuk *{config['display']}*. "
                    f"Coba lagi beberapa saat."
                )
            # Keyboard quick action tetap tampil agar bisa pindah instrumen
            # tanpa mengetik /cot lagi (callback tidak punya argumen teks).
            await safe_edit_message_text(
                query,
                message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=self._cot_quick_keyboard(),
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
