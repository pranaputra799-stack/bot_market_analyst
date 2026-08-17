"""Perintah /cot — laporan Commitments of Traders (CFTC).

Data posisi institusional (non-commercial = speculative / commercial = hedger)
di pasar futures AS. Sumber: arsip tahunan CFTC (gratis, tanpa API key), di-cache
di Supabase 7 hari per instrumen (CFTC rilis 1x/minggu). Interpretasi AI opsional
(1 panggilan per instrumen per laporan — di-cache bersama data).
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers_utils import (
    safe_reply_text,
    strip_markdown_asterisks,
)
from data.cot import (
    cot_data_from_json,
    cot_data_to_json,
    extract_market,
    fetch_tff_rows,
    fetch_year_rows,
    format_cot_message,
    format_cot_summary,
    resolve_instrument,
)
import re
from config.settings import ADMIN_USER_IDS
from data.database import db
from prompts.loader import format_prompt

logger = logging.getLogger(__name__)


class CotCommandsMixin:
    """Command COT — posisi institusional dari CFTC (on-demand + cache 7 hari)."""

    COT_USAGE = (
        "📊 *COT REPORT (CFTC)*\n\n"
        "Posisi institusional (non-commercial/speculative & commercial/hedger) "
        "di pasar futures AS — dirilis CFTC gratis setiap Jumat.\n\n"
        "`/cot gold` — Gold futures (XAU/USD)\n"
        "`/cot eur` — Euro FX\n"
        "`/cot gbp` — British Pound\n"
        "`/cot jpy` — Japanese Yen\n"
        "`/cot oil` — WTI Crude Oil\n"
        "`/cot btc` — Bitcoin futures\n"
        "`/cot dxy` — US Dollar Index\n"
        "`/cot us2y` — 2-Year T-Note\n"
        "`/cot sofr` — SOFR 3M\n"
        "`/cot fed funds` — Fed Funds\n"
        "`/cot sp400` — S&P 400 Midcap\n"
        "`/cot dow` — DJIA (E-mini Dow)\n"
        "`/cot russell` — Russell 2000 E-mini\n\n"
        "⚠️ COT adalah data futures (bukan spot forex) — pair tanpa kontrak "
        "futures AS akan ditolak dengan disclaimer."
    )

    # Instrumen populer untuk tombol quick action di pesan /cot (callback `cot:<alias>`)
    COT_QUICK_ACTIONS = [
        ("🥇 Gold", "gold"),
        ("💶 Euro", "eur"),
        ("🛢 Oil", "oil"),
        ("🪙 Bitcoin", "btc"),
        ("💵 DXY", "dxy"),
        ("🏛 10Y Note", "us10y"),
        ("💱 S&P 500", "sp500"),
        ("🌾 Corn", "corn"),
        ("🔥 SOFR", "sofr"),
        ("😨 VIX", "vix"),
    ]

    def _cot_quick_keyboard(self) -> InlineKeyboardMarkup:
        """Tombol quick action instrumen COT populer (callback `cot:<alias>`)."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f"cot:{alias}")
             for label, alias in self.COT_QUICK_ACTIONS[i:i + 2]]
            for i in range(0, len(self.COT_QUICK_ACTIONS), 2)
        ])

    def _cot_cache_key(self, config: dict) -> str:
        """Kunci cache Supabase per instrumen (TTL 7 hari di DB)."""
        return "cot:" + "_".join(config.get("keywords") or [])

    async def _load_cot_data(self, config: dict):
        """Muat data COT satu instrumen: cache Supabase (7 hari) → download CFTC → cache.

        Returns:
            Dict data COT (atau None bila instrumen tidak ditemukan di laporan).
        """
        cache_key = self._cot_cache_key(config)

        # 1) Cek cache Supabase (7 hari) — data + interpretasi AI
        data = None
        try:
            cached = await db.get_cot_cache_async(cache_key)
            if cached and cached.get("data"):
                data = cot_data_from_json(cached["data"])
        except Exception as e:
            logger.debug(f"COT cache load gagal: {e}")

        # 2) Cache kosong/expired → download arsip CFTC + parse + extract market
        if not data:
            rows = await asyncio.to_thread(fetch_year_rows)
            data = extract_market(rows, config) if rows else None
            # Market financial (USD INDEX, treasury) tidak ada di laporan legacy
            # — ambil dari laporan Traders in Financial Futures (TFF).
            if not data and config.get("report") == "tff":
                tff_rows = await asyncio.to_thread(fetch_tff_rows)
                data = extract_market(tff_rows, config) if tff_rows else None
            if data:
                # Simpan ke cache 7 hari (data diformat JSON-safe)
                try:
                    await db.set_cot_cache_async(cache_key, cot_data_to_json(data))
                except Exception as e:
                    logger.debug(f"COT cache save gagal: {e}")
        return data

    async def _cot_report_text(self, config: dict) -> str:
        """Bangun teks laporan COT untuk satu instrumen (fetch → cache → AI).

        Dipakai bersama oleh /cot dan tombol quick action (callback `cot:`),
        sehingga perilakunya identik. Selalu mengembalikan teks Markdown;
        diawali pesan error bila data tidak tersedia.
        """
        data = await self._load_cot_data(config)
        if not data:
            return (
                f"ℹ️ *{config['display']}* tidak tersedia di laporan COT "
                f"terbaru.\n\n"
                f"⚠️ COT adalah data *futures* AS, bukan spot forex — "
                f"instrumen tanpa kontrak futures AS tidak punya laporan. "
                f"Coba: `/cot gold`, `/cot eur`, `/cot oil`."
            )

        cache_key = self._cot_cache_key(config)

        # 3) Interpretasi AI (1x per laporan — ikut di-cache)
        ai_text = data.get("ai_interpretation")
        if not ai_text:
            ai_text = await self._cot_ai_interpretation(data)
            if ai_text:
                data["ai_interpretation"] = ai_text
                try:
                    await db.set_cot_cache_async(cache_key, cot_data_to_json(data))
                except Exception as e:
                    logger.debug(f"COT cache AI save gagal: {e}")

        message = format_cot_message(data)
        if ai_text:
            message += f"\n\n🧠 *Interpretasi AI:*\n{ai_text}\n"
        return message

    async def _get_cot_context_text(self, instruments: list = None, max_instruments: int = 5) -> str:
        """Ringkasan COT ringkas untuk konteks AI (morning brief / chat).

        Baca dari cache Supabase (7 hari, diisi pre-warm mingguan) — download
        CFTC hanya sebagai fallback saat cache kosong. Selalu aman: gagal → "".

        Args:
            instruments: Daftar alias instrumen (mis. ["gold", "eur", "dxy"]);
                None/kosong = set default penting.
            max_instruments: Batas jumlah instrumen yang diproses.
        """
        aliases = instruments or ["gold", "eur", "dxy", "oil", "sp500", "btc"]
        configs = []
        for alias in aliases:
            cfg = resolve_instrument(alias)
            if cfg and cfg not in configs:
                configs.append(cfg)
            if len(configs) >= max_instruments:
                break

        sections = []
        for cfg in configs:
            try:
                data = await self._load_cot_data(cfg)
                if data:
                    sections.append(format_cot_summary(data))
            except Exception as e:
                logger.debug(f"COT context {cfg.get('display')} gagal: {e}")
        return "\n\n".join(sections) if sections else ""

    @staticmethod
    def _is_cot_question(text: str) -> bool:
        """Deteksi pertanyaan yang menyebut data COT / posisi institusional."""
        q = (text or "").lower()
        if any(kw in q for kw in (
            "cftc",
            "posisi institusional",
            "smart money",
            "commitments of traders",
            "hedger",
            "hedging",
            "institutional positioning",
            "laporan cot",
            "posisi trader",
            "posisi spekulatif",
        )):
            return True
        # Kata 'cot' utuh (bukan substring seperti cotton/coto) — user bisa
        # menulis "data cot", "cot gold", "bagaimana cot-nya?".
        return re.search(r"\bcot\b", q) is not None

    async def _get_cot_context_for_question(self, question: str, max_instruments: int = 4) -> str:
        """Konteks COT untuk satu pertanyaan: instrumen yang disebut + default penting."""
        aliases = ["gold", "eur", "dxy"]
        cfg = resolve_instrument(question)
        if cfg and cfg.get("keywords"):
            alias = cfg["keywords"][0]
            aliases = [alias] + [a for a in aliases if a != alias]
        return await self._get_cot_context_text(aliases, max_instruments=max_instruments)

    async def cot_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /cot [simbol]."""
        if not await self._check_command_rate_limit(update, context):
            return
        text = update.message.text or ""
        arg = text.replace("/cot", "", 1).strip()
        if not arg or arg.lower() in ("help", "bantuan"):
            await safe_reply_text(
                update.message,
                self.COT_USAGE,
                parse_mode="Markdown",
                reply_markup=self._cot_quick_keyboard(),
            )
            return

        config = resolve_instrument(arg)
        if not config:
            await safe_reply_text(
                update.message,
                f"❌ Instrumen *{arg}* tidak dikenali di COT.\n\n"
                f"Yang tersedia: gold, silver, eur, gbp, jpy, chf, cad, aud, "
                f"nzd, mxn, oil, brent, copper, btc, dxy, s&p500, nasdaq, dow, "
                f"nikkei, us2y, us5y, us10y, us30y, fed funds, sofr, sp400, "
                f"russell, vix, corn, wheat, soybean, natural gas.\n\n"
                f"Contoh: `/cot gold`, `/cot eur`.",
                parse_mode="Markdown",
            )
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        message = await self._cot_report_text(config)
        await safe_reply_text(update.message, message, parse_mode="Markdown")

    async def _cot_ai_interpretation(self, data: dict) -> str:
        """Interpretasi AI singkat laporan COT (best-effort — gagal → '')."""
        try:
            prompt = format_prompt("cot_interpretation", REPORT_TEXT=format_cot_message(data))
            ai_text = await self.ai.generate_async(prompt, use_cache=False, max_tokens=400)
            ai_text = strip_markdown_asterisks((ai_text or "").strip())
            if ai_text and "error" not in ai_text.lower():
                return ai_text
        except Exception as e:
            logger.warning(f"COT AI interpretation gagal: {e}")
        return ""

    async def cotrefresh_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /cotrefresh [jumlah] — pemicu manual pre-warm COT (khusus admin).

        Menjalankan logika yang sama dengan job terjadwal Jumat malam
        (SchedulerJobsMixin.prewarm_cot_cache): download arsip CFTC → isi cache
        Supabase semua instrumen (+ interpretasi AI untuk data baru). Berguna
        saat admin ingin memastikan /cot instan tanpa menunggu jadwal mingguan,
        atau setelah situs CFTC pulih dari gangguan.

        Argumen opsional: jumlah instrumen yang diproses (default semua).
        """
        user_id = update.effective_user.id
        if user_id not in ADMIN_USER_IDS:
            await safe_reply_text(
                update.message,
                "🔒 Perintah ini khusus admin bot.",
                parse_mode="Markdown",
            )
            return

        text = update.message.text or ""
        arg = text.replace("/cotrefresh", "", 1).strip()
        max_instruments = 0
        if arg:
            try:
                max_instruments = int(arg)
                if max_instruments < 0:
                    raise ValueError
            except ValueError:
                await safe_reply_text(
                    update.message,
                    "Format: `/cotrefresh [jumlah]` — contoh `/cotrefresh` (semua) "
                    "atau `/cotrefresh 10` (maks 10 instrumen).",
                    parse_mode="Markdown",
                )
                return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # prewarm_cot_cache butuh Application (bot + bot_data untuk statistik &)
        # notifikasi admin). Di handler perintah tidak ada Application — beri
        # adapter minimal yang cukup untuk method tersebut.
        adapter = type("COTPrewarmApp", (), {
            "bot": context.bot,
            "bot_data": context.bot_data,
        })()

        try:
            await self.prewarm_cot_cache(adapter, max_instruments=max_instruments)
        except Exception as e:
            logger.warning(f"COT pre-warm manual gagal: {e}")
            await safe_reply_text(
                update.message,
                "❌ Pre-warm COT gagal — cek log untuk detail.",
                parse_mode="Markdown",
            )
            return

        stats = context.bot_data.get("cot_prewarm_stats") or {}
        if stats.get("ok") == 0 and stats.get("skipped") == 0 and stats.get("failed", 0) > 0:
            await safe_reply_text(
                update.message,
                "❌ Pre-warm COT gagal total — semua arsip CFTC tidak bisa diunduh "
                "(cek koneksi/URL CFTC).",
                parse_mode="Markdown",
            )
            return
        await safe_reply_text(
            update.message,
            "✅ *Pre-warm COT selesai*\n\n"
            f"• 🆕 Cache baru: {stats.get('ok', 0)}\n"
            f"• ⏭ Sudah segar: {stats.get('skipped', 0)}\n"
            f"• ❌ Gagal: {stats.get('failed', 0)}\n\n"
            "`/cot` sekarang langsung instan untuk instrumen yang ter-cache.",
            parse_mode="Markdown",
        )
