"""
Market Data Aggregator - Mengambil data harga dari multiple sources.
Multi-source redundancy: Yahoo Finance (primary) -> Alpha Vantage -> Finnhub -> Twelve Data.

Semua data delayed 15-20 menit (kecuali real-time berbayar).
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

import yfinance as yf
import requests
import aiohttp

from config.settings import (
    ALPHA_VANTAGE_KEY,
    FINNHUB_KEY,
    TWELVEDATA_KEY,
    EXCHANGE_RATE_KEY,
)
from config.providers import YAHOO_SYMBOLS
from data.cache import cache, cached, CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)

# Session HTTP dengan User-Agent browser. Hanya dipakai untuk yfinance 0.x
# (berbasis requests polos yang mudah diblokir Yahoo). yfinance 1.x SUDAH
# memakai curl_cffi session yang meng-impersonasi Chrome secara default —
# menggantinya dengan requests.Session justru menurunkan proteksi anti-429.
_YF_MAJOR_VERSION = int(yf.__version__.split(".")[0])
_YF_SESSION = requests.Session()
_YF_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
})


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

    # ===================== YAHOO FINANCE (Primary) =====================

    def get_yahoo_data(self, symbol: str, period: str = "5d", interval: str = "1h", ohlcv_limit: int = 5) -> Dict:
        """
        Ambil data harga dari Yahoo Finance.
        Sumber utama karena unlimited dan tanpa API key.

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
        cache_key = f"yahoo:{symbol}:{period}:{interval}:n{ohlcv_limit}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        try:
            if _YF_MAJOR_VERSION == 0:
                # yfinance 0.x: pakai session dengan User-Agent browser
                # (param session tersedia sejak 0.2.41).
                ticker = yf.Ticker(symbol, session=_YF_SESSION)
            else:
                # yfinance 1.x: session default sudah curl_cffi (impersonasi
                # Chrome) — biarkan apa adanya agar anti-429 tetap aktif.
                ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=interval)

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

            cache.set(cache_key, result, CACHE_TTL_SECONDS)
            return result

        except Exception as e:
            logger.warning(f"Yahoo Finance error for {symbol}: {e}")
            error_result = {
                "source": "Yahoo Finance",
                "symbol": symbol,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }
            # Negative cache: saat Yahoo rate-limited (429), get_market_summary /
            # get_top_movers memanggil banyak simbol beruntun. Menyimpan error
            # mencegah request tambahan yang hanya memperparah 429.
            try:
                cache.set(cache_key, error_result, CACHE_TTL_SECONDS)
            except Exception:
                pass
            return error_result

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

            async with aiohttp.ClientSession() as session:
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
        async with aiohttp.ClientSession() as session:
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
            url = f"https://finnhub.io/api/v1/quote"
            params = {"symbol": fh_symbol, "token": self.finnhub_key}

            async with aiohttp.ClientSession() as session:
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
            async with aiohttp.ClientSession() as session:
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

    async def get_all_quotes(self, symbol: str) -> Dict:
        """
        Ambil data dari semua source sekaligus (parallel).
        Mengembalikan data agregat dengan cross-validation.
        """
        # Yahoo Finance (synchronous, jalan di thread pool)
        yahoo = await asyncio.to_thread(self.get_yahoo_data, symbol)

        # Alpha Vantage & Finnhub (async parallel)
        alpha, finnhub = await asyncio.gather(
            self.get_alpha_vantage_quote(symbol),
            self.get_finnhub_quote(symbol),
            return_exceptions=True,
        )

        sources = [yahoo]
        if not isinstance(alpha, Exception) and "error" not in alpha:
            sources.append(alpha)
        if not isinstance(finnhub, Exception) and "error" not in finnhub:
            sources.append(finnhub)

        # Hitung average price dari multiple sources untuk cross-validation
        prices = [
            s.get("current_price")
            for s in sources
            if s.get("current_price") and isinstance(s.get("current_price"), (int, float))
        ]
        avg_price = round(sum(prices) / len(prices), 5) if prices else None

        return {
            "symbol": symbol,
            "average_price": avg_price,
            "sources_available": len(sources),
            "sources": sources,
            "timestamp": datetime.now().isoformat(),
        }

    async def get_multiple_pairs(self, symbols: List[str]) -> Dict[str, Dict]:
        """Ambil data untuk beberapa pair sekaligus."""
        results = {}
        for symbol in symbols:
            results[symbol] = await self.get_all_quotes(symbol)
        return results

    @cached(ttl=CACHE_TTL_SECONDS)
    def get_top_movers(self, limit: int = 5) -> Dict:
        """
        Mendapatkan top movers dari major forex pairs.
        Karena kita tidak punya akses real-time ke semua pair,
        kita ambil data dari pair-pair utama.
        """
        major_pairs = [
            "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
            "AUDUSD=X", "USDCAD=X", "NZDUSD=X",
        ]
        movers = []
        for i, pair in enumerate(major_pairs):
            if i > 0:
                # Jeda kecil antar request agar tidak burst
                time.sleep(0.4)
            data = self.get_yahoo_data(pair, period="2d")
            if data.get("current_price") and data.get("change_pct") is not None:
                movers.append({
                    "symbol": pair,
                    "price": data["current_price"],
                    "change_pct": data["change_pct"],
                    "abs_change": abs(data["change_pct"]),
                })

        movers.sort(key=lambda x: x["abs_change"], reverse=True)
        return {
            "top_gainers": [m for m in movers if m["change_pct"] > 0][:limit],
            "top_losers": [m for m in movers if m["change_pct"] < 0][:limit],
        }

    def get_market_summary(self) -> str:
        """
        Mendapatkan ringkasan pasar untuk morning brief.

        Hasil di-cache 10 menit: tanpa cache, setiap pertanyaan user yang butuh
        data pasar memicu 7 request Yahoo beruntun dan memperparah rate limit.
        Kalau SEMUA simbol gagal, hasil TIDAK di-cache agar pemulihan Yahoo tidak
        tertutup — per-symbol negative cache (5 menit) tetap mencegah request ulang.
        """
        cache_key = "market_summary"
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

        lines = ["📊 *RINGKASAN PASAR*\n"]
        found = 0
        for i, (name, symbol) in enumerate(pairs.items()):
            if i > 0:
                # Jeda kecil antar request agar tidak burst (memperparah 429)
                time.sleep(0.4)
            data = self.get_yahoo_data(symbol, period="2d")
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
                cache.set(cache_key, result, 600)
            except Exception:
                pass
        return result

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
