"""
Konfigurasi utama Bot AI Market Analysis.
Membaca semua environment variables dan menyediakan default values.
"""
import os
from dotenv import load_dotenv

load_dotenv()

def _get_key(key_name: str, default: str = "") -> str:
    """
    Baca key dari environment variable.
    Nilai dimuat dari .env via load_dotenv() (lokal) atau dashboard deploy (cloud).
    """
    return os.getenv(key_name, default)

# ===================== TELEGRAM =====================
TELEGRAM_TOKEN = _get_key("TELEGRAM_BOT_TOKEN", "")

# ===================== AI PROVIDERS =====================
GROQ_API_KEY = _get_key("GROQ_API_KEY", "")
GEMINI_API_KEY = _get_key("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = _get_key("OPENROUTER_API_KEY", "")
CEREBRAS_API_KEY = _get_key("CEREBRAS_API_KEY", "")
MISTRAL_API_KEY = _get_key("MISTRAL_API_KEY", "")

# Fallback order - akan dicoba berurutan sampai ada yang sukses.
# OpenRouter (banyak model free) dijadikan fallback utama setelah Groq
# agar bot tetap responsif saat Groq/Gemini sedang rate-limit / down.
# Bisa di-override via env AI_FALLBACK_ORDER (pisahkan dengan koma).
AI_FALLBACK_ORDER = [
    x.strip() for x in os.getenv(
        "AI_FALLBACK_ORDER", "groq,openrouter,gemini,cerebras,mistral"
    ).split(",") if x.strip()
]

# ===================== DATA PROVIDERS =====================
ALPHA_VANTAGE_KEY = _get_key("ALPHA_VANTAGE_KEY", "")
FRED_API_KEY = _get_key("FRED_API_KEY", "")
FINNHUB_KEY = _get_key("FINNHUB_KEY", "")
MARKETAUX_KEY = _get_key("MARKETAUX_KEY", "")
NEWSAPI_KEY = _get_key("NEWSAPI_KEY", "")
EXCHANGE_RATE_KEY = _get_key("EXCHANGE_RATE_KEY", "")
TWELVEDATA_KEY = _get_key("TWELVEDATA_KEY", "")

# ===================== DATABASE =====================
SUPABASE_URL = _get_key("SUPABASE_URL", "")
SUPABASE_KEY = _get_key("SUPABASE_KEY", "")

# Supabase juga dipakai sebagai cache persisten (L2): AI response & conversation
# memory disimpan di tabel 'app_cache' (lihat migrations/supabase.sql) agar RAM
# proses bot tidak membengkak dan cache tidak hilang saat restart.
# Set false jika ingin cache murni di memori.
SUPABASE_CACHE_ENABLED = os.getenv("SUPABASE_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")

# ===================== CACHE SETTINGS =====================
CACHE_TTL_SECONDS = 300  # 5 menit untuk data harga
CACHE_MACRO_TTL = 3600   # 1 jam untuk data makro
CACHE_NEWS_TTL = 600     # 10 menit untuk berita
CACHE_AI_TTL = 600       # 10 menit untuk response AI (pertanyaan identik)
# Batas maksimal entri di memory cache — entri terlama di-evict saat penuh agar
# RAM proses bot tetap terkendali (0 = tanpa batas).
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "5000"))

# ===================== MORNING BRIEF =====================
MORNING_BRIEF_HOUR = int(os.getenv("MORNING_BRIEF_HOUR", "7"))
MORNING_BRIEF_MINUTE = int(os.getenv("MORNING_BRIEF_MINUTE", "0"))
MORNING_BRIEF_TIMEZONE = os.getenv("MORNING_BRIEF_TIMEZONE", "Asia/Jakarta")
MORNING_BRIEF_CHAT_IDS = os.getenv("MORNING_BRIEF_CHAT_IDS", "")

# ===================== ECONOMIC EVENT ALERTS =====================
# Notifikasi otomatis event ekonomi high-impact (NFP, CPI, FOMC, dll)
ECONOMIC_ALERT_ENABLED = os.getenv("ECONOMIC_ALERT_ENABLED", "true").lower() in ("1", "true", "yes")
# Jam digest harian (daftar event high-impact hari ini)
ECONOMIC_ALERT_DIGEST_HOUR = int(os.getenv("ECONOMIC_ALERT_DIGEST_HOUR", "7"))
ECONOMIC_ALERT_DIGEST_MINUTE = int(os.getenv("ECONOMIC_ALERT_DIGEST_MINUTE", "30"))
# Berapa jam sebelum event dikirimkan reminder
ECONOMIC_ALERT_LEAD_HOURS = int(os.getenv("ECONOMIC_ALERT_LEAD_HOURS", "1"))
# Interval pengecekan reminder (menit)
ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES = int(os.getenv("ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES", "15"))

# ===================== BOT SETTINGS =====================
BOT_USERNAME = os.getenv("BOT_USERNAME", "marketai_analyst_bot")
BOT_NAME = os.getenv("BOT_NAME", "MarketAI Analyst")
ADMIN_USER_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()]

# ===================== ANALYSIS ENGINE =====================
# Multi-Agent Analysis System (from MarketLens)
ENABLE_MULTI_AGENT = os.getenv("ENABLE_MULTI_AGENT", "true").lower() in ("1", "true", "yes")

# ===================== CLOUD DEPLOYMENT =====================
# Railway
RAILWAY_SERVICE_ID = os.getenv("RAILWAY_SERVICE_ID", "")
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
IS_RAILWAY = bool(RAILWAY_SERVICE_ID or RAILWAY_PUBLIC_DOMAIN)

# Render
RENDER = os.getenv("RENDER", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
IS_RENDER = bool(RENDER or RENDER_EXTERNAL_URL)

# Koyeb
KOYEB = os.getenv("KOYEB", os.getenv("KOYEB_SERVICE_ID", ""))
IS_KOYEB = bool(KOYEB)

# Detect any cloud platform
IS_CLOUD = IS_RAILWAY or IS_RENDER or IS_KOYEB

# ===================== WEBHOOK (untuk production) =====================
PORT = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))

# Auto-detect public URL dari berbagai platform
if IS_RAILWAY and RAILWAY_PUBLIC_DOMAIN:
    WEBHOOK_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}"
elif IS_RENDER and RENDER_EXTERNAL_URL:
    WEBHOOK_URL = RENDER_EXTERNAL_URL
else:
    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

WEBHOOK_LISTEN = os.getenv("WEBHOOK_LISTEN", "0.0.0.0")

# Secret token untuk validasi webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", TELEGRAM_TOKEN[:64] if TELEGRAM_TOKEN else "")

# ===================== LOGGING =====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

# Nonaktifkan file logging di cloud (container temporary)
# Catatan: bool("0") bernilai True, jadi parsing manual diperlukan.
LOG_TO_FILE = (
    os.getenv("LOG_TO_FILE", "0").lower() in ("1", "true", "yes")
    if IS_CLOUD
    else True
)
