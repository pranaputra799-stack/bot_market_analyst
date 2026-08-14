"""
OANDA v20 REST API Client — data harga REAL-TIME untuk Forex & Gold (XAU/USD).

Kenapa OANDA? Sumber lama (Yahoo Finance) delayed 15-20 menit. OANDA demo
memberikan harga streaming real-time GRATIS (akun demo = virtual money),
jadi bot bisa menjawab "harga gold sekarang" dengan harga yang benar-benar live.

Endpoint yang dipakai:
- GET /v3/accounts/{accountID}/pricing?instruments=...  → harga bid/ask real-time
- GET /v3/instruments/{instrument}/candles              → OHLCV (chart & indikator)
- GET /v3/accounts                                      → auto-detect account ID dari token

Dokumentasi resmi: https://developer.oanda.com/rest-live-v20/
"""
import logging
from typing import Dict, List, Optional

import requests

from config.settings import OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENV
from config.providers import OANDA_SYMBOLS

logger = logging.getLogger(__name__)

# Base URL per environment (OANDA_ENV = "practice" (demo) atau "live")
_BASE_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

_TIMEOUT = 10  # detik per request HTTP

# Kode mata uang ISO 4217 yang dikenal OANDA. Dipakai is_forex() untuk
# membedakan pair forex (EUR_USD) dari logam/index/komoditas yang juga
# berformat XXX_YYY tapi bukan mata uang (XAU_USD, SPX500_USD, WTICO_USD).
_CURRENCY_CODES = {
    "AED", "AUD", "BGN", "BRL", "CAD", "CHF", "CNH", "CNY", "CZK",
    "DKK", "EUR", "GBP", "HKD", "HRK", "HUF", "IDR", "ILS", "INR",
    "ISK", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PLN", "RON",
    "RUB", "SAR", "SEK", "SGD", "THB", "TRY", "TWD", "USD", "ZAR",
}

# Yahoo interval -> granularity OANDA (v20)
_INTERVAL_TO_GRANULARITY = {
    "1m": "M1", "2m": "M2", "5m": "M5", "15m": "M15", "30m": "M30",
    "60m": "H1", "1h": "H1", "2h": "H2", "4h": "H4", "6h": "H6",
    "8h": "H8", "12h": "H12", "1d": "D", "5d": "D", "1wk": "W", "1mo": "M",
}

# Perkiraan jumlah bar per periode (dipakai kalau interval intraday)
_PERIOD_HOURS = {
    "1d": 24, "2d": 48, "5d": 120, "1wk": 168, "1mo": 720,
    "3mo": 2160, "6mo": 4320, "1y": 8640, "ytd": 4320,
}

_MAX_CANDLES_PER_REQUEST = 5000  # batas OANDA per request


