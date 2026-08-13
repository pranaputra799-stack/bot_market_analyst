"""
Konfigurasi utama Bot AI Market Analysis.
Membaca semua environment variables dan menyediakan default values.
"""
import hashlib
import os
import re

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
# OpenRouter (PRIMARY — auto-discover model GRATIS `:free` / $0) dipakai
# lebih dulu, lalu Groq -> Gemini -> Cerebras -> Mistral sebagai cadangan.
# Bisa di-override via env AI_FALLBACK_ORDER (pisahkan dengan koma).
AI_FALLBACK_ORDER = [
    x.strip() for x in os.getenv(
        "AI_FALLBACK_ORDER", "openrouter,groq,gemini,cerebras,mistral"
    ).split(",") if x.strip()
]

# Batas waktu total maksimal SATU permintaan AI (detik).
# Jika semua provider gagal/rate-limit beruntun, bot berhenti mencoba setelah
# budget ini habis dan langsung mengembalikan pesan error yang ramah — user
# tidak pernah menunggu menit-menit saat semua provider sedang down.
AI_MAX_TOTAL_WAIT_SECONDS = float(os.getenv("AI_MAX_TOTAL_WAIT_SECONDS", "60"))
# Timeout per request ke satu provider (detik). Lebih kecil = fallback lebih cepat
# saat provider hang, tapi berisiko memotong response model lambat.
AI_REQUEST_TIMEOUT = float(os.getenv("AI_REQUEST_TIMEOUT", "30"))
# Jeda minimum antar request ke provider AI yang sama (detik). 0/kosong =
# pakai nilai default per-provider (min_interval_seconds di config/providers.py).
# Nilai > 0 menimpa semuanya — berguna mengetatkan beban rate limit free tier
# TANPA perlu redeploy (cukup set env + restart).
AI_MIN_INTERVAL_SECONDS = float(os.getenv("AI_MIN_INTERVAL_SECONDS", "0") or "0")
# Temperatur AI (kreativitas 0-1). Lebih rendah = jawaban lebih DETERMINISTIK,
# konsisten antar pertanyaan yang sama, dan cenderung faktual (tidak mengarang).
# Default 0.1 — cukup rendah agar analisis stabil, tanpa membuat jawaban kaku.
# Bisa di-override via env AI_TEMPERATURE. Nilai di-clamp ke [0, 1] agar env
# typo (mis. AI_TEMPERATURE=2) tidak menghasilkan output aneh.
AI_TEMPERATURE = max(0.0, min(1.0, float(os.getenv("AI_TEMPERATURE", "0.1"))))
# Batas token output DEFAULT untuk SATU panggilan AI (max_tokens).
# Default 2048 — cukup untuk jawaban panjang tapi mencegah model bertele-tele /
# looping yang membakar token output. Call site yang butuh teks lebih panjang
# (mis. morning brief) men-set max_tokens eksplisit (4096). Env:
# AI_MAX_TOKENS_DEFAULT. Diklem ke rentang 256..8192 agar env typo tidak
# menghasilkan output terpotong parah atau kuota membengkak.
AI_MAX_TOKENS_DEFAULT = int(os.getenv("AI_MAX_TOKENS_DEFAULT", "2048"))
AI_MAX_TOKENS_DEFAULT = max(256, min(AI_MAX_TOKENS_DEFAULT, 8192))
# Response caching OpenRouter (header X-OpenRouter-Cache): payload identik
# (system + prompt + params sama) dikembalikan dari cache server dengan biaya
# $0 dan latensi milidetik — melengkapi cache lokal bot untuk panggilan
# internal yang tidak di-cache di sisi bot (research/thesis/intent).
# Set false jika tidak ingin respons disimpan di sisi OpenRouter.
OPENROUTER_RESPONSE_CACHE = os.getenv("OPENROUTER_RESPONSE_CACHE", "true").lower() in ("1", "true", "yes")

