"""
Health Endpoint - HTTP /health untuk uptime monitoring & Docker healthcheck.

Kenapa server terpisah (bukan route di webhook Telegram)?
- python-telegram-bot 20.x memakai tornado internal dan TIDAK menyediakan hook
  untuk route HTTP custom. Menambah route di sana tidak mungkin tanpa patch.
- Solusi: server aiohttp kecil (dependency sudah ada) di port terpisah
  HEALTH_PORT (default 8090, jangan sama dengan PORT webhook), berjalan di
  daemon thread — tidak mengganggu event loop Telegram sama sekali.

Penggunaan:
- HEALTH_ENDPOINT_ENABLED=true (default) → GET http://127.0.0.1:8090/health
- Docker healthcheck / uptime monitor (BetterStack, Uptime Kuma) tinggal
  menunjuk endpoint ini.

CATATAN deploy: Railway/Render hanya mengekspos PORT utama secara publik.
/health di port terpisah terutama untuk healthcheck DOCKER (internal network)
dan pemeriksaan lokal. Untuk HTTP probe publik, gabungkan dengan platform
monitoring (mis. healthcheck Railway yang memakai PORT utama).
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from aiohttp import web

from config.settings import (
    HEALTH_PORT,
    HEALTH_BIND,
    HEALTH_ENDPOINT_ENABLED,
    SUPABASE_URL,
    SUPABASE_KEY,
)

logger = logging.getLogger(__name__)

_START_TIME = time.time()

# Registry event loop per thread — dipakai stop_health_server() untuk
# menghentikan loop dengan aman dari luar (test / shutdown bersih).
_LOOPS: Dict[int, asyncio.AbstractEventLoop] = {}


def build_health_payload() -> dict:
    """
    Susun payload JSON /health (murni — mudah di-test tanpa server).

    Semua section dibungkus defensif: satu subsistem error tidak boleh
    membuat endpoint mengembalikan 500.
    """
    uptime = int(time.time() - _START_TIME)
    payload: dict = {
        "status": "ok",
        "service": "market-ai-bot",
        "uptime_seconds": uptime,
        "uptime": f"{uptime // 3600}h {(uptime % 3600) // 60}m",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Cache (L1 memori)
    try:
        from data.cache import cache

        payload["cache"] = cache.get_stats()
    except Exception as e:
        payload["cache"] = {"error": str(e)}

    # Database Supabase
    try:
        payload["database"] = {"supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY)}
    except Exception as e:
        payload["database"] = {"error": str(e)}

    # Ringkasan analisis AI (dari metrics singleton — tanpa instance engine)
    try:
        from analysis.monitoring import metrics

        report = metrics.get_report()
        payload["ai"] = {
            "total_analyses": report["total_analyses"],
            "cache_hits": report["total_cache_hits"],
            "error_rate": report["error_rate"],
            "avg_duration_ms": report["avg_duration_ms"],
        }
    except Exception:
        payload["ai"] = {}

    return payload


async def _health_handler(request: web.Request) -> web.Response:
    """Handler GET /health — selalu 200 selama proses hidup."""
    return web.json_response(build_health_payload(), status=200)


def _run_server(port: int, ready: Optional[threading.Event] = None) -> None:
    """Jalankan aiohttp server di event loop sendiri (daemon thread)."""
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app, access_log=None)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _LOOPS[threading.get_ident()] = loop
    try:
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, HEALTH_BIND, port)
        loop.run_until_complete(site.start())
        logger.info(f"Health endpoint aktif di http://{HEALTH_BIND}:{port}/health")
        if ready is not None:
            ready.set()
        loop.run_forever()
    except Exception as e:
        logger.error(f"Health server gagal start di port {port}: {e}")
        if ready is not None:
            ready.set()
    finally:
        _LOOPS.pop(threading.get_ident(), None)
        try:
            loop.run_until_complete(runner.cleanup())
        except Exception:
            pass
        loop.close()


def start_health_server(
    port: int = HEALTH_PORT,
    enabled: bool = HEALTH_ENDPOINT_ENABLED,
):
    """
    Mulai health server di daemon thread.

    Returns:
        Thread yang berjalan, atau None bila dinonaktifkan / gagal bind.
    """
    if not enabled:
        logger.info("Health endpoint dinonaktifkan (HEALTH_ENDPOINT_ENABLED=false)")
        return None
    ready = threading.Event()
    thread = threading.Thread(
        target=_run_server,
        args=(port, ready),
        daemon=True,
        name="health-server",
    )
    thread.start()
    ready.wait(timeout=10)
    if not thread.is_alive():
        logger.error(f"Health server mati setelah start (port {port} mungkin terpakai)")
        return None
    return thread


def stop_health_server(thread: threading.Thread) -> None:
    """
    Hentikan health server secara bersih (untuk test / shutdown).
    No-op bila thread bukan health server / belum pernah di-start.
    """
    loop = _LOOPS.get(thread.ident)
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
    try:
        thread.join(timeout=5)
    except RuntimeError:
        # Thread belum pernah di-start (ident None) — tidak ada yang di-join.
        pass
