"""
Chart Generator — RESOLUSI SIMBOL SAJA.

Fitur grafik harga (/chart, tombol menu, quick action) sudah dihapus dari bot,
termasuk ketergantungan matplotlib/mplfinance. Modul ini dipertahankan karena
`get_chart_symbol_from_text()` dan `_get_display_name()` masih dipakai banyak
fitur lain (fast price, /sentiment, /sentimen, resolusi simbol watchlist dll.)
"""

from typing import Optional, Tuple

# Nama display untuk setiap simbol
SYMBOL_DISPLAY_NAMES = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "USDCHF=X": "USD/CHF",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "NZDUSD=X": "NZD/USD",
    "EURGBP=X": "EUR/GBP",
    "GBPJPY=X": "GBP/JPY",
    "EURJPY=X": "EUR/JPY",
    "AUDJPY=X": "AUD/JPY",
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
    Resolusi simbol dari teks user + nama display.

    Dulu juga membuat grafik harga lokal (matplotlib/mplfinance) — fitur
    /chart sudah dihapus, jadi hanya utilitas simbol yang tersisa.
    """

    @staticmethod
    def _get_display_name(symbol: str) -> str:
        """Dapatkan nama display untuk simbol."""
        return SYMBOL_DISPLAY_NAMES.get(symbol, symbol.replace("=X", "").replace("=F", ""))

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
