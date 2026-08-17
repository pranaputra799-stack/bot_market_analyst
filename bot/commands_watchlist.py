"""Perintah /watchlist — daftar pair/instrumen favorit per user (personalisasi).

Watchlist disimpan di Supabase (tabel `watchlists`) dan dipakai untuk:
- /watchlist            → lihat daftar + harga terkini
- /watchlist add X      → tambah instrumen
- /watchlist remove X   → hapus instrumen
- /watchlist clear      → kosongkan daftar
- /map watchlist        → heatmap khusus pair watchlist (lihat commands_market)
- /morning              → brief pagi fokus ke watchlist user

Tanpa Supabase: method DB mengembalikan []/False — bot menampilkan pesan jelas
(bukan diam).
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from analysis.indicators import compute_indicators
from bot.handlers_utils import (
    label_to_symbol,
    safe_reply_text,
)
from data.database import db

logger = logging.getLogger(__name__)


class WatchlistCommandsMixin:
    """Command watchlist personal — tambah/lihat/hapus pair favorit user."""

    WATCHLIST_MAX = 10  # batas instrumen per user (jaga prompt & biaya)

    WATCHLIST_USAGE = (
        "👁️ *WATCHLIST PERSONAL*\n\n"
        "Simpan instrumen favoritmu — dipakai untuk morning brief yang lebih "
        "fokus dan `/map watchlist`.\n\n"
        "`/watchlist` — lihat daftar + harga terkini\n"
        "`/watchlist add XAU/USD` — tambah instrumen\n"
        "`/watchlist remove XAU/USD` — hapus instrumen\n"
        "`/watchlist clear` — kosongkan daftar\n\n"
        "Contoh: `/watchlist add gold`, `/watchlist add eurusd`, "
        "`/watchlist add XAU/USD`."
    )

    async def _get_user_watchlist(self, user_id: int) -> list:
        """Daftar simbol watchlist user (best-effort — [] bila gagal)."""
        try:
            return await db.get_watchlist_async(user_id)
        except Exception as e:
            logger.warning(f"Watchlist load gagal untuk {user_id}: {e}")
            return []

    async def _build_watchlist_menu(self, update: Update) -> tuple:
        """Pesan + keyboard submenu watchlist (dipakai callback settings).

        Menampilkan daftar watchlist + tombol hapus per instrumen, petunjuk
        tambah via perintah, dan tombol kembali ke Pengaturan / Menu Utama.
        """
        user_id = update.effective_user.id
        watchlist = await self._get_user_watchlist(user_id)

        if watchlist:
            lines = [
                "👁️ *KELOLA WATCHLIST*\n",
                f"📋 {len(watchlist)}/{self.WATCHLIST_MAX} instrumen:\n",
            ]
            for s in watchlist:
                lines.append(f"• {s}")
            lines.append("")
        else:
            lines = [
                "👁️ *KELOLA WATCHLIST*\n",
                "📋 Kosong — belum ada instrumen favorit.\n",
            ]
        lines.append(
            "➕ Tambah: `/watchlist add <simbol>`\n"
            "Contoh: `/watchlist add gold`, `/watchlist add eurusd`\n"
            "🗺️ Heatmap: `/map watchlist` | 🌅 Brief fokus: `/morning`"
        )

        keyboard = []
        for s in watchlist:
            keyboard.append([
                InlineKeyboardButton(f"🗑️ {s}", callback_data=f"wl_rm:{s}")
            ])
        keyboard.append([
            InlineKeyboardButton("🔙 Kembali ke Pengaturan", callback_data="settings"),
        ])
        keyboard.append([
            InlineKeyboardButton("🏠 Menu Utama", callback_data="menu"),
        ])
        return "\n".join(lines), InlineKeyboardMarkup(keyboard)

    async def watchlist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /watchlist [add|remove|clear] [simbol]."""
        text = update.message.text or ""
        body = text.replace("/watchlist", "", 1).strip()
        parts = body.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        arg = (parts[1] if len(parts) > 1 else "").strip()
        user_id = update.effective_user.id

        if action in ("help", "bantuan", "?"):
            await safe_reply_text(update.message, self.WATCHLIST_USAGE, parse_mode="Markdown")
            return

        if action in ("add", "tambah", "+"):
            await self._watchlist_add(update, user_id, arg)
            return
        if action in ("remove", "rm", "del", "hapus", "-"):
            await self._watchlist_remove(update, user_id, arg)
            return
        if action in ("clear", "reset", "kosongkan"):
            await self._watchlist_clear(update, user_id)
            return

        # Tanpa aksi → tampilkan daftar + harga terkini
        await self._watchlist_show(update, user_id)

    async def _watchlist_add(self, update: Update, user_id: int, arg: str):
        if not arg:
            await safe_reply_text(
                update.message,
                "Format: `/watchlist add <simbol>` — contoh: `/watchlist add gold` "
                "atau `/watchlist add EUR/USD`.",
                parse_mode="Markdown",
            )
            return
        detected, dname = self._resolve_symbol_from_text(arg)
        if not detected:
            await safe_reply_text(
                update.message,
                f"❌ Simbol *{arg}* tidak dikenali. Contoh: `gold`, `eurusd`, "
                "`XAU/USD`, `gbpusd`.",
                parse_mode="Markdown",
            )
            return
        watchlist = await self._get_user_watchlist(user_id)
        if dname in watchlist:
            await safe_reply_text(
                update.message,
                f"ℹ️ *{dname}* sudah ada di watchlistmu.",
                parse_mode="Markdown",
            )
            return
        if len(watchlist) >= self.WATCHLIST_MAX:
            await safe_reply_text(
                update.message,
                f"⚠️ Watchlist penuh (maks {self.WATCHLIST_MAX}). Hapus dulu: "
                "`/watchlist remove <simbol>`.",
                parse_mode="Markdown",
            )
            return
        ok = await db.add_watchlist_symbol_async(user_id, dname)
        if ok:
            await safe_reply_text(
                update.message,
                f"✅ *{dname}* ditambahkan ke watchlist.\n\n"
                f"Ketuk `/morning` untuk brief pagi yang fokus ke watchlistmu, "
                f"atau `/map watchlist` untuk heatmap khusus.",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(
                update.message,
                "❌ Gagal menyimpan watchlist. Database (Supabase) mungkin belum "
                "dikonfigurasi — pastikan `migrations/supabase.sql` sudah dijalankan.",
                parse_mode="Markdown",
            )

    async def _watchlist_remove(self, update: Update, user_id: int, arg: str):
        if not arg:
            await safe_reply_text(
                update.message,
                "Format: `/watchlist remove <simbol>` — contoh: `/watchlist remove gold`.",
                parse_mode="Markdown",
            )
            return
        watchlist = await self._get_user_watchlist(user_id)
        # Cocokkan: resolve dulu, fallback substring pada label tersimpan
        target = None
        _detected, dname = self._resolve_symbol_from_text(arg)
        if dname and dname in watchlist:
            target = dname
        else:
            low = arg.lower()
            target = next((s for s in watchlist if low in s.lower()), None)
        if not target:
            await safe_reply_text(
                update.message,
                f"ℹ️ *{arg}* tidak ada di watchlistmu. Lihat daftar: `/watchlist`.",
                parse_mode="Markdown",
            )
            return
        ok = await db.remove_watchlist_symbol_async(user_id, target)
        if ok:
            await safe_reply_text(
                update.message,
                f"🗑️ *{target}* dihapus dari watchlist.",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(
                update.message,
                "❌ Gagal menghapus dari watchlist (database belum dikonfigurasi?).",
                parse_mode="Markdown",
            )

    async def _watchlist_clear(self, update: Update, user_id: int):
        ok = await db.clear_watchlist_async(user_id)
        if ok:
            await safe_reply_text(
                update.message,
                "🗑️ Watchlist dikosongkan.",
                parse_mode="Markdown",
            )
        else:
            await safe_reply_text(
                update.message,
                "❌ Gagal mengosongkan watchlist (database belum dikonfigurasi?).",
                parse_mode="Markdown",
            )

    async def _watchlist_show(self, update: Update, user_id: int):
        """Tampilkan daftar watchlist + harga terkini (tanpa AI)."""
        watchlist = await self._get_user_watchlist(user_id)
        if not watchlist:
            await safe_reply_text(
                update.message,
                "👁️ *WATCHLIST*\n\n"
                "Watchlistmu masih kosong. Tambahkan instrumen favorit:\n"
                "`/watchlist add gold` — Gold (XAU/USD)\n"
                "`/watchlist add eurusd` — EUR/USD\n"
                "`/watchlist add gbpusd` — GBP/USD\n\n"
                "Setelah itu `/morning` jadi fokus ke watchlistmu, dan "
                "`/map watchlist` menampilkan heatmap khusus.",
                parse_mode="Markdown",
            )
            return

        await update.effective_chat.send_chat_action(action="typing")

        def _row(label: str) -> str:
            try:
                symbol = label_to_symbol(label)
                if symbol is None:
                    _detected, _d = self._resolve_symbol_from_text(label)
                    symbol = _detected
                if not symbol:
                    return f"{label:<18} ❌ simbol tidak dikenali"
                ohlcv = self.market.get_ohlcv_history(symbol, period="1mo", interval="1d", limit=30)
                ind = compute_indicators(ohlcv)
                return self._format_map_row(label, ind)
            except Exception:
                return self._format_map_row(label, {})

        rows = await asyncio.gather(
            *(asyncio.to_thread(_row, label) for label in watchlist)
        )
        header = (
            f"👁️ *WATCHLIST* ({len(watchlist)}/{self.WATCHLIST_MAX})\n\n"
        )
        body = "```\n" + "\n".join(rows) + "\n```"
        footer = (
            "\n\n➕ Tambah: `/watchlist add <simbol>`\n"
            "🗑️ Hapus: `/watchlist remove <simbol>`\n"
            "🗺️ Heatmap: `/map watchlist`\n"
            "🌅 Brief fokus: `/morning`"
        )
        await safe_reply_text(
            update.message,
            header + body + footer,
            parse_mode="Markdown",
        )
