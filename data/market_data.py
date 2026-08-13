"""
Market Data Aggregator - Mengambil data harga dari multiple sources.

Sumber data:
- OANDA (PRIMARY untuk Forex & Gold bila terkonfigurasi) — harga REAL-TIME
  (streaming bid/ask via OANDA demo API), tidak delayed seperti Yahoo.
- Yahoo Finance (fallback + instrumen non-OANDA: USD/IDR, DXY, index, crypto)
- Alpha Vantage -> Finnhub (fallback cadangan)

OANDA tidak terkonfigurasi? Semua instrumen otomatis kembali ke Yahoo Finance
(perilaku lama, zero perubahan).
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Any

import requests

from data.http_session import get_aiohttp_session
from config.settings import (
    ALPHA_VANTAGE_KEY,
    FINNHUB_KEY,
    TWELVEDATA_KEY,
    EXCHANGE_RATE_KEY,
    OANDA_PRICE_TTL,
)
from config.providers import OANDA_SYMBOLS
from data.cache import cache, CACHE_TTL_SECONDS
from data.oanda_client import OandaClient
from data.oanda_stream import oanda_stream
from data.ccxt_client import get_crypto_ticker, get_crypto_ohlcv, CRYPTO_SYMBOLS

logger = logging.getLogger(__name__)

# Ringkasan pasar (get_market_summary) di-cache 30 menit — bukan 10. Setiap
# miss memicu 7 simbol beruntun ke sumber data, jadi TTL lebih panjang = jauh
# lebih sedikit burst ke Yahoo/OANDA. Tombol '🔁 Refresh' di /overview tetap
# bisa memaksa ambil ulang (refresh=True) kapan pun user mau.
MARKET_SUMMARY_TTL = 1800

# yfinance di-load LAZY (~100MB pandas ikut terbawa) — hanya saat data Yahoo
# benar-benar diminta. Startup bot jadi jauh lebih ringan (krusial di container
# memory kecil seperti free tier JustRunMy/Railway yang OOM-restart bila RSS
# melewati limit).
_yf = None
_yf_lock = threading.Lock()
_yf_session = None


def _get_yf():
    """Muat modul yfinance sekali (lazy, thread-safe). None bila tidak terpasang."""
    global _yf
    if _yf is None:
        with _yf_lock:
            if _yf is None:
                try:
                    import yfinance as _yf_mod  # type: ignore
                    _yf = _yf_mod
                except ImportError:  # pragma: no cover - yfinance dependency keras
                    _yf = None
    return _yf


def _get_yf_context():
    """Siapkan (yf_module, session) untuk Ticker — versi-aware, di-cache.

    Session HTTP dengan User-Agent browser hanya dipakai untuk yfinance 0.x
    (berbasis requests polos yang mudah diblokir Yahoo). yfinance 1.x SUDAH
    memakai curl_cffi session yang meng-impersonasi Chrome secara default —
    menggantinya dengan requests.Session justru menurunkan proteksi anti-429.
    """
    global _yf_session
    yf_mod = _get_yf()
    if yf_mod is None:
        return None, None
    try:
        major = int(str(yf_mod.__version__).split(".")[0])
    except (AttributeError, ValueError):
        # Versi tidak terbaca (mis. __version__ hilang) — asumsi 1.x yang
        # sudah memakai curl_cffi (session default, anti-429 tetap aktif).
        major = 1
    if major == 0:
        if _yf_session is None:
            with _yf_lock:
                if _yf_session is None:
                    _yf_session = requests.Session()
                    _yf_session.headers.update({
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"
                        ),
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                    })
        return yf_mod, _yf_session
    return yf_mod, None


class MarketDataAggregator:
    """
    Aggregator data pasar dari multiple sources.
    Secara otomatis fallback jika satu source gagal.
    """

    def __init__(self):
        self.alpha_key = ALPHA_VANTAGE_KEY
        self.finnhub_key = FINNHUB_KEY
        self.twelve_key = TWELVEDATA_KEY
        self.exchange_key = EXCHANGE_RATE_KEY
        self.oanda = OandaClient()
        # Sumber data TERAKHIR yang benar-benar dipakai per simbol (symbol -> source).
        # Dicatat saat fetch sukses (OANDA/ccxt/Yahoo) — dipakai /status untuk
        # memvalidasi beban yfinance: terlihat instrumen mana yang benar-benar
        # menyentuh Yahoo vs real-time OANDA/ccxt.
        self._last_sources: Dict[str, str] = {}

    # ===================== OANDA (Primary — Forex & Gold) =====================

    def _get_oanda_data(self, symbol: str, period: str, interval: str, ohlcv_limit: int) -> Dict:
        """
        Ambil data REAL-TIME dari OANDA untuk forex & gold.

        - current_price = mid price streaming (bid/ask) — LIVE, bukan delayed.
        - previous_close = close sesi trading sebelumnya (candle harian OANDA).
        - ohlcv = candle OANDA (format sama persis dengan yfinance) untuk
          chart & indikator teknikal.

        Returns:
            Dict dengan shape identik get_yahoo_data (source="OANDA ...").

        Raises:
            Exception bila data tidak bisa diambil — caller fallback ke Yahoo.
        """
        instrument = self.oanda.instrument_for(symbol)
        if not instrument:
            raise ValueError(f"{symbol} tidak didukung OANDA")

        granularity = self.oanda.granularity_for(interval)
        count = self.oanda.count_for(period, interval, ohlcv_limit)

        # Candle untuk chart & indikator — gagal bukan akhir dunia, harga live
        # tetap berharga (fallback price-only).
        ohlcv = []
        try:
            ohlcv = self.oanda.get_candles(instrument, granularity=granularity, count=count)
        except Exception as e:
            logger.warning(f"OANDA candles gagal untuk {symbol}: {e}")

        # Harga live. PRIORITAS: harga streaming WebSocket (tanpa request HTTP,
        # paling real-time & paling hemat kuota REST). Kalau belum tersedia,
        # fallback ke REST pricing; kalau itu pun gagal, degrade ke close candle
        # terakhir agar data tetap tersedia (candle tetap lebih fresh dari Yahoo).
        bid = ask = None
        current_price = None
        stream_price = oanda_stream.get_price(instrument)
        if stream_price:
            current_price = stream_price["mid"]
            bid, ask = stream_price["bid"], stream_price["ask"]
        else:
            try:
                price = self.oanda.get_mid_price(instrument)
                current_price = price["mid"]
                bid, ask = price["bid"], price["ask"]
            except Exception as e:
                logger.warning(f"OANDA pricing gagal untuk {symbol}, pakai close candle: {e}")
                if ohlcv:
                    current_price = ohlcv[-1]["close"]

        if current_price is None:
            raise RuntimeError(f"OANDA tidak mengembalikan harga untuk {instrument}")

        # Previous close dari candle harian — jangan sampai kegagalan sub-request
        # ini membatalkan harga real-time yang sudah didapat.
        previous_close = None
        try:
            previous_close = self.oanda.get_previous_close(instrument)
        except Exception as e:
            logger.warning(f"OANDA previous_close gagal untuk {symbol}: {e}")

        change_pct = None
        if previous_close and previous_close > 0:
            change_pct = round(((current_price - previous_close) / previous_close) * 100, 2)

        spread = None
        if bid is not None and ask is not None:
            spread = round(ask - bid, 8)

        return {
            "source": f"OANDA ({self.oanda.env_name})",
            "symbol": symbol,
            "instrument": instrument,
            "current_price": current_price,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "previous_close": previous_close,
            "change_pct": change_pct,
            "high_52w": None,
            "low_52w": None,
            "volume": ohlcv[-1].get("volume", 0) if ohlcv else 0,
            "market_cap": None,
            "ohlcv": ohlcv[-ohlcv_limit:],
            "timestamp": datetime.now().isoformat(),
        }

    # ===================== YAHOO FINANCE (Fallback & Non-OANDA) =====================

    def get_yahoo_data(self, symbol: str, period: str = "5d", interval: str = "1h", ohlcv_limit: int = 5) -> Dict:
        """
        Ambil data harga — OANDA real-time dulu untuk forex & gold, lalu ccxt
        untuk crypto (BTC/ETH), fallback Yahoo.

        Bila OANDA terkonfigurasi dan simbol didukung (lihat OANDA_SYMBOLS),
        data diambil dari OANDA demo/live API (harga streaming, tidak delayed).
        Crypto (BTC-USD/ETH-USD) memakai exchange publik via ccxt (real-time,
        tanpa API key). Jika sumber-sumber itu gagal / tidak dikonfigurasi /
        instrumen non-OANDA-non-crypto (USD/IDR, DXY, index), otomatis kembali
        ke Yahoo Finance.

        Args:
            symbol: Simbol Yahoo Finance (e.g. EURUSD=X, GC=F)
            period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
            interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo
            ohlcv_limit: Jumlah bar OHLCV terakhir yang disertakan. Default 5
                (ringan, cukup untuk chart singkat); analisis teknikal butuh
                15-50 bar → pakai 60 lewat get_ohlcv_history().

        Returns:
            Dict dengan data harga atau error message
        """
        # OANDA real-time untuk forex & gold — TTL pendek (OANDA_PRICE_TTL)
        # agar harga selalu segar; gagal apa pun → lanjut ke Yahoo di bawah.
        if self.oanda.is_configured and symbol in OANDA_SYMBOLS:
            oanda_key = f"oanda:{symbol}:{period}:{interval}:n{ohlcv_limit}"
            cached_oanda = cache.get(oanda_key)
            if cached_oanda:
                if "error" not in cached_oanda:
                    self._last_sources[symbol] = cached_oanda.get("source") or "OANDA"
                    return cached_oanda
                # Negative cache (OANDA sedang down) — lewati, pakai Yahoo.
                cached_oanda = None
            try:
                result = self._get_oanda_data(symbol, period, interval, ohlcv_limit)
                if result.get("current_price") is not None:
                    self._last_sources[symbol] = result["source"]
                    cache.set(oanda_key, result, OANDA_PRICE_TTL)
                    return result
            except Exception as e:
                logger.warning(f"OANDA gagal untuk {symbol}, fallback Yahoo: {e}")
                # Negative cache pendek: jangan retry OANDA berulang saat down
                # (mirip negative cache 429 Yahoo di bawah).
                try:
                    cache.set(oanda_key, {"error": str(e)}, max(OANDA_PRICE_TTL, 60))
                except Exception:
                    pass

        # Crypto (BTC-USD / ETH-USD): data REAL-TIME dari exchange publik
        # (ccxt, tanpa API key) — Yahoo delayed 15-20 menit untuk crypto.
        # - Query harga spot (ohlcv_limit <= 5) → ticker (change 24 jam).
        # - Query OHLCV (chart / analisis teknikal, ohlcv_limit > 5) → candle
        #   real-time ccxt, jadi chart BTC/ETH & indikator ikut real-time.
        # Gagal apa pun → lanjut ke Yahoo di bawah (negative cache ccxt
        # mencegah request beruntun saat exchange down).
        if symbol in CRYPTO_SYMBOLS:
            if ohlcv_limit <= 5:
                crypto = get_crypto_ticker(symbol)
                if crypto and crypto.get("current_price") is not None:
                    self._last_sources[symbol] = crypto["source"]
                    return crypto
            else:
                crypto_ohlcv = get_crypto_ohlcv(symbol, interval=interval, limit=ohlcv_limit)
                if crypto_ohlcv:
                    self._last_sources[symbol] = "ccxt (OHLCV real-time)"
                    return self._build_crypto_ohlcv_result(symbol, crypto_ohlcv)
            logger.info(f"ccxt tidak tersedia untuk {symbol}, fallback Yahoo")

        cache_key = f"yahoo:{symbol}:{period}:{interval}:n{ohlcv_limit}"
        cached_data = cache.get(cache_key)
        if cached_data:
            self._last_sources[symbol] = cached_data.get("source") or "Yahoo Finance"
            return cached_data

        try:
            yf_mod, yf_session = _get_yf_context()
            if yf_mod is None:
                return {"source": "Yahoo", "symbol": symbol, "error": "yfinance not installed"}
            if yf_session is not None:
                # yfinance 0.x: pakai session dengan User-Agent browser
                # (param session tersedia sejak 0.2.41).
                ticker = yf_mod.Ticker(symbol, session=yf_session)
            else:
                # yfinance 1.x: session default sudah curl_cffi (impersonasi
                # Chrome) — biarkan apa adanya agar anti-429 tetap aktif.
                ticker = yf_mod.Ticker(symbol)

            # Retry 1× untuk error TRANSIEN (jaringan/flaky Yahoo). Error 429 /
            # rate-limit TIDAK di-retry — negative cache panjang (lihat except
            # di bawah) yang menanganinya; retry hanya memperparah 429.
            hist = None
            for attempt in range(2):
                try:
                    hist = ticker.history(period=period, interval=interval)
                    break
                except Exception as retry_err:
                    retry_lower = str(retry_err).lower()
                    if (
                        "429" in retry_lower
                        or "rate limit" in retry_lower
                        or "too many requests" in retry_lower
                    ):
                        raise  # rate limit — jangan retry
                    if attempt == 0:
                        logger.info(
                            f"Yahoo history transient error for {symbol}, retrying: {retry_err}"
                        )
                        time.sleep(1.5)
                        continue
                    raise

            # Ambil data teknikal dari history
            ohlcv_data = []
            if not hist.empty:
                for idx, row in hist.tail(ohlcv_limit).iterrows():
                    ohlcv_data.append({
                        "date": idx.strftime("%Y-%m-%d %H:%M") if hasattr(idx, 'strftime') else str(idx),
                        "open": round(float(row["Open"]), 5),
                        "high": round(float(row["High"]), 5),
                        "low": round(float(row["Low"]), 5),
                        "close": round(float(row["Close"]), 5),
                        "volume": self._safe_int(row.get("Volume")),
                    })

            # Harga & perubahan dihitung DARI HISTORY, bukan ticker.info.
            # ticker.info adalah panggilan berat (quoteSummary + fetch history ulang)
            # dan penyebab utama rate limit 429 Yahoo. Dengan derivasi dari history,
            # satu simbol cukup 1 request dan data harga tetap tersedia.
            current_price = None
            previous_close = None
            change_pct = None
            volume = None
            if not hist.empty:
                # Buang nilai NaN sebelum kalkulasi
                closes = [float(c) for c in hist["Close"].tolist() if c == c]
                if closes:
                    current_price = closes[-1]
                    if len(closes) >= 2:
                        # Interval intraday: bandingkan dengan close ±24 jam lalu
                        # (jumlah bar = 24 jam / durasi bar); interval harian:
                        # bandingkan dengan bar sebelumnya.
                        bars_per_day = None
                        if interval.endswith("m"):
                            bars_per_day = 1440 // int(interval[:-1])
                        elif interval.endswith("h"):
                            bars_per_day = 24 // int(interval[:-1])
                        if bars_per_day and len(closes) > bars_per_day:
                            # Bar terakhir = -1, jadi N bar ke belakang = -(N+1)
                            prev_idx = -(bars_per_day + 1)
                        else:
                            prev_idx = -2
                        previous_close = closes[prev_idx]
                        if previous_close > 0:
                            change_pct = round(((current_price - previous_close) / previous_close) * 100, 2)
                volume_col = hist.get("Volume")
                if volume_col is not None and not volume_col.empty:
                    volume = self._safe_int(volume_col.iloc[-1])

            result = {
                "source": "Yahoo Finance",
                "symbol": symbol,
                "current_price": current_price,
                "previous_close": previous_close,
                "change_pct": change_pct,
                # high_52w/low_52w/market_cap butuh ticker.info yang sangat rawan
                # 429; sengaja dilewati agar beban request ke Yahoo minimal.
                # (Caller sudah guard dengan .get(), jadi None aman.)
                "high_52w": None,
                "low_52w": None,
                "volume": volume,
                "market_cap": None,
                "ohlcv": ohlcv_data,
                "timestamp": datetime.now().isoformat(),
            }

            self._last_sources[symbol] = result["source"]
            cache.set(cache_key, result, CACHE_TTL_SECONDS)
            return result

        except Exception as e:
            logger.warning(f"Yahoo Finance error for {symbol}: {e}")
            # Tandai sumber terakhir = ERROR agar /status tidak menampilkan
            # sukses lama yang sudah basi saat fetch terakhir justru gagal
            # (OANDA down + Yahoo error sekaligus).
            self._last_sources[symbol] = "ERROR"
            error_result = {
                "source": "Yahoo Finance",
                "symbol": symbol,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }
            # Negative cache: saat Yahoo rate-limited (429), get_market_summary
            # memanggil banyak simbol beruntun. Menyimpan error mencegah request
            # tambahan yang hanya memperparah 429. Error 429 di-cache LEBIH LAMA
            # (15 menit) agar saat Yahoo sedang memblokir akun, bot tidak mencoba
            # ulang tiap 5 menit (spam log + tetap 429).
            err_lower = str(e).lower()
            is_rate_limit = (
                "429" in err_lower
                or "rate limit" in err_lower
                or "rate-limit" in err_lower
                or "too many requests" in err_lower
            )
            negative_ttl = 900 if is_rate_limit else CACHE_TTL_SECONDS
            try:
                cache.set(cache_key, error_result, negative_ttl)
            except Exception:
                pass
            return error_result

    @staticmethod
    def _build_crypto_ohlcv_result(symbol: str, ohlcv: List[Dict]) -> Dict:
        """
        Bentuk hasil lengkap dari candle ccxt — shape identik get_yahoo_data
        (dengan ohlcv terisi) sehingga chart / analisis teknikal bisa langsung
        memakai: current_price = close terakhir, change vs bar sebelumnya.
        """
        closes = [float(c["close"]) for c in ohlcv if c.get("close") is not None]
        current_price = closes[-1] if closes else None
        previous_close = closes[-2] if len(closes) >= 2 else None
        change_pct = None
        if current_price and previous_close:
            change_pct = round(((current_price - previous_close) / previous_close) * 100, 2)
        return {
            "source": "ccxt (OHLCV real-time)",
            "symbol": symbol,
            "current_price": current_price,
            "previous_close": previous_close,
            "change_pct": change_pct,
            "high_52w": None,
            "low_52w": None,
            "volume": ohlcv[-1].get("volume", 0) if ohlcv else 0,
            "market_cap": None,
            "ohlcv": ohlcv,
            "timestamp": datetime.now().isoformat(),
        }

    def get_ohlcv_history(self, symbol: str, period: str = "3mo", interval: str = "1d", limit: int = 60) -> List[Dict]:
        """
        Ambil riwayat OHLCV dalam (hingga `limit` bar) untuk analisis teknikal.

        Wrapper tipis di atas get_yahoo_data dengan cache terpisah (suffix :n),
        jadi menganalisis sebuah instrumen tidak menambah request Yahoo baru
        selama data masih dalam TTL cache (5 menit).

        Returns:
            List of {date, open, high, low, close, volume} atau [] jika gagal.
        """
        try:
            data = self.get_yahoo_data(symbol, period=period, interval=interval, ohlcv_limit=limit)
            if "error" in data:
                return []
            return data.get("ohlcv", []) or []
        except Exception as e:
            logger.warning(f"get_ohlcv_history failed for {symbol}: {e}")
            return []

    @staticmethod
    def _safe_int(value: Any) -> int:
        """Konversi ke int dengan aman (NaN/None -> 0)."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    # ===================== ALPHA VANTAGE (Secondary) =====================

    async def get_alpha_vantage_quote(self, symbol: str) -> Dict:
        """
        Ambil quote dari Alpha Vantage.
        Memiliki RSI, MACD, Bollinger Bands built-in.
        """
        if not self.alpha_key:
            return {"source": "Alpha Vantage", "error": "No API key configured"}

        try:
            # Konversi symbol untuk Alpha Vantage (EURUSD -> EUR/USD)
            av_symbol = symbol.replace("=", "").replace("-", "/")
            if av_symbol.endswith("X"):
                av_symbol = av_symbol[:-1]
            if av_symbol == "GC/F":
                av_symbol = "XAUUSD"

            url = "https://www.alphavantage.co/query"
            params = {
                "function": "GLOBAL_QUOTE",
                "symbol": av_symbol,
                "apikey": self.alpha_key,
            }

            session = get_aiohttp_session()
            async with session.get(url, params=params, timeout=10) as resp:
                data = await resp.json()

            if "Global Quote" not in data:
                return {"source": "Alpha Vantage", "error": data.get("Note", "Unknown error")}

            quote = data["Global Quote"]
            return {
                "source": "Alpha Vantage",
                "symbol": symbol,
                "current_price": float(quote.get("05. price", 0)),
                "change_pct": float(quote.get("10. change percent", "0%").replace("%", "")),
                "high_today": float(quote.get("03. high", 0)),
                "low_today": float(quote.get("04. low", 0)),
                "volume": int(quote.get("06. volume", 0)),
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.warning(f"Alpha Vantage error: {e}")
            return {"source": "Alpha Vantage", "error": str(e)}

    async def get_alpha_indicators(self, symbol: str) -> Dict:
        """Ambil indikator teknikal RSI, MACD, SMA dari Alpha Vantage."""
        if not self.alpha_key:
            return {}

        av_symbol = symbol.replace("=", "").replace("-", "/")
        if av_symbol.endswith("X"):
            av_symbol = av_symbol[:-1]
        if av_symbol == "GC/F":
            av_symbol = "XAUUSD"

        indicators = {}
        session = get_aiohttp_session()
        for func, name in [("RSI", "rsi"), ("MACD", "macd"), ("SMA", "sma_50")]:
            try:
                params = {
                    "function": func,
                    "symbol": av_symbol,
                    "interval": "daily",
                    "time_period": 50 if func == "SMA" else 14,
                    "series_type": "close",
                    "apikey": self.alpha_key,
                }
                async with session.get(
                    "https://www.alphavantage.co/query",
                    params=params,
                    timeout=10
                ) as resp:
                    data = await resp.json()

                key = f"Technical Analysis: {func}"
                if key in data:
                    values = list(data[key].values())
                    if values:
                        indicators[name] = float(values[0][func])

            except Exception as e:
                logger.warning(f"Alpha Vantage {func} error: {e}")

        return {"source": "Alpha Vantage", "indicators": indicators}

    # ===================== FINNHUB (Tertiary) =====================

    async def get_finnhub_quote(self, symbol: str) -> Dict:
        """Ambil quote dari Finnhub."""
        if not self.finnhub_key:
            return {"source": "Finnhub", "error": "No API key configured"}

        # Konversi symbol untuk Finnhub
        fh_symbol = symbol.replace("=X", "").replace("=F", "")
        if fh_symbol == "GC":
            fh_symbol = "XAUUSD"

        try:
            url = "https://finnhub.io/api/v1/quote"
            params = {"symbol": fh_symbol, "token": self.finnhub_key}

            session = get_aiohttp_session()
            async with session.get(url, params=params, timeout=10) as resp:
                data = await resp.json()

            if "c" not in data or data["c"] == 0:
                return {"source": "Finnhub", "error": "No data"}

            return {
                "source": "Finnhub",
                "symbol": symbol,
                "current_price": data.get("c"),
                "change_pct": round(((data.get("c", 0) - data.get("pc", 0)) / data.get("pc", 1)) * 100, 2),
                "high_today": data.get("h"),
                "low_today": data.get("l"),
                "open_today": data.get("o"),
                "previous_close": data.get("pc"),
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.warning(f"Finnhub error: {e}")
            return {"source": "Finnhub", "error": str(e)}

    # ===================== EXCHANGE RATE API =====================

    async def get_exchange_rate(self, base: str, target: str) -> Dict:
        """Ambil kurs dari Exchange Rate API (untuk IDR dan exotic pairs)."""
        if not self.exchange_key:
            return {"source": "Exchange Rate API", "error": "No API key"}

        try:
            url = f"https://v6.exchangerate-api.com/v6/{self.exchange_key}/pair/{base}/{target}"
            session = get_aiohttp_session()
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()

            if data.get("result") != "success":
                return {"source": "Exchange Rate API", "error": data.get("error-type", "Unknown")}

            return {
                "source": "Exchange Rate API",
                "pair": f"{base}/{target}",
                "rate": data.get("conversion_rate"),
                "last_updated": data.get("time_last_update_utc"),
            }

        except Exception as e:
            return {"source": "Exchange Rate API", "error": str(e)}

    # ===================== AGGREGATOR =====================

    def get_market_summary(self, refresh: bool = False) -> str:
        """
        Mendapatkan ringkasan pasar untuk morning brief.

        Hasil di-cache 30 menit (MARKET_SUMMARY_TTL): tanpa cache, setiap
        pertanyaan user yang butuh data pasar memicu 7 request beruntun dan
        memperparah rate limit. Kalau SEMUA simbol gagal, hasil TIDAK di-cache
        agar pemulihan sumber data tidak tertutup — per-symbol negative cache
        (5 menit, 429 → 15 menit) tetap mencegah request ulang.

        Fetch 7 simbol dilakukan PARALEL (ThreadPoolExecutor, maks 3 worker):
        total request ke sumber data SAMA persis dengan versi serial, tapi waktu
        tunggu turun drastis (dulu ~7×(request + 0.4s sleep) ≈ 10-17 detik, kini
        ~3-6 detik). Worker dibatasi agar tidak burst besar sekaligus — Yahoo
        rate limit dihitung per jam, burst kecil 3 request aman dan per-symbol
        negative cache menyerap error 429 tanpa retry.

        Args:
            refresh: Jika True, LEWATI cache dan ambil harga terbaru
                (dipakai tombol '🔁 Refresh' di /overview).
        """
        cache_key = "market_summary"
        if not refresh:
            cached_data = cache.get(cache_key)
            if cached_data:
                return cached_data

        pairs = {
            "EUR/USD": "EURUSD=X",
            "GBP/USD": "GBPUSD=X",
            "USD/JPY": "USDJPY=X",
            "XAU/USD": "GC=F",
            "USD/IDR": "USDIDR=X",
            "DXY": "DX-Y.NYB",
            "S&P 500": "^GSPC",
        }

        # Fetch semua simbol PARALEL — get_yahoo_data punya per-symbol cache
        # sendiri, jadi simbol yang masih hangat tidak keluar ke network sama
        # sekali. Worker dibatasi 3 agar burst tetap modest (bukan 7 sekaligus).
        with ThreadPoolExecutor(max_workers=3) as pool:
            fetched = list(pool.map(
                lambda symbol: self.get_yahoo_data(symbol, period="2d"),
                pairs.values(),
            ))

        lines = ["📊 *RINGKASAN PASAR*\n"]
        found = 0
        for (name, _symbol), data in zip(pairs.items(), fetched):
            price = data.get("current_price")
            change = data.get("change_pct")

            if price is not None:
                found += 1
                arrow = "🟢" if change and change > 0 else "🔴" if change and change < 0 else "⚪"
                change_str = f"{change:+.2f}%" if change is not None else "-"
                lines.append(f"{arrow} *{name}*: {self._format_price(price)} ({change_str})")
            else:
                lines.append(f"⚪ *{name}*: Data tidak tersedia")

        result = "\n".join(lines)
        if found > 0:
            try:
                cache.set(cache_key, result, MARKET_SUMMARY_TTL)
            except Exception:
                pass
        return result

    def get_instrument_source_status(self) -> List[Dict]:
        """
        Peta sumber data per instrumen utama — TANPA network (murni konfigurasi).

        Dipakai /status untuk memvalidasi beban yfinance:
        - `plan`: sumber yang AKAN dipakai berdasarkan konfigurasi saat ini
          (OANDA bila terkonfigurasi & didukung, ccxt untuk crypto, Yahoo sisanya).
        - `actual`: sumber yang TERAKHIR benar-benar dipakai saat fetch (dari
          _last_sources) — None bila simbol belum pernah di-fetch proses ini.
          Ini yang menunjukkan beban yfinance NYATA: kalau plan = OANDA tapi
          actual = Yahoo, berarti OANDA sedang fallback (perlu dicek).

        Returns:
            List of dict: {symbol, display, plan, actual}
        """
        instruments = [
            ("EURUSD=X", "EUR/USD"),
            ("GBPUSD=X", "GBP/USD"),
            ("USDJPY=X", "USD/JPY"),
            ("GC=F", "XAU/USD (Gold)"),
            ("USDIDR=X", "USD/IDR"),
            ("DX-Y.NYB", "DXY"),
            ("^GSPC", "S&P 500"),
            ("BTC-USD", "BTC/USD"),
            ("ETH-USD", "ETH/USD"),
        ]
        rows = []
        for symbol, display in instruments:
            if self.oanda.is_configured and symbol in OANDA_SYMBOLS:
                plan = f"OANDA ({self.oanda.env_name})"
            elif symbol in CRYPTO_SYMBOLS:
                plan = "ccxt (real-time)"
            else:
                plan = "Yahoo Finance"
            rows.append({
                "symbol": symbol,
                "display": display,
                "plan": plan,
                "actual": self._last_sources.get(symbol),
            })
        return rows

    def _format_price(self, price: float) -> str:
        """Format harga sesuai dengan instrumen."""
        if price is None:
            return "-"
        if price >= 1000:
            return f"{price:,.2f}"
        elif price >= 100:
            return f"{price:.2f}"
        elif price >= 10:
            return f"{price:.3f}"
        elif price >= 1:
            return f"{price:.4f}"
        else:
            return f"{price:.5f}"
