"""Notifikasi otomatis ke admin (ADMIN_USER_IDS) — anti silent-fail.

Sebelumnya kegagalan start/setup hanya tercatat di log (stdout) yang jarang
dibuka di panel Render. Modul ini mengirim pesan Telegram langsung ke admin
saat peristiwa penting:

- Bot online (setelah deploy / restart berhasil)
- Setup webhook gagal (semua retry habis) — bot idle
- Semua provider AI down / pulih (rate-limited, 1x/jam)

Semua fungsi best-effort: kegagalan kirim hanya di-log, TIDAK pernah raise
agar notifikasi tidak menggagalkan alur utama bot.
"""
import logging

from config.settings import ADMIN_USER_IDS

logger = logging.getLogger(__name__)


async def notify_admins(bot, text: str, parse_mode: str = "Markdown") -> int:
    """Kirim pesan ke semua ADMIN_USER_IDS (best-effort).

    Args:
        bot: Telegram Bot (application.bot) yang sudah ter-init.
        text: Isi pesan (bisa memakai Markdown Telegram).
        parse_mode: Parse mode pesan (default "Markdown").

    Returns:
        Jumlah admin yang berhasil menerima pesan.
    """
    sent = 0
    for chat_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
            sent += 1
        except Exception as e:
            logger.warning(f"Gagal kirim alert admin ke {chat_id}: {e}")
    return sent


def _admin_configured() -> bool:
    """True bila ada minimal satu admin yang di-set (ADMIN_USER_IDS)."""
    return bool(ADMIN_USER_IDS)
