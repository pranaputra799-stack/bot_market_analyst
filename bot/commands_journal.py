from telegram.ext import (
    ContextTypes,
)
from typing import Dict
from telegram import (
    Update,
)
from data.database import db
from bot.messages import (
    format_price,
)
import logging

from bot.handlers_utils import (
    safe_reply_text,
)

logger = logging.getLogger(__name__)

class JournalCommandsMixin:
    """Trading journal — /journal add/close/list/stats/del."""

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
