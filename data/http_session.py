"""
Shared HTTP sessions — hemat handshake TCP/TLS & koneksi keep-alive.

Sebelumnya setiap panggilan API membuat session baru (koneksi TCP + TLS
handshake dari nol per request). Modul ini menyediakan session BERSAMA:

- get_aiohttp_session(): SATU ClientSession per event loop untuk seluruh
  pipeline async bot (aiohttp dirancang untuk banyak request bersamaan dalam
  satu session — koneksi keep-alive ke host yang sama di-reuse). Session
  otomatis dibuat ulang bila tertutup atau event loop berubah.
- get_requests_session(): satu Session per-thread (thread-safe untuk panggilan
  dari thread pool / background thread, mirip pola database.py).

Catatan: aiohttp ClientSession TIDAK boleh dipakai dengan `async with`
di sini — context manager itu menutup session saat keluar blok. Caller
cukup memanggil get_aiohttp_session() dan memakai session.get/post.
"""
import threading
from typing import Any

import aiohttp
import asyncio
import requests

# ── aiohttp (async) ──────────────────────────────────────────────
_AIOHTTP_SESSION: Any = None
_AIOHTTP_LOOP: Any = None
_AIOHTTP_LOCK = threading.Lock()


def get_aiohttp_session() -> aiohttp.ClientSession:
    """ClientSession bersama untuk event loop berjalan (lazy, thread-safe).

    aiohttp mengikat session ke event loop saat dibuat. Guard di bawah
    memastikan session SELALU dibuat & dipakai di loop yang sama — bila
    loop berubah (restart / test dengan asyncio.run baru), session dibuat
    ulang otomatis, bukan memakai session milik loop yang sudah mati.
    """
    global _AIOHTTP_SESSION, _AIOHTTP_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - dipanggil di luar async context
        loop = None
    if (
        _AIOHTTP_SESSION is None
        or _AIOHTTP_SESSION.closed
        or (loop is not None and _AIOHTTP_LOOP is not None and _AIOHTTP_LOOP is not loop)
    ):
        with _AIOHTTP_LOCK:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover
                loop = None
            if (
                _AIOHTTP_SESSION is None
                or _AIOHTTP_SESSION.closed
                or (loop is not None and _AIOHTTP_LOOP is not None and _AIOHTTP_LOOP is not loop)
            ):
                _AIOHTTP_SESSION = aiohttp.ClientSession()
                _AIOHTTP_LOOP = loop
    return _AIOHTTP_SESSION


# ── requests (sync) ──────────────────────────────────────────────
_requests_local = threading.local()


def get_requests_session() -> requests.Session:
    """Session requests khusus thread — aman dipakai konkuren.

    Tiap thread (event loop, thread pool, background thread) mendapat
    session sendiri, jadi tidak ada race pada koneksi pool.
    """
    s = getattr(_requests_local, "session", None)
    if s is None:
        s = requests.Session()
        _requests_local.session = s
    return s
