"""
Utility functions untuk validasi input user.
"""
import re
from typing import Optional, Tuple, List


def is_valid_forex_pair(pair: str) -> bool:
    """Cek apakah string adalah pair forex yang valid."""
    pattern = r'^[A-Z]{3}/[A-Z]{3}$'
    major_pairs = [
        "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF",
        "AUD/USD", "USD/CAD", "NZD/USD", "USD/IDR",
        "EUR/JPY", "GBP/JPY", "EUR/GBP", "AUD/JPY",
    ]
    return re.match(pattern, pair.upper()) is not None or pair.upper() in major_pairs


def is_valid_amount(amount: str) -> bool:
    """Cek apakah string adalah angka yang valid."""
    try:
        value = float(amount.replace(",", "").replace(" ", ""))
        return value > 0
    except (ValueError, AttributeError):
        return False


def sanitize_text(text: str, max_length: int = 500) -> str:
    """Bersihkan dan batasi panjang teks input user."""
    # Ganti karakter kontrol dengan SPASI (bukan dihapus) agar kata tidak
    # menempel: "gold\nanalysis" → "gold analysis", bukan "goldanalysis".
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    # Rapatkan whitespace ganda (sisa newline/tab yang sudah jadi spasi)
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Batasi panjang
    return cleaned[:max_length].strip()


def extract_forex_pairs(text: str) -> List[str]:
    """Ekstrak pair forex dari teks."""
    pattern = r'([A-Z]{3}/[A-Z]{3})'
    found = re.findall(pattern, text.upper())
    valid_pairs = []
    for pair in found:
        if is_valid_forex_pair(pair):
            valid_pairs.append(pair)
    return valid_pairs


def detect_query_type(text: str) -> str:
    """
    Deteksi tipe pertanyaan user.

    Returns: 'price', 'technical', 'fundamental', 'macro', 'news', 'correlation', 'general'
    """
    text_lower = text.lower()

    # Price query
    price_keywords = ["harga", "price", "rate", "kurs", "berapa", "nilai", "quote"]
    if any(kw in text_lower for kw in price_keywords):
        # Check if followed by forex pair
        if extract_forex_pairs(text):
            return "price"

    # Technical analysis
    technical_keywords = [
        "teknikal", "technical", "support", "resistance", "rsi", "macd",
        "trend", "trendline", "chart", "pattern", "bollinger",
    ]
    if any(kw in text_lower for kw in technical_keywords):
        return "technical"

    # Fundamental / Macro
    macro_keywords = [
        "nfp", "cpi", "inflasi", "inflation", "fed", "fomc", "gdp",
        "unemployment", "jobless", "tenaga kerja", "pengangguran",
        "suku bunga", "interest rate", "makro", "macro",
    ]
    if any(kw in text_lower for kw in macro_keywords):
        return "fundamental"

    # News / Sentiment
    news_keywords = ["berita", "news", "sentimen", "sentiment", "headline"]
    if any(kw in text_lower for kw in news_keywords):
        return "news"

    # Correlation
    correlation_keywords = ["korelasi", "correlation", "hubungan", "relationship", "dampak"]
    if any(kw in text_lower for kw in correlation_keywords):
        return "correlation"

    return "general"


def validate_chat_id(chat_id: str) -> Optional[int]:
    """Validasi dan konversi chat ID."""
    try:
        return int(chat_id.strip())
    except (ValueError, AttributeError):
        return None
