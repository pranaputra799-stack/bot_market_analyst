"""Sinkronisasi menu perintah bot (set_my_commands) — satu sumber kebenaran.

Kenapa modul ini ada:
- Daftar command bisa berubah antar versi; menu di Telegram HANYA ter-update
  saat set_my_commands dipanggil. Kegagalan transient (mis. error 500 saat
  deploy) atau command yang pernah di-set via scope lain meninggalkan menu
  LAMA — command mati seperti /pa, /chart, /watch masih tampil saat user
  mengetik "/".
- Versi ini menangani dua masalah itu:
  1. Retry (3x, backoff) — kegagalan sesaat tidak membuat menu basi.
  2. Bersihkan SEMUA scope (default, group chat, chat admin) — command basi
     yang pernah di-set scope-spesifik ikut terhapus.

Dipakai oleh main.py (post_init) dan perintah admin /syncmenu.
"""
import asyncio
import logging

from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
)

logger = logging.getLogger(__name__)

# Satu-satunya sumber daftar perintah bot. Edit di SINI → menu & /help
# konsisten. (Command admin /broadcast, /stats, /syncmenu sengaja TIDAK
# dimasukkan — hanya tampil untuk admin via handler, bukan di menu umum.)
COMMANDS = [
    BotCommand("start", "🚀 Mulai bot"),
    BotCommand("help", "❓ Bantuan & panduan"),
    BotCommand("morning", "🌅 Morning Brief harian"),
    BotCommand("sentiment", "🧠 Sentimen pasar"),
    BotCommand("calendar", "📅 Kalender Ekonomi"),
    BotCommand("aftermath", "🎯 Analisis dampak event (contoh: /aftermath nfp)"),
    BotCommand("prediksi", "🎯 Win rate prediksi news (XAU/USD)"),
    BotCommand("alert", "🔔 Notifikasi event ekonomi"),
    BotCommand("status", "✅ Status sistem & API"),
    BotCommand("clear", "🧹 Bersihkan konteks"),
    BotCommand("memory", "🧠 Lihat & hapus riwayat percakapan"),
    BotCommand("settings", "⚙️ Pengaturan bot"),
    BotCommand("overview", "🌍 Overview pasar"),
    BotCommand("sentimen", "🧠 Sentimen retail (OANDA)"),
    BotCommand("subscribe", "🔔 Langganan Morning Brief"),
    BotCommand("unsubscribe", "🔕 Berhenti langganan"),
    BotCommand("risk", "📐 Kalkulator ukuran posisi"),
    BotCommand("pivot", "📐 Pivot point & level kunci"),
    BotCommand("map", "🗺️ Heatmap semua instrumen"),
    BotCommand("journal", "📓 Catatan transaksi (trading journal)"),
    BotCommand("watchlist", "👁️ Watchlist instrumen favorit"),
    BotCommand("plan", "📋 Rencana trading mingguan (personal)"),
    BotCommand("cot", "📊 Laporan COT (posisi institusional)"),
    BotCommand("about", "ℹ️ Tentang bot ini"),
]


async def set_bot_commands(bot, attempts: int = 3) -> bool:
    """Set menu perintah ke Telegram: retry + bersihkan semua scope.

    Args:
        bot: Telegram Bot (application.bot).
        attempts: Jumlah percobaan sebelum menyerah (default 3).

    Returns:
        True bila berhasil (atau semua scope beres), False bila gagal total.
    """
    for attempt in range(1, attempts + 1):
        try:
            # Default scope — daftar utama (menggantikan menu lama / BotFather)
            await bot.set_my_commands(COMMANDS)
            # Scope group & admin dikosongkan — command basi yang pernah
            # di-set scope-spesifik (mis. /pa di group) tidak bertahan.
            await bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())
            await bot.set_my_commands([], scope=BotCommandScopeAllChatAdministrators())
            logger.info(
                f"Menu perintah disinkronkan: {len(COMMANDS)} command "
                f"(default + scope group/admin dibersihkan)"
            )
            return True
        except Exception as e:
            logger.warning(
                f"set_my_commands attempt {attempt}/{attempts} gagal: {e}"
            )
            if attempt < attempts:
                await asyncio.sleep(2 ** attempt)
    return False
