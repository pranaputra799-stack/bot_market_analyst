"""ConversationHandler untuk /plan setup — alur tanya-jawab profil trading.

Menggantikan argumen satu baris dengan percakapan bertahap:
    modal → risiko % → gaya trading (tombol inline) → pair favorit → jam (opsional).

- Profil parsial disimpan di context.user_data["plan_setup_profile"].
- /cancel (atau /batal) membatalkan kapan saja tanpa menyimpan.
- Registrasi: main.py menaruh ConversationHandler ini SEBELUM handler pesan
  umum, sehingga teks user saat percakapan aktif diarahkan ke sini.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from analysis.trading_plan import format_profile_line, validate_profile_input
from bot.handlers_utils import safe_reply_text
from data.database import db

logger = logging.getLogger(__name__)

# State percakapan (urutan alur)
(ASK_MODAL, ASK_RISK, ASK_STYLE, ASK_PAIRS, ASK_HOURS) = range(5)

# Kunci profil parsial di context.user_data
_SETUP_KEY = "plan_setup_profile"

_CANCEL_HINT = "\n\nKetik /cancel untuk membatalkan."

_STYLE_OPTIONS = [
    ("⚡ Scalping", "scalping"),
    ("📈 Day Trade", "day_trade"),
    ("🌓 Swing", "swing"),
]


def _style_keyboard() -> InlineKeyboardMarkup:
    """Tombol pilihan gaya trading (callback plan_style:<value>)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"plan_style:{value}")]
        for label, value in _STYLE_OPTIONS
    ])


# ===================== STATE HANDLERS =====================

async def _ask_modal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State 1/5 — terima modal (angka USD)."""
    text = (update.message.text or "").strip().replace(",", "").replace(" ", "")
    try:
        balance = float(text)
    except ValueError:
        await safe_reply_text(
            update.message,
            f"❌ Modal harus angka (USD), contoh: 1000. Coba lagi.{_CANCEL_HINT}",
        )
        return ASK_MODAL
    if balance <= 0:
        await safe_reply_text(
            update.message,
            f"❌ Modal harus lebih besar dari 0. Coba lagi.{_CANCEL_HINT}",
        )
        return ASK_MODAL
    context.user_data[_SETUP_KEY] = {"balance": balance}
    await safe_reply_text(
        update.message,
        f"💰 Modal: *${balance:,.0f}*.\n\n"
        f"Langkah 2/5 — Berapa % risiko per trade? (contoh: 2){_CANCEL_HINT}",
        parse_mode="Markdown",
    )
    return ASK_RISK


async def _ask_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State 2/5 — terima risiko % per trade (0 < x ≤ 100)."""
    text = (update.message.text or "").strip().replace(",", "").replace("%", "")
    try:
        risk = float(text)
    except ValueError:
        await safe_reply_text(
            update.message,
            f"❌ Risiko harus angka persen (contoh: 2). Coba lagi.{_CANCEL_HINT}",
        )
        return ASK_RISK
    if not (0 < risk <= 100):
        await safe_reply_text(
            update.message,
            f"❌ Risiko harus 0 < x ≤ 100 (contoh: 2). Coba lagi.{_CANCEL_HINT}",
        )
        return ASK_RISK
    data = context.user_data.get(_SETUP_KEY) or {}
    data["risk_per_trade"] = risk
    context.user_data[_SETUP_KEY] = data
    await safe_reply_text(
        update.message,
        f"⚠️ Risiko: {risk:g}%/trade.\n\nLangkah 3/5 — Pilih gaya trading:",
        reply_markup=_style_keyboard(),
    )
    return ASK_STYLE


