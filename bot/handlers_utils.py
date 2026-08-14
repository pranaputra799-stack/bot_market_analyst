"""Utilitas murni bot — fungsi & konstanta yang dipindah dari bot/handlers.py
(satu sumber kebenaran; handlers.py me-re-export nama ini untuk kompatibilitas test lama)."""
from telegram.error import BadRequest
from utils.chart_generator import ChartGenerator
from typing import Dict, List, Optional
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
import re
import logging

logger = logging.getLogger(__name__)


TG_MAX_MESSAGE_CHARS = 4000

def split_long_text(text: str, max_len: int = TG_MAX_MESSAGE_CHARS) -> List[str]:
    """
    Pecah teks panjang menjadi beberapa bagian agar tidak melebihi batas
    4096 karakter per pesan Telegram.

    Memotong di batas paragraf, lalu batas baris, agar konten tidak terpotong
    di tengah kalimat. Bagian-bagian yang sudah dipecah tetap utuh (tidak ada
    konten yang dibuang).

    Args:
        text: Teks yang akan dipecah
        max_len: Batas panjang per bagian

    Returns:
        List of text chunks
    """
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    current = ""

    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 <= max_len:
            current = f"{current}\n\n{para}" if current else para
            continue

        # Flush paragraf yang sudah terkumpul
        if current:
            chunks.append(current)
            current = ""

        if len(para) <= max_len:
            current = para
            continue

        # Paragraf sendirian lebih panjang dari max_len: pecah per baris,
        # lalu hard-split bila masih ada baris yang lebih panjang dari max_len.
        buf = ""
        for line in para.split("\n"):
            if len(buf) + len(line) + 1 <= max_len:
                buf = f"{buf}\n{line}" if buf else line
            else:
                if buf:
                    chunks.append(buf)
                while len(line) > max_len:
                    chunks.append(line[:max_len])
                    line = line[max_len:]
                buf = line
        if buf:
            current = buf

    if current:
        chunks.append(current)

    return [c for c in chunks if c]

