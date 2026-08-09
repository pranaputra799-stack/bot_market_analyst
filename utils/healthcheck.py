"""
Docker Healthcheck - Cek kesehatan bot dari dalam container.

Dipakai Dockerfile HEALTHCHECK (berjalan DI DALAM container, sehingga bind
127.0.0.1 pada /health bisa diakses langsung):

    HEALTHCHECK CMD ["python", "utils/healthcheck.py"]

Logika (exit 0 = sehat, exit 1 = tidak sehat):
1. Coba GET http://<HEALTH_BIND>:<HEALTH_PORT>/health (default 127.0.0.1:8090).
   Endpoint ini dijalankan daemon thread oleh main.py (utils/health_server.py).
2. Bila endpoint dinonaktifkan (HEALTH_ENDPOINT_ENABLED=false) atau belum up
   (proses masih start), fallback ke cek proses utama: ada proses dengan
   "main.py" di command-line (/proc/<pid>/cmdline, Linux container).
3. Keduanya gagal → exit 1 (bot crash / hang total → platform restart).

Catatan: hanya pakai stdlib (urllib, os, glob) — tidak perlu curl/pgrep yang
tidak tersedia di python:3.11-slim.
"""

import glob
import os
import sys
import urllib.request

# Baca dari env — sama seperti config/settings.py, tapi script ini TIDAK
# mengimpor modul bot (agar tetap ringan & tidak bergantung dependency).
HEALTH_BIND = os.getenv("HEALTH_BIND", "127.0.0.1")
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8090"))
HEALTH_TIMEOUT = float(os.getenv("HEALTH_TIMEOUT", "5"))


def _check_host(bind: str) -> str:
    """Normalisasi host untuk koneksi keluar: 0.0.0.0/:: → 127.0.0.1."""
    if bind in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return bind


def health_ok(bind: str = HEALTH_BIND, port: int = HEALTH_PORT,
              timeout: float = HEALTH_TIMEOUT) -> bool:
    """True bila endpoint /health merespons HTTP 200."""
    host = _check_host(bind)
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def bot_process_alive(proc_root: str = "/proc") -> bool:
    """
    True bila ada proses dengan "main.py" di command-line.

    Di Linux container, /proc/<pid>/cmdline berisi argv proses. Digunakan
    sebagai fallback saat endpoint /health dinonaktifkan / belum up.
    """
    try:
        pattern = os.path.join(proc_root, "[0-9]*", "cmdline")
        for cmdline_path in glob.glob(pattern):
            try:
                with open(cmdline_path, "rb") as f:
                    raw = f.read().replace(b"\x00", b" ")
                if b"main.py" in raw:
                    return True
            except OSError:
                continue
    except Exception:
        pass
    return False


def check() -> bool:
    """Keseluruhan pemeriksaan: /health dulu, lalu fallback proses."""
    if health_ok():
        return True
    if bot_process_alive():
        return True
    return False


def main() -> int:
    # Output singkat untuk diagnosis di `docker inspect` / platform logs.
    if health_ok():
        print("healthcheck: health-ok")
        return 0
    if bot_process_alive():
        print("healthcheck: process-alive (fallback, endpoint /health off/belum up)")
        return 0
    print("healthcheck: unhealthy (endpoint /health down & proses main.py tidak ditemukan)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