class OandaClient:
    """
    Client ringan untuk OANDA v20 REST API.

    Sinkron (requests) — dipanggil lewat asyncio.to_thread oleh data layer.
    Setiap method melempar exception saat gagal; caller (MarketDataAggregator)
    yang memutuskan untuk fallback ke Yahoo Finance.
    """

    def __init__(self, api_key: str = "", account_id: str = "", env: str = ""):
        self.api_key = api_key or OANDA_API_KEY
        self._account_id = account_id or OANDA_ACCOUNT_ID
        self.env = (env or OANDA_ENV or "practice").lower()
        self.base_url = _BASE_URLS.get(self.env, _BASE_URLS["practice"])
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        })
        self._account_discovered: Optional[str] = None
        self._discovery_attempted = False

    # ===================== STATUS =====================

    @property
    def is_configured(self) -> bool:
        """True bila API key terisi (account ID bisa auto-detect)."""
        return bool(self.api_key)

    @property
    def is_practice(self) -> bool:
        return self.env == "practice"

    @property
    def env_name(self) -> str:
        return "Demo" if self.is_practice else "Live"

    # ===================== SIMBOL =====================

    @staticmethod
    def instrument_for(yahoo_symbol: str) -> Optional[str]:
        """Konversi simbol Yahoo (EURUSD=X, GC=F) ke instrumen OANDA (EUR_USD, XAU_USD)."""
        return OANDA_SYMBOLS.get(yahoo_symbol)

    @staticmethod
    def is_forex(instrument: str) -> bool:
        """
        True bila instrumen adalah pair forex (XXX_YYY dengan dua mata uang 3 huruf).

        Position/Order Book OANDA HANYA tersedia untuk forex — gold (XAU_USD),
        index (SPX500_USD), oil (WTICO_USD), crypto (BTC_USD) tidak punya data
        sentimen retail. Dipakai handler /sentimen untuk pesan yang akurat.
        """
        if not instrument or not isinstance(instrument, str):
            return False
        parts = instrument.split("_")
        return (
            len(parts) == 2
            and parts[0] in _CURRENCY_CODES
            and parts[1] in _CURRENCY_CODES
        )

    # ===================== GRANULARITY & COUNT =====================

    @staticmethod
    def granularity_for(interval: str) -> str:
        """Yahoo interval -> OANDA granularity (default H1)."""
        return _INTERVAL_TO_GRANULARITY.get(interval, "H1")

    @classmethod
    def count_for(cls, period: str, interval: str, ohlcv_limit: int) -> int:
        """
        Perkirakan jumlah candle yang dibutuhkan agar data mencakup periode
        yang diminta DAN minimal ohlcv_limit bar tersedia (untuk indikator/chart).
        """
        granularity = cls.granularity_for(interval)
        granularity_minutes = {
            "M1": 1, "M2": 2, "M5": 5, "M15": 15, "M30": 30,
            "H1": 60, "H2": 120, "H4": 240, "H6": 360, "H8": 480, "H12": 720,
            "D": 1440, "W": 10080, "M": 43200,
        }.get(granularity, 60)

        hours = _PERIOD_HOURS.get(period, 120)
        if granularity == "D":
            count = max(3, hours // 24)
        elif granularity == "W":
            count = max(2, hours // 168)
        elif granularity == "M":
            count = max(2, hours // 720)
        else:
            count = (hours * 60) // granularity_minutes

        count = max(count, ohlcv_limit, 2)
        return min(count, _MAX_CANDLES_PER_REQUEST)

    # ===================== ACCOUNT ID =====================

    def _resolve_account_id(self) -> str:
        """
        Account ID dari env, atau auto-detect akun pertama via GET /v3/accounts.
        Hasil discovery di-cache di memori (dicek sekali per proses).
        """
        if self._account_id:
            return self._account_id
        if self._account_discovered:
            return self._account_discovered
        if self._discovery_attempted:
            return ""

        self._discovery_attempted = True
        try:
            resp = self._session.get(f"{self.base_url}/v3/accounts", timeout=_TIMEOUT)
            resp.raise_for_status()
            accounts = resp.json().get("accounts", [])
            if accounts:
                self._account_discovered = accounts[0]["id"]
                logger.info(f"OANDA account auto-detected: {self._account_discovered}")
                return self._account_discovered
            logger.warning("OANDA /v3/accounts mengembalikan daftar kosong")
        except Exception as e:
            logger.warning(f"OANDA account discovery gagal: {e}")
        return ""

    def resolve_account_id(self) -> str:
        """Public wrapper — dipakai OandaPriceStream untuk discovery account."""
        return self._resolve_account_id()

    # ===================== PRICING (REAL-TIME) =====================

    def get_mid_price(self, instrument: str) -> Dict:
        """
        Harga bid/ask REAL-TIME via /v3/accounts/{id}/pricing.

        Returns:
            {"mid": float, "bid": float, "ask": float, "time": str}

        Raises:
            Exception bila gagal (caller fallback ke Yahoo).
        """
        account_id = self._resolve_account_id()
        if not account_id:
            raise RuntimeError("OANDA account ID tidak tersedia (set OANDA_ACCOUNT_ID atau cek token)")

        resp = self._session.get(
            f"{self.base_url}/v3/accounts/{account_id}/pricing",
            params={"instruments": instrument},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        prices = resp.json().get("prices", [])
        if not prices:
            raise RuntimeError(f"OANDA tidak mengembalikan harga untuk {instrument}")

        price = prices[0]
        bids = price.get("bids") or []
        asks = price.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"OANDA pricing kosong untuk {instrument}")

        bid = float(bids[0]["price"])
        ask = float(asks[0]["price"])
        return {
            "mid": round((bid + ask) / 2.0, 8),
            "bid": bid,
            "ask": ask,
            "time": price.get("time", ""),
        }

    # ===================== CANDLES (OHLCV) =====================

    def get_candles(self, instrument: str, granularity: str = "H1", count: int = 120) -> List[Dict]:
        """
        Riwayat OHLCV via /v3/instruments/{instrument}/candles (mid price).

        Returns:
            List of {date, open, high, low, close, volume} — dari paling lama
            ke paling baru (kronologis), sama seperti format yfinance yang
            dipakai chart generator & indikator.

        Raises:
            Exception bila gagal.
        """
        resp = self._session.get(
            f"{self.base_url}/v3/instruments/{instrument}/candles",
            params={"granularity": granularity, "count": count, "price": "M"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        candles = resp.json().get("candles", [])

        ohlcv = []
        for c in candles:
            mid = c.get("mid")
            if not mid:
                continue
            try:
                ohlcv.append({
                    "date": self._normalize_date(c.get("time", "")),
                    "open": round(float(mid["o"]), 5),
                    "high": round(float(mid["h"]), 5),
                    "low": round(float(mid["l"]), 5),
                    "close": round(float(mid["c"]), 5),
                    "volume": int(c.get("volume", 0)),
                    "complete": bool(c.get("complete", False)),
                })
            except (TypeError, ValueError, KeyError) as e:
                logger.debug(f"OANDA candle skip ({e}): {mid}")
                continue
        return ohlcv

    @staticmethod
    def _normalize_date(raw: str) -> str:
        """
        Normalisasi timestamp OANDA (RFC3339 nanodetik, mis. "2026-08-05T13:00:00.000000000Z")
        ke format yfinance "%Y-%m-%d %H:%M:%S" agar chart generator & indikator
        bisa parse langsung tanpa bergantung fallback fromisoformat (Python >= 3.11).
        """
        if not raw:
            return ""
        # "T" → spasi, buang suffix "Z" dan pecahan detik ".nnnn"
        return raw.replace("T", " ").replace("Z", "").split(".")[0].strip()

    def get_previous_close(self, instrument: str) -> Optional[float]:
        """
        Close sesi trading sebelumnya dari candle harian (granularity=D).

        Aturan (menyamakan makna "previous close" Yahoo):
        - Candle terakhir BELUM complete (sesi hari ini berjalan) → previous
          close = close candle complete terakhir (kemarin).
        - Candle terakhir SUDAH complete (pasar tutup / weekend) → previous
          close = close candle complete kedua terakhir.

        Returns None bila data harian tidak cukup.
        """
        candles = self.get_candles(instrument, granularity="D", count=4)
        complete = [c for c in candles if c.get("complete")]
        if not complete:
            return None

        last = candles[-1]
        if last.get("complete"):
            # Sesi terakhir sudah selesai → bandingkan dengan sesi sebelumnya
            return float(complete[-2]["close"]) if len(complete) >= 2 else float(complete[-1]["close"])
        # Sesi hari ini masih berjalan → bandingkan dengan sesi complete terakhir
        return float(complete[-1]["close"])

    # ===================== SENTIMEN RETAIL (POSITION / ORDER BOOK) =====================

    def get_position_book(self, instrument: str) -> Dict:
        """
        Position Book OANDA — rasio posisi LONG/SHORT trader ritel saat ini.

        Endpoint: GET /v3/instruments/{instrument}/positionBook
        Dokumentasi: https://developer.oanda.com/rest-live-v20/instrument-ep/

        Returns:
            {"instrument", "time", "long_ratio", "short_ratio", "positions": [...]}

        Raises:
            Exception bila gagal (mis. instrumen tidak didukung / akun non-demo).
        """
        resp = self._session.get(
            f"{self.base_url}/v3/instruments/{instrument}/positionBook",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        book = data.get("positionBook") or {}
        buckets = book.get("buckets") or []
        if not buckets:
            raise RuntimeError(f"OANDA positionBook kosong untuk {instrument}")

        long_ratio = 0.0
        for b in buckets:
            long_ratio += float(b.get("longCount", 0))
        total = sum(
            float(b.get("longCount", 0)) + float(b.get("shortCount", 0)) for b in buckets
        )
        if total <= 0:
            raise RuntimeError(f"OANDA positionBook tanpa data posisi untuk {instrument}")
        long_ratio = round(long_ratio / total * 100.0, 1)

        return {
            "instrument": instrument,
            "time": book.get("time", ""),
            "long_ratio": long_ratio,
            "short_ratio": round(100.0 - long_ratio, 1),
            "positions": buckets,
        }

    def get_order_book(self, instrument: str) -> Dict:
        """
        Order Book OANDA — konsentrasi pending order BUY/SELL di atas/bawah harga.

        Endpoint: GET /v3/instruments/{instrument}/orderBook

        Returns:
            {"instrument", "time", "buy_ratio", "sell_ratio", "price", "orders": [...]}

        Raises:
            Exception bila gagal.
        """
        resp = self._session.get(
            f"{self.base_url}/v3/instruments/{instrument}/orderBook",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        book = data.get("orderBook") or {}
        buckets = book.get("buckets") or []
        if not buckets:
            raise RuntimeError(f"OANDA orderBook kosong untuk {instrument}")

        buy_ratio = 0.0
        for b in buckets:
            buy_ratio += float(b.get("buyCount", 0))
        total = sum(
            float(b.get("buyCount", 0)) + float(b.get("sellCount", 0)) for b in buckets
        )
        if total <= 0:
            raise RuntimeError(f"OANDA orderBook tanpa data order untuk {instrument}")
        buy_ratio = round(buy_ratio / total * 100.0, 1)

        return {
            "instrument": instrument,
            "time": book.get("time", ""),
            "buy_ratio": buy_ratio,
            "sell_ratio": round(100.0 - buy_ratio, 1),
            "price": book.get("price", ""),
            "orders": buckets,
        }

    def get_retail_sentiment(self, instrument: str) -> Dict:
        """
        Gabungan sentimen retail: position book + order book dalam satu panggilan.
        Aman dipanggil meski salah satu endpoint gagal (mis. instrumen tidak
        punya order book di region tertentu).

        Returns:
            {"instrument", "time", "long_ratio", "short_ratio",
             "buy_ratio", "sell_ratio", "price"}
        """
        result: Dict = {"instrument": instrument}
        try:
            pb = self.get_position_book(instrument)
            result.update({
                "time": pb.get("time", ""),
                "long_ratio": pb.get("long_ratio"),
                "short_ratio": pb.get("short_ratio"),
            })
        except Exception as e:
            logger.debug(f"positionBook gagal untuk {instrument}: {e}")
        try:
            ob = self.get_order_book(instrument)
            result.update({
                "buy_ratio": ob.get("buy_ratio"),
                "sell_ratio": ob.get("sell_ratio"),
                "price": ob.get("price", ""),
            })
        except Exception as e:
            logger.debug(f"orderBook gagal untuk {instrument}: {e}")
        if "long_ratio" not in result and "buy_ratio" not in result:
            raise RuntimeError(f"Sentimen retail tidak tersedia untuk {instrument}")
        return result


def _test():
    """CLI dev: python -m data.oanda_client EUR_USD"""
    import sys

    inst = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    client = OandaClient()
    if not client.is_configured:
        logger.warning("OANDA_API_KEY belum di-set (lihat .env)")
        return
    try:
        price = client.get_mid_price(inst)
        logger.info(f"{inst} mid={price['mid']} bid={price['bid']} ask={price['ask']} ({client.env_name})")
        candles = client.get_candles(inst, "H1", 5)
        logger.info(f"candles: {len(candles)} bar, terakhir close={candles[-1]['close'] if candles else '-'}")
        logger.info(f"previous_close (daily): {client.get_previous_close(inst)}")
    except Exception as e:
        logger.error(f"ERROR: {e}")


if __name__ == "__main__":
    _test()
