"""
Utility functions untuk formatting angka, tanggal, dan data keuangan.
"""
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict


def format_number(value: Optional[Union[int, float]], decimals: int = 2) -> str:
    """Format angka dengan pemisah ribuan."""
    if value is None:
        return "N/A"
    return f"{value:,.{decimals}f}"


def format_percentage(value: Optional[float], include_sign: bool = True) -> str:
    """Format persentase."""
    if value is None:
        return "N/A"
    if include_sign:
        return f"{value:+.2f}%"
    return f"{value:.2f}%"


def format_price(value: Optional[float], instrument_type: str = "forex") -> str:
    """
    Format harga berdasarkan tipe instrumen.

    Args:
        value: Harga yang akan diformat
        instrument_type: forex, gold, index, crypto, idr
    """
    if value is None:
        return "N/A"

    if instrument_type == "gold" or instrument_type == "futures":
        return f"${value:,.2f}"
    elif instrument_type == "index":
        return f"{value:,.2f} pts"
    elif instrument_type == "crypto":
        return f"${value:,.2f}"
    elif instrument_type == "idr":
        return f"Rp{value:,.0f}"
    elif instrument_type == "jpy":
        return f"¥{value:.3f}"
    else:  # forex
        if value >= 1000:
            return f"{value:,.2f}"
        elif value >= 100:
            return f"{value:.2f}"
        elif value >= 10:
            return f"{value:.3f}"
        elif value >= 1:
            return f"{value:.4f}"
        else:
            return f"{value:.5f}"


def format_change(change: float, as_percentage: bool = True) -> str:
    """Format perubahan harga dengan arrow."""
    arrow = "▲" if change > 0 else "▼" if change < 0 else "◆"
    if as_percentage:
        return f"{arrow} {abs(change):.2f}%"
    return f"{arrow} {abs(change):.4f}"


def format_timestamp(ts: Optional[Union[str, datetime]], fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format timestamp ke string."""
    if ts is None:
        return "N/A"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return ts
    return ts.strftime(fmt)


def format_duration(seconds: int) -> str:
    """Format durasi dalam detik ke string yang mudah dibaca."""
    if seconds < 60:
        return f"{seconds} detik"
    elif seconds < 3600:
        return f"{seconds // 60} menit {seconds % 60} detik"
    elif seconds < 86400:
        return f"{seconds // 3600} jam {(seconds % 3600) // 60} menit"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days} hari {hours} jam"


def format_market_sentiment(sentiment_score: float) -> str:
    """Format sentiment score ke label yang mudah dibaca."""
    if sentiment_score > 0.5:
        return "🟢 Sangat Positif"
    elif sentiment_score > 0.2:
        return "🟢 Positif"
    elif sentiment_score > -0.2:
        return "⚪ Netral"
    elif sentiment_score > -0.5:
        return "🔴 Negatif"
    else:
        return "🔴 Sangat Negatif"


def format_list(items: List[str], bullet: str = "•") -> str:
    """Format list items dengan bullet points."""
    return "\n".join(f"{bullet} {item}" for item in items)


def escape_markdown(text: str) -> str:
    """Escape karakter khusus Markdown untuk Telegram."""
    special_chars = ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def truncate_text(text: str, max_length: int = 400) -> str:
    """Potong teks jika terlalu panjang."""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."
