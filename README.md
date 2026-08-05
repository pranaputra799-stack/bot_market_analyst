# 🤖 MarketAI Analyst Bot

Bot Telegram analisis pasar keuangan (Forex, Gold/Emas, dan makroekonomi) dengan **Multi-Agent Analysis System** dan **Multi-AI Provider fallback**.

Dibuat untuk trader retail Indonesia — semua jawaban dalam Bahasa Indonesia, berfokus pada angka, tren, dan implikasi.

## ✨ Fitur

- **Multi-Agent Analysis** 🧠 — Pipeline 7 agent: Research → Signals → Thesis → Contradiction → Scenarios → Confidence → Risk Gates
- **Multi-AI Provider** — Groq (primary) → OpenRouter → Gemini → Cerebras → Mistral, fallback otomatis saat satu provider down/rate-limit. OpenRouter auto-discover model gratis (`:free`)
- **Multi-Source Data** 📊 — Yahoo Finance, Alpha Vantage, Finnhub, Exchange Rate API
- **Data Makroekonomi** 🏛️ — FRED (resmi & gratis): CPI, NFP, Fed Rate, GDP, dll
- **Kalender Ekonomi** 📅 — Jadwal rilis BLS/Fed real-time via FRED, dikonversi ke WIB, lengkap dengan Forecast/Previous/Actual
- **Morning Brief** 🌅 — Ringkasan pasar otomatis setiap pagi (bisa dilanggan per-user)
- **Alert Event Ekonomi** 🔔 — Digest harian + reminder sebelum event high-impact (NFP, CPI, FOMC)
- **Sentimen Pasar** 🧠 — Skor sentimen berbasis berita (Finnhub + lexicon + LLM)
- **Grafik Harga Lokal** 📈 — Candlestick/line chart digambar langsung di server (matplotlib), tanpa layanan eksternal
- **Error Tracking** (opsional) — Sentry, aktif otomatis jika `SENTRY_DSN` diisi

## 🏗️ Arsitektur

```
main.py                  → Entry point (polling / webhook) + scheduler job
bot/
  handlers.py            → Handler perintah & pesan Telegram
  messages.py            → Template pesan & formatter status
analysis/
  director.py            → Orchestrator pipeline multi-agent
  research_agent.py      → Kumpulkan konteks pasar/makro/berita/kalender
  signals.py             → Agregasi sinyal teknikal (trend, momentum, volatilitas, volume)
  thesis_agent.py        → Tesis pasar dengan directional bias
  contradiction_agent.py → Deteksi sinyal konflik
  scenarios_agent.py     → Skenario bull/bear/base dengan probabilitas
  confidence_agent.py    → Kalibrasi keyakinan analisis
  risk_gates.py          → Asesmen risiko edukasi
  sentiment.py           → Skor sentimen pasar dari berita
  intent_classifier.py   → Klasifikasi intent (price, teknikal, makro, dll)
ai/
  engine.py              → AI fallback engine multi-provider
  openrouter_client.py   → Auto-discovery model gratis OpenRouter
data/
  market_data.py         → Data harga (Yahoo → Alpha Vantage → Finnhub)
  macro_data.py          → Data makro & kalender ekonomi (FRED, Finnhub, jadwal resmi)
  news_data.py           → Berita & sentimen (Finnhub, Marketaux, RSS)
  cache.py               → In-memory cache dengan TTL
  database.py            → Supabase REST (opsional): user & subscriber
config/
  settings.py            → Semua konfigurasi dari environment variables
utils/
  chart_generator.py     → Chart lokal (matplotlib, dark theme)
tests/                   → Unit tests (unittest / pytest-compatible)
```

## 🚀 Setup Lokal

```bash
# 1. Clone & masuk direktori
cd app/bot-telegram

# 2. Buat virtual environment & install dependency
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 3. Konfigurasi
cp .env.example .env
# Isi minimal: TELEGRAM_BOT_TOKEN + minimal 1 API key AI (Groq direkomendasikan)

# 4. Jalankan (polling untuk development)
python main.py
```

### Environment variables penting

| Variabel | Wajib | Keterangan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token dari @BotFather |
| `GROQ_API_KEY` | opsi | AI primary — https://console.groq.com/keys |
| `OPENROUTER_API_KEY` | opsi | Fallback, banyak model gratis — https://openrouter.ai/keys |
| `GEMINI_API_KEY` | opsi | Fallback — https://aistudio.google.com/app/apikey |
| `FRED_API_KEY` | opsi | Kalender ekonomi real-time resmi — https://fred.stlouisfed.org |
| `FINNHUB_KEY` | opsi | Berita & sentimen |
| `SUPABASE_URL` / `SUPABASE_KEY` | opsi | Penyimpanan user & subscriber morning brief |
| `SENTRY_DSN` | opsi | Error tracking (kosongkan untuk menonaktifkan) |
| `MORNING_BRIEF_CHAT_IDS` | opsi | Chat ID penerima morning brief otomatis |
| `ECONOMIC_ALERT_ENABLED` | opsi | Notifikasi event ekonomi (`true`/`false`) |

Lihat `.env.example` untuk daftar lengkap.

## 📦 Deploy

**Docker (justrunmy / Railway / Render):**

```bash
# Build & push ke registry Anda, atau
git push origin deploy   # justrunmy: otomatis build Dockerfile + restart app
```

- `IS_CLOUD` terdeteksi otomatis (Railway/Render/Koyeb) → bot jalan dalam mode **webhook**
- `PORT` diisi otomatis oleh platform
- `WEBHOOK_URL` auto-detect dari platform

## 🧪 Testing

```bash
cd app/bot-telegram
python -m unittest discover -s tests -v
# atau jika pytest terpasang:
# pytest tests/ -v
```

Test mencakup logika murni (tanpa network): sentiment analyzer, signal engine, split pesan panjang, kalender ekonomi, dan AI fallback engine (provider di-stub).

## 📚 Perintah Bot

| Perintah | Fungsi |
|---|---|
| `/start` | Menu utama & tombol aksi cepat |
| `/help` | Panduan penggunaan |
| `/morning` | Morning Brief hari ini |
| `/subscribe` | Langganan Morning Brief otomatis |
| `/unsubscribe` | Berhenti langganan Morning Brief |
| `/sentiment` | Sentimen pasar (contoh: `/sentiment eurusd`) |
| `/calendar` | Kalender ekonomi high-impact bulan ini |
| `/alert on\|off` | Notifikasi event ekonomi otomatis |
| `/chart <simbol>` | Grafik harga (contoh: `/chart gold`, `/chart eurusd`) |
| `/status` | Status sistem, AI provider, dan data source |
| `/about` | Informasi bot |

## ⚠️ Disclaimer

Bot ini adalah **alat edukasi**, bukan penyedia sinyal trading atau rekomendasi investasi. Semua analisis berbasis data publik yang bisa delay 15–20 menit. Keputusan trading sepenuhnya tanggung jawab pengguna.
