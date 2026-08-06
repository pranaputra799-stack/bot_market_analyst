# 🤖 MarketAI Analyst Bot

Bot Telegram analisis pasar keuangan (Forex, Gold/Emas, dan makroekonomi) dengan **Multi-Agent Analysis System** dan **Multi-AI Provider fallback**.

Dibuat untuk trader retail Indonesia — semua jawaban dalam Bahasa Indonesia, berfokus pada angka, tren, dan implikasi.

## ✨ Fitur

- **Multi-Agent Analysis** 🧠 — Pipeline 7 agent: Research → Signals → Thesis → Contradiction → Scenarios → Confidence → Risk Gates
- **Multi-AI Provider** — OpenRouter (primary, **hanya model gratis** `:free`/`$0`) → Groq → Gemini → Cerebras → Mistral, fallback otomatis saat satu provider down/rate-limit. OpenRouter auto-discover model gratis (`:free`)
- **Multi-Source Data** 📊 — Yahoo Finance, Alpha Vantage, Finnhub, Exchange Rate API
- **Data Makroekonomi** 🏛️ — FRED (resmi & gratis): CPI, NFP, Fed Rate, GDP, dll
- **Kalender Ekonomi** 📅 — Jadwal rilis BLS/Fed real-time via FRED, dikonversi ke WIB, lengkap dengan Forecast/Previous/Actual
- **Morning Brief** 🌅 — Ringkasan pasar otomatis setiap pagi (bisa dilanggan per-user)
- **Alert Event Ekonomi** 🔔 — Digest harian + reminder sebelum event high-impact (NFP, CPI, FOMC)
- **Alert Harga** 🎯 — Pasang target harga per instrumen (`/pa eurusd 1.0900`), bot mengirim notifikasi saat tersentuh
- **Sentimen Pasar** 🧠 — Skor sentimen berbasis berita (Finnhub + lexicon + LLM)
- **Grafik Harga Lokal** 📈 — Candlestick/line chart digambar langsung di server (matplotlib), tanpa layanan eksternal
- **Error Tracking** (opsional) — Sentry, aktif otomatis jika `SENTRY_DSN` diisi
- **Memory Percakapan** 💬 — Bot mengingat konteks percakapan per-user (±15 menit) sehingga pertanyaan follow-up seperti *"kalau begitu level support-nya di mana?"* tetap dipahami konteksnya

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
prompts/
  loader.py              → Loader template prompt (single source of truth)
  *.txt                  → Template prompt analisis — edit di sini tanpa ubah kode
utils/
  chart_generator.py     → Chart lokal (matplotlib, dark theme)
tests/                   → Unit tests (unittest / pytest-compatible)
```

## 📝 Prompts — Single Source of Truth

Semua template prompt bot disimpan sebagai file `.txt` di folder `prompts/` — termasuk
prompt agent multi-agent (sebelumnya inline di `analysis/prompts.py`):

| File | Dipakai untuk |
|---|---|
| `market_analysis.txt` | Analisis pasar/teknikal (path legacy) |
| `technical_analysis.txt` | Analisis korelasi antar instrumen (DXY vs Gold vs FX) |
| `macro_explanation.txt` | Penjelasan data makroekonomi (CPI, NFP, Fed, GDP, dll) |
| `morning_brief.txt` | Morning brief harian |
| `director_system.txt` | Orchestrator pipeline multi-agent (Director) |
| `research_system.txt` / `research_analysis_template.txt` | Agent Research |
| `signals_system.txt` | Agent Signals |
| `thesis_system.txt` / `thesis_formulation_template.txt` | Agent Thesis |
| `contradiction_system.txt` / `contradiction_template.txt` | Agent Contradiction |
| `scenarios_system.txt` / `scenarios_template.txt` | Agent Scenarios |
| `confidence_system.txt` / `confidence_template.txt` | Agent Confidence |
| `risk_system.txt` / `risk_template.txt` | Agent Risk Gates |
| `final_synthesis_template.txt` | Sintesis jawaban akhir multi-agent |
| `engine_system.txt` | System prompt default AI engine |

**Edit file `.txt` → perilaku bot berubah tanpa mengubah kode** (restart bot, atau
panggil `prompts.loader.reload_prompts()` di runtime untuk dev hot-reload).
Placeholder `{NAMA}` diisi otomatis oleh `prompts/loader.py`; jika sebuah file
hilang/tidak terbaca, bot otomatis memakai template bawaan sebagai fallback
sehingga tetap berjalan normal. Catatan: template agent memakai kurung kurawal
ganda (`{{ }}`) untuk contoh skema JSON — biarkan apa adanya saat mengedit.

### Preview prompt (dev CLI)

```bash
cd app/bot-telegram
python -m prompts.loader --list                        # daftar semua template
python -m prompts.loader --show market_analysis       # template mentah (raw)
python -m prompts.loader --show market_analysis --sample   # terisi data contoh
python -m prompts.loader --show morning_brief --sample --data DATE="Kamis, 07 Agu"
```

`--sample` merender template dengan data contoh (`prompts.loader.SAMPLE_DATA`),
`--data KEY=VALUE` menimpa placeholder tertentu (bisa diulang). Untuk system
prompt agent, preview sama persis dengan output produksi (termasuk timestamp).

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
# Isi minimal: TELEGRAM_BOT_TOKEN + OPENROUTER_API_KEY (primary, model gratis)
# Set data policy OpenRouter ke "Allow all" agar semua model free bisa dipakai
# https://openrouter.ai/settings/privacy

# 4. Jalankan (polling untuk development)
python main.py
```

