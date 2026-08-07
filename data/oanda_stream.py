"""
OANDA v20 Streaming Prices — harga REAL-TIME via WebSocket.

Menjaga harga bid/ask LIVE di memori untuk semua instrumen OANDA (lihat
OANDA_SYMBOLS). Thread daemon + reconnect otomatis (heartbeat timeout +
exponential backoff). Data layer memakai harga ini untuk menjawab
"harga sekarang" dengan harga streaming, bukan polling REST.

Dokumentasi: https://developer.oanda.com/rest-live-v20/pricing-stream-ep/
"""
import json
import logging
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlencode

from config.settings import OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENV
from config.providers import OANDA_SYMBOLS

logger = logging.getLogger(__name__)

_STREAM_BASE_URLS = {
    "practice": "wss://stream-fxpractice.oanda.com",
    "live": "wss://stream-fxtrade.oanda.com",
}
_STREAM_PATH = "/v3/accounts/{account_id}/pricing/stream"

# Reconnect backoff (detik)
_RECONNECT_BASE = 5
_RECONNECT_MAX = 60
# Tidak ada pesan (harga/heartbeat) selama ini -> koneksi dianggap putus
_HEARTBEAT_TIMEOUT = 15

# Instrumen INTI yang tersedia di hampir semua akun OANDA (forex mayor/cross +
# logam mulia). PENTING: endpoint streaming OANDA menolak SELURUH subscription
# bila satu instrumen tidak valid untuk akun (mis. sebagian akun demo tidak punya
# crypto/index/oil). Jadi percobaan pertama memakai daftar penuh OANDA_SYMBOLS;
# bila server mengembalikan error / sesi tutup tanpa harga sama sekali, stream
# otomatis turun ke daftar inti ini agar koneksi tetap hidup.
_CORE_STREAM_INSTRUMENTS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "NZD_USD", "USD_CAD",
    "EUR_GBP", "GBP_JPY", "EUR_JPY", "AUD_JPY", "EUR_AUD", "USD_MXN",
    "XAU_USD", "XAG_USD",
]