# ===================== DATA PROVIDERS =====================
ALPHA_VANTAGE_KEY = _get_key("ALPHA_VANTAGE_KEY", "")
FRED_API_KEY = _get_key("FRED_API_KEY", "")
FINNHUB_KEY = _get_key("FINNHUB_KEY", "")
MARKETAUX_KEY = _get_key("MARKETAUX_KEY", "")
NEWSAPI_KEY = _get_key("NEWSAPI_KEY", "")
EXCHANGE_RATE_KEY = _get_key("EXCHANGE_RATE_KEY", "")
TWELVEDATA_KEY = _get_key("TWELVEDATA_KEY", "")

# ===================== OANDA (REAL-TIME FOREX & GOLD) =====================
# OANDA v20 API — sumber data REAL-TIME untuk Forex & Gold (XAU/USD).
# Yahoo Finance (sumber lama) delayed 15-20 menit; OANDA demo memberi harga
# streaming real-time gratis (akun demo = virtual money).
#
# Cara dapat kredensial:
# 1) Daftar akun demo gratis: https://www.oanda.com/demo-account/
# 2) Generate API token: https://www.oanda.com/demo-account/tpa/personal_token
#    (login akun demo -> Manage API Access)
# 3) OANDA_ACCOUNT_ID boleh dikosongkan — bot auto-detect akun pertama dari token
#    via GET /v3/accounts.
OANDA_API_KEY = _get_key("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = _get_key("OANDA_ACCOUNT_ID", "")
# "practice" (demo, default) atau "live"
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
# TTL cache harga OANDA (detik). Lebih kecil = lebih realtime (harga segar),
# tapi lebih banyak request API. Default 30 dtk.
OANDA_PRICE_TTL = int(os.getenv("OANDA_PRICE_TTL", "30"))

# ===================== DATABASE =====================
SUPABASE_URL = _get_key("SUPABASE_URL", "")
SUPABASE_KEY = _get_key("SUPABASE_KEY", "")

# Supabase juga dipakai sebagai cache persisten (L2): AI response & conversation
# memory disimpan di tabel 'app_cache' (lihat migrations/supabase.sql) agar RAM
# proses bot tidak membengkak dan cache tidak hilang saat restart.
# Set false jika ingin cache murni di memori.
SUPABASE_CACHE_ENABLED = os.getenv("SUPABASE_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")

# ===================== CACHE SETTINGS =====================
CACHE_TTL_SECONDS = 600  # 10 menit untuk data harga (per-simbol)
# Dulu 300s (5 menit). Dinaikkan agar cache per-simbol Yahoo/OANDA lebih jarang
# expire → miss cache lebih sedikit → beban yfinance turun. Data "overview"
# tetap dianggap segar: get_market_summary punya TTL terpisah (30 menit) dan
# tombol 🔁 Refresh tetap bisa memaksa ambil ulang.
CACHE_MACRO_TTL = 3600   # 1 jam untuk data makro
CACHE_NEWS_TTL = 600     # 10 menit untuk berita
CACHE_AI_TTL = 600       # 10 menit untuk response AI (pertanyaan identik)
# TTL riwayat percakapan per-user (conversation memory) — default 24 jam agar
# follow-up lintas restart tetap punya konteks. Bisa diatur via env
# MEMORY_TTL_SECONDS (mis. 900 = 15 menit seperti versi awal). Diklem ke
# rentang wajar 5 menit s.d. 30 hari untuk cegah salah konfigurasi.
MEMORY_TTL_SECONDS = int(os.getenv("MEMORY_TTL_SECONDS", str(24 * 60 * 60)))
MEMORY_TTL_SECONDS = max(5 * 60, min(MEMORY_TTL_SECONDS, 30 * 24 * 60 * 60))
# Jumlah pertukaran percakapan (Q&A) yang DISIMPAN per user — default 10.
# Naik = konteks follow-up lebih panjang, tapi token prompt ikut bertambah.
# Env: MEMORY_MAX_ENTRIES. Diklem 2 s.d. 30.
MEMORY_MAX_ENTRIES = int(os.getenv("MEMORY_MAX_ENTRIES", "10"))
MEMORY_MAX_ENTRIES = max(2, min(MEMORY_MAX_ENTRIES, 30))
# Berapa pertukaran terakhir yang DIMASUKKAN ke prompt LLM — default 6 (tidak
# boleh melebihi jumlah yang disimpan). Env: MEMORY_MAX_EXCHANGES_IN_CONTEXT.
MEMORY_MAX_EXCHANGES_IN_CONTEXT = int(os.getenv("MEMORY_MAX_EXCHANGES_IN_CONTEXT", "6"))
MEMORY_MAX_EXCHANGES_IN_CONTEXT = max(1, min(MEMORY_MAX_EXCHANGES_IN_CONTEXT, MEMORY_MAX_ENTRIES))
# Budget TOKEN untuk riwayat percakapan yang disuntikkan ke prompt LLM —
# selain batas jumlah pertukaran di atas, total riwayat juga dibatasi token
# agar konteks tidak membengkak (jawaban lama bisa panjang). Hitung akurat
# via tiktoken bila tersedia (fallback estimasi karakter). Env:
# MEMORY_MAX_TOKENS_IN_CONTEXT. Diklem 100..4000.
MEMORY_MAX_TOKENS_IN_CONTEXT = int(os.getenv("MEMORY_MAX_TOKENS_IN_CONTEXT", "600"))
MEMORY_MAX_TOKENS_IN_CONTEXT = max(100, min(MEMORY_MAX_TOKENS_IN_CONTEXT, 4000))
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

# ===================== EVENT AFTERMATH (POST-RELEASE ANALYSIS) =====================
# Notifikasi otomatis SETELAH event high-impact rilis: angka Actual vs Forecast/
# Previous + analisis AI dampaknya ke DXY (US Dollar Index), Gold, dan FX —
# dikirim ke subscriber /alert (dedup persisten di Supabase tabel event_reports).
EVENT_AFTERMATH_ENABLED = os.getenv("EVENT_AFTERMATH_ENABLED", "true").lower() in ("1", "true", "yes")
# Jendela jam ke belakang: event yang rilis dalam N jam terakhir akan dilaporkan
# (setiap job check berjalan sekali per EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES).
EVENT_AFTERMATH_LOOKBACK_HOURS = int(os.getenv("EVENT_AFTERMATH_LOOKBACK_HOURS", "6"))
# Interval pengecekan aftermath (menit) — TERPISAH dari interval reminder.
# Aftermath tidak butuh ketepatan waktu (menganalisis event yang SUDAH rilis
# dalam jendela lookback 6 jam), jadi bisa lebih jarang dari reminder 15 menit
# agar tidak membebani server (FRED + AI hanya dipanggil saat ada event baru).
# Diklem 10..120 menit.
EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES = int(
    os.getenv("EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES", "30")
)
EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES = max(
    10, min(EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES, 120)
)

# ===================== NEWS PREDICTION (XAU/USD) =====================
# Prediksi arah emas (naik/turun) untuk event ekonomi high-impact, dikirim ke
# subscriber /alert. Alur: T-5 menit prediksi dikirim → setelah rilis (T+settle)
# AI menilai benar/salah/flat → riwayat & win rate dilihat via /prediksi.
NEWS_PREDICTION_ENABLED = os.getenv("NEWS_PREDICTION_ENABLED", "true").lower() in ("1", "true", "yes")
# Menit sebelum rilis saat prediksi dikirim ("5 menit sebelum news")
NEWS_PREDICTION_LEAD_MINUTES = int(os.getenv("NEWS_PREDICTION_LEAD_MINUTES", "5"))
# Menit setelah rilis sebelum hasil dievaluasi (beri waktu harga bereaksi)
NEWS_PREDICTION_SETTLE_MINUTES = int(os.getenv("NEWS_PREDICTION_SETTLE_MINUTES", "15"))
# Interval pengecekan (menit) — kecil agar prediksi mendekati T-5 menit
NEWS_PREDICTION_CHECK_INTERVAL_MINUTES = int(os.getenv("NEWS_PREDICTION_CHECK_INTERVAL_MINUTES", "1"))
# Ambang pergerakan harga (persen): di bawah ini AI/aturan menyatakan "flat"
# (pergerakan tidak signifikan — tidak dihitung benar/salah pada win rate)
NEWS_PREDICTION_MIN_MOVE_PCT = float(os.getenv("NEWS_PREDICTION_MIN_MOVE_PCT", "0.05"))
# Maksimum prediksi dibuat/dievaluasi per run (batas anggaran AI)
NEWS_PREDICTION_MAX_PER_RUN = int(os.getenv("NEWS_PREDICTION_MAX_PER_RUN", "2"))

# ===================== PRICE ALERTS =====================
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
# CATATAN: JustRunMy (justrunmy.app) TIDAK menyediakan env var deteksi
# platform otomatis — deteksi JustRunMy dilakukan lewat WEBHOOK_URL terisi
# atau BOT_RUN_MODE=webhook (lihat main.py).
IS_CLOUD = IS_RAILWAY or IS_RENDER or IS_KOYEB

# Mode menjalankan bot: "auto" (default) | "webhook" | "polling".
# - auto: webhook bila IS_CLOUD (Railway/Render/Koyeb) ATAU WEBHOOK_URL terisi
#   (cara deteksi JustRunMy), selain itu polling (cocok untuk dev & JustRunMy
#   tanpa setup port).
# - webhook: paksa webhook (butuh WEBHOOK_URL publik + PORT sesuai mapping
#   port di panel JustRunMy).
# - polling: paksa long polling (jalan tanpa port/URL — rekomendasi JustRunMy
#   bila tidak mau setup HTTPS port).
BOT_RUN_MODE = os.getenv("BOT_RUN_MODE", "auto").strip().lower()

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

# Secret token untuk validasi webhook.
# Telegram hanya mengizinkan karakter A-Z, a-z, 0-9, _ dan - pada secret
# (lihat setWebhook docs). Default LAMA = TELEGRAM_TOKEN[:64] — token berisi
# titik dua ':' → Telegram menolak dengan "Secret token contains unallowed
# characters". Sekarang: (1) env WEBHOOK_SECRET di-sanitasi (karakter tak
# valid dibuang), (2) bila kosong, diturunkan deterministik dari sha256 token
# (hex = 0-9a-f, dijamin valid, 64 karakter). Konsisten dengan validasi header
# X-Telegram-Bot-Api-Secret-Token di build_webhook_app (sama-sama baca konstan ini).
def _safe_webhook_secret(raw: str) -> str:
    """Sisakan hanya karakter yang diizinkan Telegram pada secret token."""
    return re.sub(r"[^A-Za-z0-9_-]", "", raw)[:256]


_env_secret = _safe_webhook_secret(os.getenv("WEBHOOK_SECRET", ""))
WEBHOOK_SECRET = (
    _env_secret
    or hashlib.sha256(TELEGRAM_TOKEN.encode("utf-8")).hexdigest()[:64]
)

# ===================== LOGGING =====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

# ===================== HEALTH ENDPOINT =====================
# Endpoint HTTP /health (aiohttp, port terpisah HEALTH_PORT) untuk uptime
# monitoring / Docker healthcheck. Berjalan di daemon thread sehingga tidak
# mengganggu webhook Telegram (PTB 20.x tidak punya hook untuk route custom).
# Diaktifkan via env HEALTH_ENDPOINT_ENABLED=true.
HEALTH_ENDPOINT_ENABLED = os.getenv("HEALTH_ENDPOINT_ENABLED", "true").lower() in ("1", "true", "yes")
# Port /health — HARUS berbeda dari PORT webhook Telegram. Default 8090.
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8090"))
# Bind address /health. Default 127.0.0.1 (localhost) — aman untuk Docker
# healthcheck (dijalankan di dalam container) & probe lokal tanpa membuka
# endpoint ke publik. Ubah ke 0.0.0.0 hanya jika platform butuh probe remote
# pada port ini.
HEALTH_BIND = os.getenv("HEALTH_BIND", "127.0.0.1")

# File logging default NONAKTIF — log cukup ke stdout (ditangkap panel
# platform: JustRunMy/Railway/Render). Menghindari bot.log tumbuh tak
# terkendali di filesystem container (JustRunMy tidak terdeteksi IS_CLOUD,
# jadi default lama True justru menulis file di tiap start).
# Aktifkan eksplisit bila mau file lokal: LOG_TO_FILE=1.
# Catatan: bool("0") bernilai True, jadi parsing manual diperlukan.
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "0").lower() in ("1", "true", "yes")
