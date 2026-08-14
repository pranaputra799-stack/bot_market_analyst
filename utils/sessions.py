"""Sesi market forex utama — dipakai notifikasi "sesi buka".

Sesi relevan untuk trader Indonesia (UTC+7, tanpa DST):
- Sydney 🇦🇺  buka 22:00 UTC (05:00 WIB)
- Tokyo  🇯🇵  buka 00:00 UTC (07:00 WIB)
- London 🇬🇧  buka 08:00 UTC (15:00 WIB)
- New York 🇺🇸 buka 13:30 UTC (20:30 WIB) — waktu NY dibulatkan ke jam penuh
  agar penjadwalan tetap sederhana (selisih 30 menit tidak signifikan untuk
  notifikasi edukasi).

Semua murni & deterministik — mudah di-test tanpa network.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List


@dataclass(frozen=True)
class MarketSession:
    key: str          # kunci internal (untuk dedup)
    name: str         # nama tampilan
    emoji: str
    utc_open_hour: int  # jam UTC saat sesi dibuka
    utc_close_hour: int  # jam UTC perkiraan penutupan (informasi saja)


SESSIONS: List[MarketSession] = [
    MarketSession("SYDNEY", "Sydney", "🇦🇺", 22, 7),
    MarketSession("TOKYO", "Tokyo", "🇯🇵", 0, 9),
    MarketSession("LONDON", "London", "🇬🇧", 8, 17),
    MarketSession("NEW_YORK", "New York", "🇺🇸", 13, 22),
]


def sessions_just_opened(now_utc: datetime, window_minutes: int = 30) -> List[MarketSession]:
    """Sesi yang BARU SAJA buka dalam window_minutes terakhir.

    Sebuah sesi dianggap "baru buka" bila now berada di [open, open + window).
    Menangani sesi lintas tengah malam (Sydney buka 22:00 UTC) dengan
    membandingkan terhadap jam buka HARI INI.
    """
    out: List[MarketSession] = []
    for s in SESSIONS:
        open_time = now_utc.replace(
            hour=s.utc_open_hour, minute=0, second=0, microsecond=0
        )
        if open_time <= now_utc < open_time + timedelta(minutes=window_minutes):
            out.append(s)
    return out


def format_session_text(session: MarketSession, tz_name: str = "Asia/Jakarta") -> str:
    """Teks notifikasi sesi buka (jam buka dikonversi ke zona waktu user)."""
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
        open_local = datetime.now(tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Konversi jam buka UTC → lokal dengan bantuan delta sederhana.
        # (UTC+7 WIB: buka 22:00 UTC = 05:00 WIB hari berikutnya)
        delta_hours = int(tz.utcoffset(datetime.now(timezone.utc)).total_seconds() // 3600)
        local_hour = (session.utc_open_hour + delta_hours) % 24
        open_local = open_local.replace(hour=local_hour)
    except Exception:
        open_local = None

    line = f"🔔 *SESI {session.name.upper()} BUKA* {session.emoji}\n\n"
    line += (
        "Volatilitas biasanya meningkat saat sesi ini aktif — "
        "pantau harga & berita ekonomi.\n\n"
    )
    if open_local is not None:
        line += f"🕐 Buka pukul {open_local.strftime('%H:%M')} ({tz_name})\n"
    line += (
        f"⏱ Buka  {session.utc_open_hour:02d}:00 UTC\n"
        f"🔚 Tutup ±{session.utc_close_hour:02d}:00 UTC\n\n"
        "⚠️ Edukasi — bukan saran trading."
    )
    return line