### Environment variables penting

| Variabel | Wajib | Keterangan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token dari @BotFather |
| `OPENROUTER_API_KEY` | ✅ (recommended) | AI primary — hanya model gratis (`:free`/`$0`) — https://openrouter.ai/keys |
| `GROQ_API_KEY` | opsi | Fallback — https://console.groq.com/keys |
| `GEMINI_API_KEY` | opsi | Fallback — https://aistudio.google.com/app/apikey |
| `FRED_API_KEY` | opsi | Kalender ekonomi real-time resmi — https://fred.stlouisfed.org |
| `FINNHUB_KEY` | opsi | Berita & sentimen |
| `SUPABASE_URL` / `SUPABASE_KEY` | opsi | User & subscriber morning brief + cache persisten (L2) |
| `SUPABASE_CACHE_ENABLED` | opsi | Aktifkan cache persisten Supabase (`true`/`false`, default `true`) |
| `CACHE_MAX_ENTRIES` | opsi | Batas entri memory cache (default `5000`) |
| `SENTRY_DSN` | opsi | Error tracking (kosongkan untuk menonaktifkan) |
| `AI_MAX_TOTAL_WAIT_SECONDS` | opsi | Batas waktu total satu permintaan AI (default `60`) |
| `AI_REQUEST_TIMEOUT` | opsi | Timeout per request ke satu AI provider (default `30`) |
| `MORNING_BRIEF_CHAT_IDS` | opsi | Chat ID penerima morning brief otomatis |
| `ECONOMIC_ALERT_ENABLED` | opsi | Notifikasi event ekonomi (`true`/`false`) |

Lihat `.env.example` untuk daftar lengkap.

## 🗄️ Cache Persisten di Supabase (anti-RAM membengkak)

Bot memakai cache **dua lapis (hybrid)**:

- **L1 — Memory** (cepat): semua data pasar, berita, makro, hasil analisis.
  Dibatasi `CACHE_MAX_ENTRIES` entri (FIFO eviction) dan dibersihkan otomatis
  tiap 10 menit oleh job scheduler — RAM proses dijamin tidak membengkak.
- **L2 — Supabase** (persisten): AI response & conversation memory disimpan di
  tabel `app_cache` secara background (tidak memblokir request). Cache bertahan
  lintas restart dan tidak menambah beban RAM.

**Setup sekali saja** — jalankan `migrations/supabase.sql` di Supabase SQL Editor
(membuat tabel `app_cache`, `users`, `subscribers` + index + kebijakan RLS).
Jika Supabase belum dikonfigurasi / tabel belum dibuat, bot otomatis jatuh ke
mode memory-only tanpa error.

| Data | L1 (memori) | L2 (Supabase) |
|---|---|---|
| Data pasar/makro/berita | ✅ | — |
| Hasil analisis multi-agent | ✅ | — |
| AI response (besar) | ✅ (TTL 10 mnt) | ✅ (persisten) |
| Conversation memory | ✅ (TTL 15 mnt) | ✅ (persisten) |

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
| `/pa <simbol> <harga>` | 🎯 Alert harga — notifikasi saat harga menyentuh target (contoh: `/pa eurusd 1.0900`; kelola: `/pa list`, `/pa del <id>`) |
| `/chart <simbol>` | Grafik harga (contoh: `/chart gold`, `/chart eurusd`) |
| `/overview` | Ringkasan instan semua instrumen utama (tanpa AI) |
| `/status` | Status sistem, AI provider, dan data source |
| `/about` | Informasi bot |

## ⚠️ Disclaimer

Bot ini adalah **alat edukasi**, bukan penyedia sinyal trading atau rekomendasi investasi. Semua analisis berbasis data publik yang bisa delay 15–20 menit. Keputusan trading sepenuhnya tanggung jawab pengguna.
