"""Perintah /plan — rencana trading mingguan personal (fitur AI).

- /plan setup — isi/update profil lewat alur TANYA-JAWAB (ConversationHandler
  di bot/conversation_plan.py). Bentuk sekali isi tetap didukung:
  `/plan setup 1000 2 swing XAU/USD,EUR/USD 09:00-16:00`.
- /plan       — generate rencana mingguan (1 panggilan AI, cache 1 minggu)
- /plan clear — hapus profil
- /plan help  — bantuan

Generator ada di analysis/trading_plan.py (reuse engine AI + sumber data pasar
+ logika ukuran posisi /risk). Hasil di-cache per minggu di bot_data agar user
yang spam /plan tidak membakar kuota AI.
"""
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from analysis.trading_plan import (
    format_profile_line,
    generate_trading_plan,
    validate_profile_input,
)
from bot.handlers_utils import safe_reply_text
from data.database import db

logger = logging.getLogger(__name__)


class PlanCommandsMixin:
    """Command trading plan personal — setup profil + generate rencana mingguan."""

    PLAN_USAGE = (
        "📋 *TRADING PLAN MINGGUAN*\n\n"
        "Bot menyusun rencana trading PERSONAL (pair layak, entry/SL/TP, ukuran "
        "posisi dari modal & risiko) berdasarkan profilmu.\n\n"
        "Langkah:\n"
        "1️⃣ Isi profil: `/plan setup` — dijawab bertahap (modal → risiko → gaya "
        "→ pair → jam).\n"
        "   Sekali isi (opsional): `/plan setup 1000 2 swing XAU/USD,EUR/USD 09:00-16:00`\n"
        "2️⃣ Generate: `/plan` — rencana minggu ini (1x AI call, di-cache 1 minggu)\n\n"
        "Lainnya:\n"
        "`/plan clear` — hapus profil\n"
        "`/plan help` — bantuan ini"
    )

    def _plan_cache(self, context: ContextTypes.DEFAULT_TYPE) -> dict:
        """Cache rencana per minggu di bot_data: {user_id: [week_start, text]}."""
        return context.bot_data.setdefault("plan_cache", {})

    @staticmethod
    def _week_start_iso() -> str:
        now = datetime.now(timezone.utc)
        return (now - timedelta(days=now.weekday())).date().isoformat()

    async def plan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /plan [setup|clear|help] — delegasi ke entry ConversationHandler.

        Registrasi resmi memakai ConversationHandler (bot/conversation_plan.py)
        via plan_conversation_entry; method ini dipertahankan agar pemanggilan
        langsung (test / integrasi) tetap berfungsi.
        """
        return await self.plan_conversation_entry(update, context)

    async def plan_conversation_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Entry point ConversationHandler /plan.

        - 'setup' tanpa argumen → mulai percakapan (return ASK_MODAL).
        - 'setup <args>' → sekali isi (backward-compat, langsung selesai).
        - 'help' / 'clear' → selesai langsung.
        - tanpa argumen → generate rencana (selesai langsung).
        """
        from bot.conversation_plan import ASK_MODAL, ConversationHandler

        text = update.message.text or ""
        body = text.replace("/plan", "", 1).strip()
        parts = body.split()
        action = parts[0].lower() if parts else ""
        user_id = update.effective_user.id

        if action == "setup":
            if len(parts) > 1:
                # Bentuk sekali isi tetap didukung
                await self._plan_setup(update, user_id, parts[1:])
                return ConversationHandler.END
            await safe_reply_text(
                update.message,
                "📋 *SETUP PROFIL TRADING PLAN* (1/5)\n\n"
                "Langkah 1 — Berapa *modal* kamu (USD)?\n"
                "Contoh: `1000`\n\n"
                "Ketik /cancel untuk membatalkan.",
                parse_mode="Markdown",
            )
            return ASK_MODAL
        if action in ("help", "bantuan", "?"):
            await safe_reply_text(update.message, self.PLAN_USAGE, parse_mode="Markdown")
            return ConversationHandler.END
        if action in ("clear", "hapus", "reset"):
            ok = await db.delete_user_profile_async(user_id)
            await safe_reply_text(
                update.message,
                "🗑️ Profil trading plan dihapus." if ok
                else "❌ Gagal menghapus profil (database belum dikonfigurasi?).",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        # Generate rencana mingguan
        await self._plan_generate(update, context, user_id)
        return ConversationHandler.END

    async def _plan_setup(self, update: Update, user_id: int, args: list):
        if not args:
            await safe_reply_text(
                update.message,
                "Format: `/plan setup <modal> <risk%> <gaya> <pair1,pair2> [jam]`\n\n"
                "Contoh:\n"
                "`/plan setup 1000 2 swing XAU/USD,EUR/USD 09:00-16:00`\n"
                "`/plan setup 500 1 day_trade EUR/USD`\n\n"
                "Gaya: `scalping`, `day_trade`, `swing`.",
                parse_mode="Markdown",
            )
            return
        profile = validate_profile_input(list(args))
        if "error" in profile:
            await safe_reply_text(update.message, f"❌ {profile['error']}\n\n{self.PLAN_USAGE}", parse_mode="Markdown")
            return
        ok = await db.upsert_user_profile_async(user_id, profile)
        if ok:
            await safe_reply_text(
                update.message,
                "✅ *Profil trading plan tersimpan!*\n\n"
                f"👤 {format_profile_line(profile)}\n\n"
                "Sekarang ketik `/plan` untuk generate rencana mingguanmu.",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(
                update.message,
                "❌ Gagal menyimpan profil. Database (Supabase) mungkin belum "
                "dikonfigurasi — pastikan `migrations/supabase.sql` sudah dijalankan.",
                parse_mode="Markdown",
            )

    async def _plan_generate(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
        # Rate limit + kuota harian AI (sama seperti command analisis lain)
        if not await self._check_command_rate_limit(update, context):
            return
        profile = await db.get_user_profile_async(user_id)
        if not profile:
            await safe_reply_text(
                update.message,
                "ℹ️ Profil belum diisi. Ketik `/plan setup` dulu — contoh:\n"
                "`/plan setup 1000 2 swing XAU/USD,EUR/USD 09:00-16:00`",
                parse_mode="Markdown",
            )
            return

        # Cache mingguan: hasil yang sama (minggu ini) tidak membakar AI lagi
        week = self._week_start_iso()
        cache = self._plan_cache(context)
        cached = cache.get(user_id)
        if cached and cached[0] == week:
            await safe_reply_text(update.message, cached[1], parse_mode="Markdown")
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        result = await generate_trading_plan(self.ai, self.market, self.macro, self.news, profile)
        if "error" in result:
            await safe_reply_text(
                update.message,
                f"❌ {result['error']}\n\nCoba lagi beberapa saat — kalau berulang, cek `/status`.",
                parse_mode="Markdown",
            )
            return
        cache[user_id] = [week, result["text"]]
        await safe_reply_text(update.message, result["text"], parse_mode="Markdown")
