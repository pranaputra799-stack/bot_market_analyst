"""
Chart Generator - Membuat grafik harga menggunakan QuickChart.io API.
QuickChart.io: API gratis (1,000 chart/bulan), Chart.js-based, tanpa dependency Python tambahan.

Mendukung:
- Candlestick chart (forex, gold, index)
- Line chart (data time-series)
- Area chart (crypto, volume)
"""
import json
import logging
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# QuickChart.io base URL
QUICKCHART_URL = "https://quickchart.io/chart"

# Nama display untuk setiap simbol
SYMBOL_DISPLAY_NAMES = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "USDCHF=X": "USD/CHF",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "NZDUSD=X": "NZD/USD",
    "GC=F": "XAU/USD (Gold)",
    "SI=F": "XAG/USD (Silver)",
    "USDIDR=X": "USD/IDR",
    "EURIDR=X": "EUR/IDR",
    "GBPIDR=X": "GBP/IDR",
    "BTC-USD": "Bitcoin (BTC/USD)",
    "ETH-USD": "Ethereum (ETH/USD)",
    "DX-Y.NYB": "US Dollar Index (DXY)",
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ",
    "^DJI": "Dow Jones",
    "^VIX": "VIX",
}


class ChartGenerator:
    """
    Generator grafik harga menggunakan QuickChart.io.
    Menghasilkan URL gambar chart yang bisa langsung dikirim ke Telegram.
    """

    # Warna untuk bullish/bearish candle
    CANDLE_UP_COLOR = "#26a69a"   # Hijau (bullish)
    CANDLE_DOWN_COLOR = "#ef5350" # Merah (bearish)
    CHART_BG_COLOR = "#1a1a2e"    # Dark theme background
    GRID_COLOR = "#2a2a4a"        # Grid lines
    TEXT_COLOR = "#e0e0e0"        # Text color

    @staticmethod
    def _get_display_name(symbol: str) -> str:
        """Dapatkan nama display untuk simbol."""
        return SYMBOL_DISPLAY_NAMES.get(symbol, symbol.replace("=X", "").replace("=F", ""))

    def build_candlestick_chart(
        self,
        ohlcv_data: List[Dict],
        symbol: str,
        width: int = 700,
        height: int = 400,
        max_points: int = 30,
    ) -> Optional[str]:
        """
        Generate URL candlestick chart dari data OHLCV.

        Args:
            ohlcv_data: List of {date, open, high, low, close, volume}
            symbol: Simbol Yahoo Finance (e.g. EURUSD=X)
            width: Lebar gambar (px)
            height: Tinggi gambar (px)
            max_points: Max data points (QuickChart URL length limit ~8KB)

        Returns:
            QuickChart URL string, atau None jika data kosong
        """
        if not ohlcv_data or len(ohlcv_data) < 3:
            logger.warning(f"Not enough OHLCV data for {symbol}")
            return None

        display_name = self._get_display_name(symbol)

        # Limit & format data untuk candlestick chart
        data_slice = ohlcv_data[-max_points:] if len(ohlcv_data) > max_points else ohlcv_data
        candles = []
        for row in data_slice:
            date_str = row.get("date", "")
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                timestamp = dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                timestamp = date_str[:10]

            candles.append({
                "t": timestamp,
                "o": float(row.get("open", 0)),
                "h": float(row.get("high", 0)),
                "l": float(row.get("low", 0)),
                "c": float(row.get("close", 0)),
            })

        # Reverse to chronological order
        candles.reverse()

        chart_config = {
            "type": "candlestick",
            "data": {
                "datasets": [{
                    "label": display_name,
                    "data": candles,
                    "color": {
                        "up": self.CANDLE_UP_COLOR,
                        "down": self.CANDLE_DOWN_COLOR,
                        "unchanged": "#888888",
                    },
                }]
            },
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "plugins": {
                    "legend": {
                        "display": True,
                        "labels": {"color": self.TEXT_COLOR, "font": {"size": 14}}
                    },
                    "title": {
                        "display": True,
                        "text": f"{display_name}",
                        "color": self.TEXT_COLOR,
                        "font": {"size": 16, "weight": "bold"}
                    }
                },
                "scales": {
                    "x": {
                        "type": "time",
                        "time": {"unit": "day", "displayFormats": {"day": "MMM d"}},
                        "grid": {"color": self.GRID_COLOR},
                        "ticks": {"color": self.TEXT_COLOR}
                    },
                    "y": {
                        "grid": {"color": self.GRID_COLOR},
                        "ticks": {"color": self.TEXT_COLOR}
                    }
                },
                "backgroundColor": self.CHART_BG_COLOR,
            }
        }

        return self._build_url(chart_config, width, height, version=3)

    def build_line_chart(
        self,
        data_points: List[float],
        labels: List[str],
        symbol: str,
        width: int = 700,
        height: int = 350,
        max_points: int = 30,
    ) -> str:
        """
        Generate URL line chart (fallback jika candlestick tidak tersedia).
        """
        display_name = self._get_display_name(symbol)

        # Limit data
        if len(data_points) > max_points:
            data_points = data_points[-max_points:]
            labels = labels[-max_points:]

        # Deteksi trend utk warna garis
        if len(data_points) >= 2:
            is_up = data_points[-1] >= data_points[0]
            line_color = self.CANDLE_UP_COLOR if is_up else self.CANDLE_DOWN_COLOR
        else:
            line_color = "#42a5f5"

        chart_config = {
            "type": "line",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": display_name,
                    "data": data_points,
                    "borderColor": line_color,
                    "backgroundColor": f"{line_color}33",
                    "borderWidth": 2,
                    "pointRadius": 2,
                    "fill": True,
                    "tension": 0.1,
                }]
            },
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "plugins": {
                    "legend": {
                        "display": True,
                        "labels": {"color": self.TEXT_COLOR, "font": {"size": 14}}
                    },
                    "title": {
                        "display": True,
                        "text": f"{display_name}",
                        "color": self.TEXT_COLOR,
                        "font": {"size": 16, "weight": "bold"}
                    }
                },
                "scales": {
                    "x": {
                        "grid": {"color": self.GRID_COLOR},
                        "ticks": {"color": self.TEXT_COLOR, "maxTicksLimit": 8}
                    },
                    "y": {
                        "grid": {"color": self.GRID_COLOR},
                        "ticks": {"color": self.TEXT_COLOR}
                    }
                },
                "backgroundColor": self.CHART_BG_COLOR,
            }
        }

        return self._build_url(chart_config, width, height)

    def _build_url(
        self,
        chart_config: Dict,
        width: int,
        height: int,
        version: int = 4,
    ) -> str:
        """
        Bangun URL QuickChart dari konfigurasi Chart.js.

        Args:
            chart_config: Chart.js configuration dict
            width: Image width
            height: Image height
            version: Chart.js version (3 for candlestick, 4 for latest)

        Returns:
            Full QuickChart URL
        """
        # Serialize config to JSON
        config_json = json.dumps(chart_config)

        # URL encode
        encoded_config = urllib.parse.quote(config_json)

        # Build URL with dark theme background
        bkg_color = urllib.parse.quote(self.CHART_BG_COLOR)

        url = (
            f"{QUICKCHART_URL}?"
            f"v={version}"
            f"&w={width}"
            f"&h={height}"
            f"&bkg={bkg_color}"
            f"&devicePixelRatio=2"
            f"&c={encoded_config}"
        )

        return url

    @staticmethod
    def get_chart_symbol_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Deteksi simbol dari teks user.

        Args:
            text: Input user (e.g. "chart eurusd", "grafik gold")

        Returns:
            Tuple of (yahoo_symbol, display_name) or (None, None)
        """
        text_lower = text.lower().replace("/chart", "").replace("/grafik", "").strip()

        # Mapping input ke symbol
        keyword_map = {
            "eurusd": "EURUSD=X", "eur/usd": "EURUSD=X", "euro": "EURUSD=X",
            "gbpusd": "GBPUSD=X", "gbp/usd": "GBPUSD=X", "pound": "GBPUSD=X",
            "usdjpy": "USDJPY=X", "usd/jpy": "USDJPY=X",
            "gold": "GC=F", "xauusd": "GC=F", "xau/usd": "GC=F", "emas": "GC=F",
            "silver": "SI=F", "xagusd": "SI=F", "perak": "SI=F",
            "usdidr": "USDIDR=X", "usd/idr": "USDIDR=X", "dolar": "USDIDR=X",
            "btc": "BTC-USD", "bitcoin": "BTC-USD",
            "eth": "ETH-USD", "ethereum": "ETH-USD",
            "dxy": "DX-Y.NYB", "dollar index": "DX-Y.NYB",
            "sp500": "^GSPC", "s&p": "^GSPC", "snp": "^GSPC",
            "nasdaq": "^IXIC",
            "vix": "^VIX",
        }

        # Check exact match
        if text_lower in keyword_map:
            symbol = keyword_map[text_lower]
            return symbol, SYMBOL_DISPLAY_NAMES.get(symbol, symbol)

        # Check partial match
        for keyword, symbol in keyword_map.items():
            if keyword in text_lower:
                return symbol, SYMBOL_DISPLAY_NAMES.get(symbol, symbol)

        return None, None