async def _reply_chunk(message, chunk: str, parse_mode: Optional[str], kwargs: Dict):
    """Kirim satu chunk via reply_text dengan fallback plain text jika Markdown gagal."""
    try:
        return await message.reply_text(chunk, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if any(err in str(e).lower() for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await message.reply_text(chunk, **kwargs_copy)
        raise

async def safe_reply_text(message, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Kirim reply_text dengan aman.
    - Fallback ke plain text jika parse mode gagal.
    - Otomatis pecah jadi beberapa pesan jika melebihi batas 4096 karakter
      (menyebabkan morning brief / analisis panjang tidak terpotong).
    """
    try:
        return await _reply_chunk(message, text, parse_mode, kwargs)
    except BadRequest as e:
        if "too long" in str(e).lower():
            logger.info(f"Message too long ({len(text)} chars), splitting into parts...")
            result = None
            chunks = split_long_text(text)
            for i, chunk in enumerate(chunks):
                chunk_kwargs = kwargs if i == len(chunks) - 1 else _strip_reply_markup(kwargs)
                result = await _reply_chunk(message, chunk, parse_mode, chunk_kwargs)
            return result
        raise

async def _send_chunk(bot, chat_id: int, chunk: str, parse_mode: Optional[str], kwargs: Dict):
    """Kirim satu chunk via send_message dengan fallback plain text jika Markdown gagal."""
    try:
        return await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if any(err in str(e).lower() for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await bot.send_message(chat_id=chat_id, text=chunk, **kwargs_copy)
        raise

async def safe_send_message(bot, chat_id: int, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Kirim send_message dengan aman.
    - Fallback ke plain text jika parse mode gagal.
    - Otomatis pecah jadi beberapa pesan jika melebihi batas 4096 karakter.
    """
    try:
        return await _send_chunk(bot, chat_id, text, parse_mode, kwargs)
    except BadRequest as e:
        if "too long" in str(e).lower():
            logger.info(f"Message too long ({len(text)} chars), splitting into parts...")
            result = None
            for chunk in split_long_text(text):
                result = await _send_chunk(bot, chat_id, chunk, parse_mode, kwargs)
            return result
        raise

async def safe_edit_message_text(query, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Edit message text dengan aman.
    - Fallback ke plain text jika parse mode gagal.
    - Jika teks terlalu panjang untuk di-edit, kirim sebagai pesan balasan baru
      (dipecah bila perlu) agar konten tidak hilang.
    """
    try:
        return await query.edit_message_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return None  # konten sudah sama persis — tidak perlu edit
        if "too long" in msg:
            if query.message is not None:
                logger.info(f"Edit message too long ({len(text)} chars), replying with parts instead...")
                result = None
                for chunk in split_long_text(text):
                    result = await _reply_chunk(query.message, chunk, parse_mode, kwargs)
                return result
            # Tidak ada message untuk di-reply (mis. inline mode) — biarkan error asli
            raise
        if any(err in msg for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await query.edit_message_text(text, **kwargs_copy)
        raise

async def _edit_progress_message(message, text: str, parse_mode: Optional[str] = "Markdown", **kwargs):
    """
    Ganti pesan progress dengan jawaban akhir (edit_text).
    - Fallback plain text jika Markdown gagal.
    - Jika terlalu panjang untuk di-edit, reply berchunk lalu hapus pesan progress.
    """
    try:
        return await message.edit_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return None  # teks sama persis — abaikan
        if "too long" in msg:
            result = None
            chunks = split_long_text(text)
            for i, chunk in enumerate(chunks):
                # Hanya chunk terakhir yang membawa tombol aksi cepat
                chunk_kwargs = kwargs if i == len(chunks) - 1 else _strip_reply_markup(kwargs)
                result = await _reply_chunk(message, chunk, parse_mode, chunk_kwargs)
            try:
                await message.delete()  # bersihkan pesan progress
            except Exception:
                pass
            return result
        if any(err in msg for err in ["parse", "entity", "entities"]):
            logger.warning(f"Markdown parse error: {e}. Retrying without parse_mode.")
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop("parse_mode", None)
            return await message.edit_text(text, **kwargs_copy)
        raise

def _strip_provider_prefix(text: str) -> str:
    """
    Hapus prefix '[via Provider] 🤖' dari response AI (ditambahkan engine
    untuk request tanpa system_override) agar konten analisis bersih.
    """
    if "[via" in text:
        parts = text.split("\n\n", 1)
        return parts[1] if len(parts) > 1 else text
    return text

def strip_markdown_asterisks(text: str) -> str:
    """
    Hapus simbol '*' (markdown bold/italic) dari teks respons AI agar tidak
    tampil mentah di Telegram.

    Asterisk yang diapit angka (mis. perkalian "5*3" atau footnote) dipertahankan
    agar data numerik tidak rusak.
    """
    if not text:
        return text
    # 1) Hapus pasangan markdown: **teks** atau *teks* (sisakan isinya)
    text = re.sub(r"\*{1,2}([^*\n]+?)\*{1,2}", r"\1", text)
    # 2) Buang asterisk tersisa yang tidak berpasangan (markdown rusak),
    #    kecuali yang diapit angka (perkalian/footnote numerik).
    text = re.sub(r"(?<!\d)\*+(?!\d)", "", text)
    return text

_PRICE_QUERY_KEYWORDS = (
    "harga", "price", "berapa", "rate", "kurs", "nilai", "quote",
    "spot", "live price", "harga sekarang", "harga saat ini",
)

_ANALYSIS_EXCLUDE_KEYWORDS = (
    "analisis", "teknikal", "support", "resistance", "rsi", "macd",
    "prediksi", "forecast", "ramalan", "kenapa", "mengapa", "sebab",
    "dampak", "korelasi", "hubungan", "banding", "perbedaan", "vs",
    "berita", "sentimen", "chart", "grafik", "cara", "belajar",
    "apa itu", "pengertian", "definisi", "risiko", "jadwal",
    "mana yang", "naik apa turun", "masih bisa", "akan naik", "akan turun",
)

_LABEL_TO_YAHOO = {
    "XAU/USD (Gold)": "GC=F",
    "XAG/USD (Silver)": "SI=F",
    "BTC/USD (Bitcoin)": "BTC-USD",
    "ETH/USD (Ethereum)": "ETH-USD",
    "DXY (Dollar Index)": "DX-Y.NYB",
    "S&P 500": "^GSPC",
    "NASDAQ": "^IXIC",
    "VIX": "^VIX",
}

def label_to_symbol(label: Optional[str]) -> Optional[str]:
    """Konversi label fokus aset (dari conversation memory) ke simbol Yahoo Finance."""
    if not label:
        return None
    if label in _LABEL_TO_YAHOO:
        return _LABEL_TO_YAHOO[label]
    # Label pair forex "XXX/YYY" → "XXXYYY=X"
    m = re.match(r"^([A-Z]{3})/([A-Z]{3})$", label.strip().upper())
    if m:
        return f"{m.group(1)}{m.group(2)}=X"
    return None

MENU_KEYBOARD_LABELS = [
    ["🥇 Harga Gold", "💱 EUR/USD"],
    ["🌍 Overview Pasar", "📅 Kalender"],
    ["📰 Sentimen Pasar", "🏛️ Data Makro"],
    ["🌅 Morning Brief", "🎯 Prediksi News"],
    ["🔔 Alert Event", "⚙️ Pengaturan"],
    ["❓ Bantuan"],
]

MENU_KEYBOARD_ACTIONS = {
    "🥇 Harga Gold": "gold_price",
    "💱 EUR/USD": "eurusd",
    "🌍 Overview Pasar": "overview",
    "📅 Kalender": "calendar",
    "📰 Sentimen Pasar": "sentiment",
    "🏛️ Data Makro": "macro",
    "🌅 Morning Brief": "morning",
    "🎯 Prediksi News": "prediksi",
    "🔔 Alert Event": "alert_on",
    "⚙️ Pengaturan": "settings",
    "❓ Bantuan": "help",
}

def _menu_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard persistent di bawah kolom input — menu selalu terlihat."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(label) for label in row] for row in MENU_KEYBOARD_LABELS],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Pilih menu atau tanya pasar...",
    )

def _main_menu_inline_keyboard() -> InlineKeyboardMarkup:
    """Menu utama inline (di dalam pesan) — dipakai /start dan tombol 'Kembali'."""
    keyboard = [
        [
            InlineKeyboardButton("🥇 Harga Gold", callback_data="gold_price"),
            InlineKeyboardButton("💱 EUR/USD", callback_data="eurusd"),
        ],
        [
            InlineKeyboardButton("🌍 Overview Pasar", callback_data="overview"),
            InlineKeyboardButton("📅 Kalender", callback_data="calendar"),
        ],
        [
            InlineKeyboardButton("📰 Sentimen Pasar", callback_data="sentiment"),
            InlineKeyboardButton("🏛️ Data Makro", callback_data="macro"),
        ],
        [
            InlineKeyboardButton("🌅 Morning Brief", callback_data="morning"),
            InlineKeyboardButton("🎯 Prediksi News", callback_data="prediksi"),
        ],
        [
            InlineKeyboardButton("🔔 Alert Event", callback_data="alert_on"),
            InlineKeyboardButton("⚙️ Pengaturan", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("❓ Bantuan", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def _quick_action_keyboard(symbol: Optional[str] = None):
    """Keyboard aksi cepat — simbol ter-embed di callback agar tombol lama
    tetap bekerja untuk instrumen yang benar walau konteks sudah berubah."""
    sr_data = f"qa:sr:{symbol}" if symbol else "qa:sr"
    scenario_data = f"qa:scenario:{symbol}" if symbol else "qa:scenario"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 S/R & Target", callback_data=sr_data),
            InlineKeyboardButton("🔮 Skenario", callback_data=scenario_data),
        ],
        [
            InlineKeyboardButton("🧹 Bersihkan Konteks", callback_data="qa:clear"),
        ],
    ])

def _strip_reply_markup(kwargs: Dict) -> Dict:
    """Salin kwargs tanpa reply_markup — untuk chunk pesan selain yang terakhir
    agar keyboard aksi cepat tidak muncul di setiap potongan pesan panjang."""
    return {k: v for k, v in kwargs.items() if k != "reply_markup"}

def detect_fast_price_query(text: str):
    """
    Deteksi pertanyaan harga sederhana yang bisa dijawab INSTAN tanpa AI.

    Contoh yang cocok:
      "berapa harga eurusd?", "harga gold sekarang", "rate usd/jpy",
      "harga bitcoin", "kurs eurusd", "spot eurusd"

    Contoh yang TIDAK cocok (harus lewat pipeline analisis penuh):
      "kenapa gold naik?", "analisis teknikal eurusd", "berapa prediksi gold?"

    Returns:
        Tuple (yahoo_symbol, display_name) atau None jika bukan pertanyaan harga.
    """
    if not text:
        return None
    q = text.lower().strip()

    # Harus mengandung kata kunci harga
    if not any(kw in q for kw in _PRICE_QUERY_KEYWORDS):
        return None
    # Jangan sentuh pertanyaan yang butuh analisis (bukan sekadar cek harga)
    if any(kw in q for kw in _ANALYSIS_EXCLUDE_KEYWORDS):
        return None

    symbol, display_name = ChartGenerator.get_chart_symbol_from_text(q)
    if not symbol:
        return None
    return symbol, display_name

MEMORY_USAGE = (
    "🧠 *RIWAYAT PERCAKAPAN*\n\n"
    "Bot menyimpan beberapa pertukaran terakhir percakapanmu sebagai konteks "
    "agar jawaban follow-up konsisten (tersimpan di database, 24 jam).\n\n"
    "Perintah:\n"
    "`/memory` — lihat riwayat & konteks yang tersimpan\n"
    "`/memory clear` — hapus riwayat sekarang\n\n"
    "🔒 Privasi: riwayat otomatis terhapus setelah 24 jam."
)