def parse_price_message(msg: dict) -> Optional[Dict]:
    """
    Parse satu pesan streaming OANDA menjadi harga bersih.

    Args:
        msg: Raw JSON message dari streaming (dict).

    Returns:
        {"instrument", "mid", "bid", "ask", "time"} untuk pesan PRICE,
        None untuk pesan lain (heartbeat dll) / data tidak lengkap.
    """
    if not isinstance(msg, dict) or msg.get("type") != "PRICE":
        return None
    instrument = msg.get("instrument")
    bids = msg.get("bids") or []
    asks = msg.get("asks") or []
    if not instrument or not bids or not asks:
        return None
    try:
        bid = float(bids[0]["price"])
        ask = float(asks[0]["price"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return {
        "instrument": instrument,
        "mid": round((bid + ask) / 2.0, 8),
        "bid": bid,
        "ask": ask,
        "time": msg.get("time", ""),
    }


class OandaPriceStream:
    """
    Streaming harga OANDA di thread daemon.

    - start(): idempotent, aman dipanggil kapan saja (no-op bila tidak
      dikonfigurasi / sudah berjalan).
    - get_price(instrument): harga live terakhir, atau None.
    - Reconnect otomatis: heartbeat timeout + backoff exponensial.
    """

    def __init__(self, api_key: str = "", account_id: str = "", env: str = ""):
        self.api_key = api_key or OANDA_API_KEY
        self.account_id = account_id or OANDA_ACCOUNT_ID
        self.env = (env or OANDA_ENV or "practice").lower()
        self._prices: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False
        # True = daftar penuh OANDA_SYMBOLS ditolak server → pakai inti saja
        self._use_core_only = False

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    # ===================== PUBLIC API =====================

    def start(self):
        """Mulai thread streaming (idempotent; no-op tanpa API key)."""
        if self._started or not self.is_configured:
            return
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="oanda-price-stream"
        )
        self._thread.start()
        logger.info("OANDA price stream started (daemon thread)")

    def stop(self):
        self._stop_event.set()
        self._started = False

    def get_price(self, instrument: str) -> Optional[Dict]:
        """Harga live terakhir untuk instrumen OANDA (mis. EUR_USD), atau None."""
        with self._lock:
            p = self._prices.get(instrument)
            return dict(p) if p else None

    def get_all_prices(self) -> Dict[str, Dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._prices.items()}

    @property
    def live_instruments(self) -> List[str]:
        """Instrumen yang di-subscribe: daftar penuh, atau inti bila ditolak server."""
        if self._use_core_only:
            return sorted(set(_CORE_STREAM_INSTRUMENTS))
        return sorted(set(OANDA_SYMBOLS.values()))

    @property
    def is_running(self) -> bool:
        return self._started and bool(self._thread and self._thread.is_alive())

    # ===================== INTERNAL =====================

    def _resolve_account_id(self) -> str:
        """Account ID dari env, atau auto-detect via REST (OandaClient)."""
        if self.account_id:
            return self.account_id
        try:
            from data.oanda_client import OandaClient

            return OandaClient().resolve_account_id()
        except Exception as e:
            logger.warning(f"OANDA account resolve untuk stream gagal: {e}")
            return ""

    def _run(self):
        backoff = _RECONNECT_BASE
        hint_logged = False
        while not self._stop_event.is_set():
            try:
                self._connect_and_listen()
                backoff = _RECONNECT_BASE  # sesi sukses -> reset backoff
            except Exception as e:
                logger.warning(f"OANDA stream session error: {e}")
                if not hint_logged:
                    hint_logged = True
                    logger.warning(
                        "Hint: bila error berulang 'timed out while waiting for handshake', "
                        "kemungkinan proxy sistem (HTTP(S)_PROXY) di host ini mengganggu WebSocket. "
                        "Kode kini memaksa koneksi langsung (proxy=None). "
                        "Pastikan juga host dapat menjangkau wss://stream-fxpractice.oanda.com:443."
                    )
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, _RECONNECT_MAX)

    def _connect_and_listen(self):
        """Satu sesi streaming: connect + baca pesan sampai putus/dihentikan."""
        try:
            from websockets.sync.client import connect  # websockets >= 12 (sync)
        except ImportError:
            logger.warning("Library 'websockets' tidak tersedia — streaming OANDA nonaktif")
            self._stop_event.wait(30)
            return

        account_id = self._resolve_account_id()
        if not account_id:
            logger.warning("OANDA account ID tidak ditemukan — streaming OANDA nonaktif")
            self._stop_event.wait(60)
            return

        instruments = self.live_instruments
        if not instruments:
            return
        base = _STREAM_BASE_URLS.get(self.env, _STREAM_BASE_URLS["practice"])
        query = urlencode({"instruments": ",".join(instruments)})
        url = f"{base}{_STREAM_PATH.format(account_id=account_id)}?{query}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        logger.info(f"Connecting OANDA stream ({self.env}) — {len(instruments)} instruments...")
        received_any = False
        with self._open_connection(connect, url, headers) as ws:
            while not self._stop_event.is_set():
                try:
                    raw = ws.recv(timeout=_HEARTBEAT_TIMEOUT)
                except TimeoutError:
                    logger.warning("OANDA stream heartbeat timeout — reconnect...")
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                # Error dari server (mis. daftar instrumen tidak valid untuk akun)
                # → turun ke daftar inti & reconnect.
                if isinstance(msg, dict) and msg.get("type") == "error":
                    logger.warning(
                        f"OANDA stream error: {msg.get('message', msg)} — turun ke instrumen inti"
                    )
                    self._use_core_only = True
                    break
                price = parse_price_message(msg)
                if price:
                    received_any = True
                    with self._lock:
                        self._prices[price["instrument"]] = price
                # pesan heartbeat/other: abaikan (koneksi tetap hidup)
        # Sesi berakhir tanpa SATU pun harga diterima → kemungkinan daftar penuh
        # ditolak server tanpa pesan error eksplisit; coba lagi dengan inti.
        if not received_any:
            self._use_core_only = True

    @staticmethod
    def _open_connection(connect, url: str, headers: Dict):
        """
        Buka koneksi WebSocket — kompatibel lintas versi websockets.

        PENTING (proxy=None): sejak websockets 14, deteksi proxy sistem AKTIF
        secara default. Di host cloud dengan env HTTP(S)_PROXY, koneksi wss bisa
        di-route lewat proxy yang tidak mendukung WebSocket CONNECT → request
        terkirim tapi jawaban handshake tidak pernah datang, lalu gagal dengan
        "timed out while waiting for handshake response". Memaksa koneksi
        langsung (proxy=None) menghindari masalah ini.

        open_timeout 20 dtk: toleransi handshake di jaringan lambat (sebelumnya
        10 dtk terlalu ketat).
        """
        try:
            return connect(
                url,
                additional_headers=headers,
                proxy=None,
                open_timeout=20,
                close_timeout=5,
            )
        except TypeError:
            # websockets versi lama yang belum punya parameter proxy
            return connect(
                url,
                additional_headers=headers,
                open_timeout=20,
                close_timeout=5,
            )


# Singleton yang dipakai data layer & main.py
oanda_stream = OandaPriceStream()


def start_stream() -> None:
    """Start streaming global (idempotent) — dipanggil main.py saat boot."""
    oanda_stream.start()


def _test():
    """CLI dev: python -m data.oanda_stream (jalan 30 detik, print harga live)."""
    start_stream()
    print("Menunggu harga live 30 detik... (pastikan OANDA_API_KEY terisi)")
    end = time.time() + 30
    while time.time() < end:
        for inst in oanda_stream.live_instruments[:5]:
            p = oanda_stream.get_price(inst)
            if p:
                print(f"{inst}: mid={p['mid']} bid={p['bid']} ask={p['ask']}")
        time.sleep(5)


if __name__ == "__main__":
    _test()