async def _ask_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State 3/5 — terima pilihan gaya via tombol inline."""
    query = update.callback_query
    await query.answer()
    style = query.data.split(":", 1)[1]
    data = context.user_data.get(_SETUP_KEY) or {}
    data["trading_style"] = style
    context.user_data[_SETUP_KEY] = data
    await safe_reply_text(
        query.message,
        f"📈 Gaya: *{style}*.\n\n"
        f"Langkah 4/5 — Pair favorit? Pisahkan koma (contoh: XAU/USD,EUR/USD){_CANCEL_HINT}",
        parse_mode="Markdown",
    )
    return ASK_PAIRS


async def _ask_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State 4/5 — terima daftar pair favorit (dipisah koma)."""
    pairs_raw = (update.message.text or "").strip()
    if not pairs_raw:
        await safe_reply_text(
            update.message,
            f"❌ Minimal 1 pair (contoh: XAU/USD). Coba lagi.{_CANCEL_HINT}",
        )
        return ASK_PAIRS
    data = context.user_data.get(_SETUP_KEY) or {}
    data["favorite_pairs"] = pairs_raw
    context.user_data[_SETUP_KEY] = data
    skip_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Lewati", callback_data="plan_hours_skip")]
    ])
    await safe_reply_text(
        update.message,
        f"📌 Pair: {pairs_raw.upper()}.\n\n"
        f"Langkah 5/5 — Jam trading? (opsional, contoh: 09:00-16:00 WIB)",
        reply_markup=skip_kb,
    )
    return ASK_HOURS


async def _ask_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State 5/5 — terima jam trading (teks) lalu simpan."""
    return await _finish_setup(update, context, (update.message.text or "").strip())


async def _skip_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State 5/5 — tombol 'Lewati' (jam kosong) lalu simpan."""
    query = update.callback_query
    await query.answer()
    return await _finish_setup(update, context, "")


async def _finish_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, hours: str):
    """Gabungkan profil parsial → validasi akhir → simpan ke Supabase → END."""
    user_id = update.effective_user.id
    data = context.user_data.get(_SETUP_KEY) or {}
    parts = [
        str(data.get("balance") or ""),
        str(data.get("risk_per_trade") or ""),
        data.get("trading_style") or "",
        data.get("favorite_pairs") or "",
    ]
    if hours:
        parts.append(hours)
    profile = validate_profile_input(parts)
    if "error" in profile:
        # Defensif: input sudah divalidasi per-langkah, jadi jarang terjadi.
        context.user_data.pop(_SETUP_KEY, None)
        await safe_reply_text(
            update.effective_message,
            f"❌ {profile['error']}\n\nSilakan mulai lagi: /plan setup",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    ok = await db.upsert_user_profile_async(user_id, profile)
    if ok:
        await safe_reply_text(
            update.effective_message,
            f"✅ *Profil trading plan tersimpan!*\n\n"
            f"👤 {format_profile_line(profile)}\n\n"
            f"Sekarang ketik /plan untuk generate rencana mingguanmu.",
            parse_mode="Markdown",
        )
    else:
        await safe_reply_text(
            update.effective_message,
            "❌ Gagal menyimpan profil (database belum dikonfigurasi?).",
            parse_mode="Markdown",
        )
    context.user_data.pop(_SETUP_KEY, None)
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Batalkan percakapan — profil tidak diubah."""
    context.user_data.pop(_SETUP_KEY, None)
    await safe_reply_text(
        update.message,
        "🚫 Setup dibatalkan — profil tidak diubah. Ketik /plan setup untuk mulai lagi.",
    )
    return ConversationHandler.END


# ===================== BUILDER =====================

def build_plan_setup_conversation(entry) -> ConversationHandler:
    """Bangun ConversationHandler untuk /plan.

    Args:
        entry: async callable (update, context) → state/END — entry point
            /plan dari bot (PlanCommandsMixin.plan_conversation_entry): membuka
            percakapan untuk 'setup', atau menangani /plan tanpa argumen
            (generate) / help / clear secara langsung.

    Registrasi: tambahkan ke Application SEBELUM handler pesan umum agar teks
    saat percakapan aktif diarahkan ke sini (PTB memilih handler pertama yang
    cocok dalam satu group).
    """
    return ConversationHandler(
        entry_points=[CommandHandler("plan", entry)],
        states={
            ASK_MODAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ask_modal)],
            ASK_RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ask_risk)],
            ASK_STYLE: [CallbackQueryHandler(_ask_style, pattern=r"^plan_style:")],
            ASK_PAIRS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ask_pairs)],
            ASK_HOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _ask_hours),
                CallbackQueryHandler(_skip_hours, pattern=r"^plan_hours_skip$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel),
            CommandHandler("batal", _cancel),
        ],
        name="plan_setup",
    )
