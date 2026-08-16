# 🤖 MarketAI Analyst Bot

Bot Telegram analisis pasar keuangan (Forex, Gold/Emas, dan makroekonomi) dengan **Multi-Agent Analysis System** dan **Multi-AI Provider fallback**.

Dibuat untuk trader retail Indonesia — semua jawaban dalam Bahasa Indonesia, berfokus pada angka, tren, dan implikasi.

## ✨ Fitur

- **Multi-Agent Analysis** 🧠 — Pipeline 7 agent: Research → Signals → Thesis → Contradiction → Scenarios → Confidence → Risk Gates
- **Multi-AI Provider** — OpenRouter (primary, **hanya model gratis** `:free`/`$0`) → Groq → Gemini → Cerebras → Mistral, fallback otomatis saat satu provider down/rate-limit. OpenRouter auto-discover model gratis (`:free`)
- **Multi-Source Data** 📊 — OANDA (real-time utk Forex & Gold) + Yahoo Finance, Alpha Vantage, Finnhub, Exchange Rate API
- **Streaming Harga Real-time** ⚡ — Harga bid/ask LIVE via WebSocket OANDA (daemon thread, reconnect otomatis, fallback otomatis ke REST bila streaming tidak tersedia)
- **Instrumen OANDA Diperluas** 🌐 — selain major pair & Gold/Silver: cross pair (EUR/GBP, GBP/JPY, AUD/JPY, ...), indeks (S&P 500, Dow, Nasdaq, Nikkei), minyak (WTI, Brent), dan crypto (BTC, ETH) — semua real-time bila tersedia di akun
- **Spread Bid/Ask** 💱 — Jawaban harga menampilkan Bid/Ask + spread (dari OANDA real-time)
- **Sentimen Retail** 🧠 — `/sentimen` menampilkan rasio posisi Long/Short trader ritel (OANDA Position Book) + cluster pending order (Order Book) — data unik yang tidak ada di sumber lain
- **Data Makroekonomi** 🏛️ — FRED (resmi & gratis): CPI, NFP, Fed Rate, GDP, dll
- **Kalender Ekonomi** 📅 — Jadwal rilis BLS/Fed real-time via FRED, dikonversi ke WIB, lengkap dengan Forecast/Previous/Actual
- **Morning Brief** 🌅 — Ringkasan pasar otomatis setiap pagi (bisa dilanggan per-user)
- **Alert Event Ekonomi** 🔔 — Digest harian + reminder sebelum event high-impact (NFP, CPI, FOMC)
- **Aftermath Event Analysis** 📰 — Notifikasi otomatis SETELAH event high-impact rilis: angka Actual vs Forecast/Previous, interpretasi arah DXY (US Dollar Index), analisis AI dampak ke Gold & FX, dan penjelasan berita — dedup persisten agar tidak dobel
- **Sentimen Pasar** 🧠 — Skor sentimen berbasis berita (Finnhub + lexicon + LLM)
- **Harga Crypto Real-time** 🪙 — BTC/ETH dari exchange publik (Binance, Coinbase, Kraken, OKX, Bybit, KuCoin) via ccxt **tanpa API key** — harga spot DAN candle OHLCV (chart & analisis teknikal BTC/ETH ikut real-time), Yahoo untuk crypto delayed 15-20 menit
- **Health Endpoint** 💚 — `GET /health` (aiohttp, port `HEALTH_PORT`) untuk uptime monitoring & Docker healthcheck
- **Prompt Evaluation** 🧪 — Scaffolding promptfoo (`promptfoo/`) untuk menguji kualitas prompt & validitas JSON agent lintas provider (dev-time)
- **Error Tracking** (opsional) — Sentry, aktif otomatis jika `SENTRY_DSN` diisi
- **Notifikasi Admin Otomatis** 🔔 — pesan ke `ADMIN_USER_IDS` saat bot online (deploy sukses), setup webhook gagal, tabel Supabase hilang, dan semua AI provider down/pulih — anti silent-fail tanpa perlu buka log panel
- **Alat Edukasi Tanpa AI** 🧰 — `/risk` (position size), `/pivot` (level kunci), `/map` (heatmap instan) — cepat, tanpa biaya token
- **Trading Journal** 📓 — `/journal` mencatat transaksi per user (win rate per pair) — data tersimpan di Supabase
- **Watchlist Personal** 👁️ — `/watchlist` menyimpan daftar pair/instrumen favorit per user; morning brief & `/map watchlist` fokus ke daftarmu (di-cache, hemat token)
- **Trading Plan Mingguan** 📋 — `/plan setup` (alur tanya-jawab: modal → risiko → gaya → pair → jam) lalu `/plan` menghasilkan rencana trading personal: pair layak, entry/SL/TP, R:R, dan ukuran posisi dari modal & risiko (1 call AI/minggu, di-cache)
- **COT Report (CFTC)** 📊 — `/cot` menampilkan posisi institusional (speculative vs commercial/hedger) untuk 32 instrumen: FX, Gold, Oil, BTC, index, treasury (2Y/5Y/10Y/30Y), Fed Funds, SOFR, dll — data gratis CFTC, cache 7 hari + pre-warm otomatis tiap Jumat malam agar instan
- **Kuota Harian Per-User (persisten)** ⏳ — batas pertanyaan/hari (default 30, `USER_DAILY_QUOTA`) agar kuota AI gratis tidak terkuras satu user; tersimpan di Supabase sehingga **tidak reset saat restart/spin-down** free tier
- **Laporan AI Usage** 📊 — `/usage` (admin) + laporan harian otomatis ke admin: token & request per provider, biar tahu kapan mendekati limit gratis
- **Market Session Alerts** 🌏 — notifikasi sesi Sydney/Tokyo/London/New York buka ke subscriber morning brief (tanpa AI)
- **Memory Percakapan** 💬 — Bot mengingat konteks percakapan per-user (±15 menit) sehingga pertanyaan follow-up seperti *"kalau begitu level support-nya di mana?"* tetap dipahami konteksnya

## 🏗️ Arsitektur

```
main.py                  → Entry point (polling / webhook) + scheduler job
bot/
  handlers.py            → Agregator MarketBot (gabungan mixin per domain) + re-export API publik
  handlers_utils.py      → Fungsi & konstanta murni (split teks, keyboard, deteksi query harga)
  scheduler_jobs.py      → Job terjadwal & prediksi news/aftermath + pre-warm cache COT (SchedulerJobsMixin)
  commands_market.py     → Command analisis pasar (sentiment, kalender, overview, risk, pivot, map)
  commands_journal.py    → Trading journal (/journal)
  commands_watchlist.py  → Watchlist personal (/watchlist + /map watchlist + submenu Settings)
  commands_plan.py       → Trading plan mingguan (/plan, /plan setup)
  conversation_plan.py   → ConversationHandler alur tanya-jawab /plan setup
  commands_cot.py        → Laporan COT CFTC (/cot)
  message_flow.py        → Alur pesan (handle_message, fast price, prompt)
  callback_flow.py       → Alur callback tombol inline & keyboard (handle_callback)
  messages.py            → Template pesan & formatter status
analysis/
  director.py            → Orchestrator pipeline multi-agent
  research_agent.py      → Kumpulkan konteks pasar/makro/berita/kalender
  signals.py             → Agregasi sinyal teknikal (trend, momentum, volatilitas, volume)
  indicators.py          → Hitung indikator teknikal lokal dari OHLCV (RSI, MACD, pivot, fib, EMA)
  thesis_agent.py        → Tesis pasar dengan directional bias
  contradiction_agent.py → Deteksi sinyal konflik
  scenarios_agent.py     → Skenario bull/bear/base dengan probabilitas
  confidence_agent.py    → Kalibrasi keyakinan analisis
  risk_gates.py          → Asesmen risiko edukasi
  sentiment.py           → Skor sentimen pasar dari berita
  intent_classifier.py   → Klasifikasi intent (price, teknikal, makro, dll)
  fact_check.py          → Verifikasi deterministik angka jawaban vs data terhitung (anti-halusinasi)
  monitoring.py          → Metrics & statistik pipeline multi-agent
  prompts.py             → Konstanta prompt agent (dimuat dari prompts/*.txt)
ai/
  engine.py              → AI fallback engine multi-provider
  openrouter_client.py   → Auto-discovery model gratis OpenRouter
data/
  market_data.py         → Data harga (OANDA real-time → ccxt crypto → Yahoo → Alpha Vantage → Finnhub)
  ccxt_client.py         → Harga crypto real-time dari exchange publik (tanpa API key, multi-exchange failover)
  oanda_client.py        → Client OANDA v20 API (pricing real-time + candles + position/order book)
  oanda_stream.py        → Streaming harga WebSocket real-time (daemon thread)
  macro_data.py          → Data makro & kalender ekonomi (FRED, Finnhub, jadwal resmi)
  news_data.py           → Berita & sentimen (Finnhub, Marketaux, RSS)
  cache.py               → Cache dua lapis (L1 memori + L2 Supabase) dengan TTL
  database.py            → Supabase REST (opsional): user, subscriber, event alert, news predictions, watchlist, profil /plan, cache COT
  cot.py                 → Parser & fetcher laporan COT CFTC (legacy + TFF, 32 instrumen)
  conversation_memory.py → Memori percakapan per-user (konteks follow-up)
  news_predictions.py    → Store prediksi news XAU/USD (win rate & riwayat)
  http_session.py        → Session requests/aiohttp bersama (connection pooling per-thread)
config/
  settings.py            → Semua konfigurasi dari environment variables
  providers.py           → Daftar provider AI & simbol data (YAHOO_SYMBOLS, OANDA_SYMBOLS, FRED_INDICATORS)
prompts/
  loader.py              → Loader template prompt (single source of truth, CLI preview)
  _agent_defaults.py     → Salinan fallback template agent (dijaga sinkron dengan .txt)
  *.txt                  → Template prompt analisis — edit di sini tanpa ubah kode
utils/
  chart_generator.py     → Resolusi simbol dari teks user (get_chart_symbol_from_text)
  health_server.py       → Endpoint /health (aiohttp daemon thread)
  healthcheck.py         → Script healthcheck Docker (stdlib-only)
  token_budget.py        → Token counting & truncation presisi (tiktoken opsional)
  validators.py          → Sanitasi & validasi input user
promptfoo/               → Scaffolding evaluasi prompt (promptfoo, dev-time)
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
| `morning_brief.txt` | Morning brief harian (placeholder `{WATCHLIST}` untuk brief personal) |
| `trading_plan.txt` | Rencana trading mingguan personal (`/plan`) |
| `cot_interpretation.txt` | Interpretasi AI laporan COT (`/cot`) |
| `director_system.txt` | Orchestrator pipeline multi-agent (Director) |
| `research_system.txt` / `research_analysis_template.txt` | Agent Research |
| `signals_system.txt` | Agent Signals |
| `thesis_system.txt` / `thesis_formulation_template.txt` | Agent Thesis |
| `contradiction_system.txt` / `contradiction_template.txt` | Agent Contradiction |
| `scenarios_system.txt` / `scenarios_template.txt` | Agent Scenarios |
| `confidence_system.txt` / `confidence_template.txt` | Agent Confidence |
| `risk_system.txt` / `risk_template.txt` | Agent Risk Gates |
| `event_aftermath.txt` | Analisis dampak event high-impact (aftermath) |
| `news_prediction.txt` | Prediksi arah emas (XAU/USD) sebelum event rilis |
| `news_prediction_verdict.txt` | Evaluasi benar/salah/flat prediksi news |
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
| `OANDA_API_KEY` | opsi* | **Real-time Forex & Gold** — token akun demo OANDA (https://www.oanda.com/demo-account/) |
| `OANDA_ACCOUNT_ID` | opsi* | ID akun OANDA (kosongkan → auto-detect dari token) |
| `OANDA_ENV` | opsi | `practice` (demo, default) atau `live` |
| `OANDA_PRICE_TTL` | opsi | TTL cache harga OANDA (detik, default `30`) |

*Tanpa `OANDA_API_KEY`, bot memakai Yahoo Finance (delayed 15-20 menit) seperti sebelumnya.
| `SUPABASE_URL` / `SUPABASE_KEY` | opsi | User & subscriber morning brief + cache persisten (L2) |
| `SUPABASE_CACHE_ENABLED` | opsi | Aktifkan cache persisten Supabase (`true`/`false`, default `true`) |
| `CACHE_MAX_ENTRIES` | opsi | Batas entri memory cache (default `5000`) |
| `SENTRY_DSN` | opsi | Error tracking (kosongkan untuk menonaktifkan) |
| `AI_MAX_TOTAL_WAIT_SECONDS` | opsi | Batas waktu total satu permintaan AI (default `60`) |
| `AI_REQUEST_TIMEOUT` | opsi | Timeout per request ke satu AI provider (default `30`) |
| `AI_MIN_INTERVAL_SECONDS` | opsi | Jeda minimum antar request AI (0 = default per-provider; naikkan jika free tier sering 429) |
| `MORNING_BRIEF_CHAT_IDS` | opsi | Chat ID penerima morning brief otomatis |
| `ECONOMIC_ALERT_ENABLED` | opsi | Notifikasi event ekonomi (`true`/`false`) |
| `EVENT_AFTERMATH_ENABLED` | opsi | Analisis aftermath setalah event rilis (`true`/`false`) |
| `EVENT_AFTERMATH_LOOKBACK_HOURS` | opsi | Jendela jam ke belakang untuk laporan aftermath (default `6`) |
| `EVENT_AFTERMATH_CHECK_INTERVAL_MINUTES` | opsi | Interval pengecekan aftermath (default `30`, terpisah dari reminder 15 mnt) — lebih jarang = hemat FRED + AI di hari tanpa news |
| `NEWS_PREDICTION_ENABLED` | opsi | Prediksi arah emas (XAU/USD) sebelum news high-impact (`true`/`false`, default `true`) |
| `NEWS_PREDICTION_LEAD_MINUTES` | opsi | Menit sebelum rilis saat prediksi dikirim (default `5`) |
| `NEWS_PREDICTION_SETTLE_MINUTES` | opsi | Menit setelah rilis sebelum hasil dievaluasi (default `15`) |
| `NEWS_PREDICTION_MIN_MOVE_PCT` | opsi | Ambang pergerakan harga untuk status flat (default `0.05`) |
| `COT_PREWARM_ENABLED` | opsi | Pre-warm cache COT otomatis harian agar `/cot` instan (`true`/`false`, default `true`) |
| `COT_PREWARM_HOUR` / `COT_PREWARM_MINUTE` | opsi | Jam pre-warm COT (zona `MORNING_BRIEF_TIMEZONE`, default `4:00` — setelah rilis CFTC Jumat 15:30 ET / 02:30 WIB) |
| `COT_PREWARM_DAYS` | opsi | Hari pre-warm (ISO weekday `1`=Senin..`7`=Minggu, pisahkan koma; default `1,2,3,4,5,6` = Senin-Sabtu) |
| `COT_PREWARM_MAX_INSTRUMENTS` | opsi | Batas instrumen COT per run pre-warm (0 = semua, default `0`) |
| `COT_PREWARM_SKIP_WITHOUT_DB` | opsi | Skip pre-warm bila Supabase tidak terhubung (hemat resource, default `true`) |
| `HEALTH_ENDPOINT_ENABLED` | opsi | Endpoint `/health` untuk monitoring (`true`/`false`, default `true`) |
| `HEALTH_PORT` | opsi | Port `/health` — beda dari PORT webhook (default `8090`) |
| `CCXT_PRICE_TTL` | opsi | TTL cache harga crypto ccxt dalam detik (default `30`) |
| `CCXT_OHLCV_TTL` | opsi | TTL cache candle OHLCV crypto ccxt dalam detik (default `60`) |
| `MEMORY_MAX_TOKENS_IN_CONTEXT` | opsi | Budget token riwayat percakapan dalam prompt (default `600`) |
| `BOT_RUN_MODE` | opsi | `auto` (default) / `webhook` / `polling` — JustRunMy: `auto` = polling kecuali `WEBHOOK_URL` terisi |

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
(membuat tabel `app_cache`, `users`, `subscribers`, `event_reports`,
`event_alert_subscribers`, `event_alert_notified`, `news_predictions`,
`journal`, `user_daily_usage`, `watchlists`, `user_profiles`, `cot_cache`
+ index + kebijakan RLS). File idempotent — aman dijalankan ulang.
Jika Supabase belum dikonfigurasi / tabel belum dibuat, bot otomatis jatuh ke
mode memory-only tanpa error.

| Data | L1 (memori) | L2 (Supabase) |
|---|---|---|
| Data pasar/makro/berita | ✅ | — |
| Hasil analisis multi-agent | ✅ | — |
| AI response (besar) | ✅ (TTL 10 mnt) | ✅ (persisten) |
| Conversation memory | ✅ (TTL 15 mnt) | ✅ (persisten) |
| Aktivitas user (last_active_at, total_questions) | ✅ (buffer) | ✅ (flush batch tiap 10 mnt) |

## 📊 Statistik & Hemat Token

- **Pemakaian token AI di-track** dari field `usage` response API (per provider)
  dan ditampilkan di `/status` (semua user) dan `/stats` (admin).
- **Aktivitas user** (kapan terakhir aktif + jumlah pertanyaan) di-flush
  **batch** ke tabel `users` tiap 10 menit — numpang job cache cleanup yang
  sudah ada, tanpa request per pesan dan tanpa wake-up tambahan.
- **Admin `/stats`**: token total + per provider, user terdaftar / aktif 24 jam,
  subscriber morning brief & alert event, win rate prediksi news (XAU/USD).

## 📦 Deploy

### JustRunMy (justrunmy.app) — yang kamu pakai

JustRunMy **tidak** meng-inject env var platform atau URL publik secara otomatis
(beda dengan Railway/Render). Karena itu bot memakai mode eksplisit:

| Mode | Cara set | Kapan dipakai |
|---|---|---|
| **Polling** (rekomendasi) | Tidak perlu apa-apa — cukup `TELEGRAM_BOT_TOKEN` | Paling simpel: Telegram yang menghubungi bot, tidak butuh port/URL publik. Default `BOT_RUN_MODE=auto` otomatis memilih ini di JustRunMy (tidak ada deteksi cloud, `WEBHOOK_URL` kosong) |
| **Webhook** | Panel → tambah **HTTPS port** (mapping ke port 8080 container) → set `WEBHOOK_URL=https://<app>.justrunmy.app` + `PORT=8080` + `BOT_RUN_MODE=webhook` | Latensi lebih rendah, dianjurkan untuk production |

**Langkah deploy (git push):**

```bash
# 1. Buat app di justrunmy.app, lalu tambah remote git-nya
#    (JustRunMy menampilkan URL remote git + perintah setup di dashboard)
git remote add jrma https://git.justrunmy.app/<user>/<app>.git
git push jrma main
```

Alternatif: **zip upload** (dashboard menerima arsip; JustRunMy mendeteksi
Dockerfile / requirements.txt dan build otomatis) atau **docker push** image
prebuilt.

**Env var yang wajib/sering diisi di panel JustRunMy:**

- `TELEGRAM_BOT_TOKEN` (wajib) + minimal satu AI key (`OPENROUTER_API_KEY` direkomendasikan)
- `OANDA_API_KEY` (opsional, real-time forex/gold) — lihat bagian setup OANDA
- `SUPABASE_URL` / `SUPABASE_KEY` (opsional, cache persisten)
- `BOT_RUN_MODE` (opsional; default `auto`)
- `WEBHOOK_URL` + `PORT=8080` — **hanya** untuk mode webhook

Catatan: endpoint `/health` berjalan di port 8090 (localhost) — JustRunMy hanya
mengekspos port yang kamu mapping di panel, jadi `/health` dipakai untuk
healthcheck Docker / probe lokal, bukan probe publik.

**Docker healthcheck:** Dockerfile sudah menyertakan `HEALTHCHECK` yang mengecek
`GET http://127.0.0.1:8090/health` (script `utils/healthcheck.py`, stdlib-only).
Bila endpoint dinonaktifkan (`HEALTH_ENDPOINT_ENABLED=false`), script otomatis
fallback ke cek proses `main.py` masih hidup — jadi container tidak pernah
salah ditandai unhealthy karena konfigurasi itu. Platform yang mendukung
(auto-restart saat unhealthy) akan me-restart container yang crash.

### Render (direkomendasikan — free 512MB, always-on dengan keep-alive)

Render meng-inject `RENDER_EXTERNAL_URL` & `PORT` otomatis → `IS_RENDER` terdeteksi
→ bot otomatis jalan mode **webhook** (tidak perlu set `WEBHOOK_URL` manual).

**Blueprint `render.yaml` sudah disiapkan di root repo:**

1. Push repo ke GitHub:

   ```bash
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin master
   ```

2. Render Dashboard → **New → Blueprint** → pilih repo → **Apply**
   (Render membaca `render.yaml`, build Dockerfile, buat service free).

3. Isi secret env di dashboard Render → Environment:
   - `TELEGRAM_BOT_TOKEN` (wajib) + `OPENROUTER_API_KEY` (disarankan)
   - `SUPABASE_URL` / `SUPABASE_KEY` (opsional), `OANDA_API_KEY` (opsional)

4. **Keep-alive (penting):** free tier Render tidur setelah ~15 menit tanpa
   traffic masuk. Buat monitor **UptimeRobot** (gratis) → tipe HTTP(S) → URL
   **`https://<app>.onrender.com/health`** → interval **5 menit**. Endpoint
   `/health` publik (disajikan server webhook aiohttp kita) mengembalikan
   **200** → monitor UP → Render tidak pernah tidur. Tanpa pinger ini, bot
   cold-start ±1 menit saat pesan masuk pertama setelah periode diam.

Catatan:
- `healthCheckPath: /health` di `render.yaml` **aman dipakai** — server
  webhook aiohttp kita menyediakan `GET /health` → 200 (server tornado
  bawaan PTB hanya menerima POST, jadi tidak bisa dipakai untuk health
  check / keep-alive).
- Endpoint `/health` internal tetap jalan di `127.0.0.1:8090` (Docker
  healthcheck & diagnosis lokal).

### Railway / Koyeb

- `IS_CLOUD` terdeteksi otomatis dari env var platform → bot jalan mode **webhook**
- `PORT` & `WEBHOOK_URL` diisi otomatis oleh platform
- Koyeb free: tidur setelah ±1 jam tanpa traffic masuk — pakai webhook +
  pinger keep-alive (sama seperti Render). Railway: tidak ada free tier permanen.

## 🧪 Testing

```bash
cd app/bot-telegram
python -m unittest discover -s tests -v
# atau jika pytest terpasang:
# pytest tests/ -v
```

Test mencakup logika murni (tanpa network): sentiment analyzer, signal engine, split pesan panjang, kalender ekonomi, AI fallback engine (provider di-stub), client ccxt (exchange di-mock), dan payload health endpoint.

### CI (GitHub Actions)

Repo punya workflow CI di `.github/workflows/ci.yml` yang otomatis berjalan di
setiap push/PR (infrastruktur GitHub — **tidak membebani instance Render**):

- **Test** — compile check + seluruh unit test di Python 3.9 & 3.11 (3.11 =
  versi production di Dockerfile)
- **Lint** — `ruff` dengan config di `pyproject.toml` (rules `F`, `E4`, `E7`, `E9`)

### Lint & pre-commit (dev-time, opsional)

```bash
uvx ruff check .            # lint cepat (jalankan tanpa install)
# atau
pip install ruff && ruff check .
```

Mau lint otomatis tiap commit? Install sekali:

```bash
pip install pre-commit && pre-commit install
```

Config ada di `.pre-commit-config.yaml` (ruff dengan auto-fix).

### Evaluasi kualitas prompt (promptfoo)

```bash
cd app/bot-telegram/promptfoo
export OPENROUTER_API_KEY=sk-or-v1-...   # model gratis, biaya $0
npx promptfoo eval && npx promptfoo view
```

Detail lengkap di `promptfoo/README.md`.

### Cek kesehatan bot

```bash
curl http://127.0.0.1:8090/health   # JSON: status, uptime, cache, ai
```

## 📚 Perintah Bot

| Perintah | Fungsi |
|---|---|
| `/start` | Menu utama + **tombol menu di keyboard bawah chat** (reply keyboard persistent — ketuk kapan saja tanpa mengetik perintah) |
| `/help` | Panduan penggunaan |
| `/morning` | Morning Brief hari ini |
| `/subscribe` | Langganan Morning Brief otomatis |
| `/unsubscribe` | Berhenti langganan Morning Brief |
| `/sentiment` | Sentimen pasar berbasis berita (contoh: `/sentiment eurusd`) |
| `/sentimen` | Sentimen retail trader OANDA — Position/Order Book, hanya pair forex (contoh: `/sentimen eurusd`) |
| `/calendar` | Kalender ekonomi high-impact bulan ini |
| `/alert on\|off` | Notifikasi event ekonomi otomatis — digest harian, reminder sebelum rilis, **+ analisis aftermath (dampak ke DXY) + prediksi arah emas** |
| `⚙️ Pengaturan` (menu) | Satu menu untuk semua yang bisa diatur: toggle notifikasi event, langganan morning brief, kelola watchlist, **detail jadwal & statistik pre-warm COT**, & hapus konteks percakapan |
| `/prediksi` | 🎯 Win rate prediksi news (XAU/USD) — total, benar/salah/flat, 10 prediksi terakhir (`/prediksi history` untuk 25) |
| `/overview` | Ringkasan instan semua instrumen utama (tanpa AI) |
| `/map` | 🗺️ Heatmap instan — RSI/trend/% change semua instrumen utama dalam satu pesan (tanpa AI). Varian: `/map watchlist` = heatmap hanya untuk pair di watchlist-mu |
| `/pivot` | 📐 Pivot point & level kunci (support/resistance + Fibonacci) — tanpa AI |
| `/risk` | 📐 Kalkulator ukuran posisi: modal + risiko% + SL pips → lot (tanpa AI) |
| `/journal` | 📓 Trading journal — catat transaksi, win rate per pair, rekap (butuh tabel `journal` di Supabase) |
| `/watchlist` | 👁️ Lihat daftar pair/instrumen favorit + harga terkini (butuh tabel `watchlists`) |
| `/watchlist add <pair>` | 👁️ Tambah instrumen ke watchlist (contoh: `/watchlist add gold`) |
| `/watchlist remove <pair>` | 👁️ Hapus instrumen dari watchlist |
| `/watchlist clear` | 👁️ Kosongkan seluruh watchlist |
| `/plan setup` | 📋 Isi/update profil trading plan via alur **tanya-jawab** (modal → risiko % → gaya → pair favorit → jam). Sekali isi juga didukung: `/plan setup 1000 2 swing XAU/USD,EUR/USD 09:00-16:00` |
| `/plan` | 📋 Generate rencana trading mingguan personal — pair layak, entry/SL/TP, R:R, ukuran posisi (1 call AI/minggu, di-cache) |
| `/plan clear` | 📋 Hapus profil trading plan |
| `/cot <instrumen>` | 📊 Laporan COT (CFTC): posisi net speculative & commercial + perubahan mingguan + interpretasi. 33 instrumen — contoh: `/cot gold`, `/cot eur`, `/cot oil`, `/cot btc`, `/cot dxy`, `/cot dow`, `/cot us2y`, `/cot us5y`, `/cot us10y`, `/cot us30y`, `/cot fed funds`, `/cot sofr`, `/cot sp400`, `/cot russell`, `/cot vix` (butuh tabel `cot_cache`). `/cot` tanpa argumen menampilkan **tombol quick action** 10 instrumen populer (Gold, Euro, Oil, BTC, DXY, 10Y, S&P 500, Corn, SOFR, VIX) |
| `/settings` | ⚙️ Pengaturan bot — toggle alert event, morning brief, kelola **watchlist**, & hapus konteks dalam satu menu |
| `/status` | Status sistem, AI provider, data source, **+ jadwal & statistik pre-warm COT terakhir** |
| `/about` | Informasi bot |
| `/broadcast <pesan>` | 🔒 **Khusus admin** (`ADMIN_USER_IDS`) — preview jumlah penerima, lalu `/broadcast send <pesan>` untuk mengirim pengumuman ke semua subscriber |
| `/usage` | 🔒 **Khusus admin** — laporan pemakaian AI (token & request per provider) |
| `/stats` | 🔒 **Khusus admin** (`ADMIN_USER_IDS`) — statistik lengkap: pemakaian token AI (per provider), user aktif 24 jam, subscriber, win rate prediksi news |
| `/syncmenu` | 🔒 **Khusus admin** — force sinkronisasi menu perintah ke Telegram (hapus command lama yang sudah tidak ada, mis. `/pa`, `/chart`) tanpa redeploy |
| `/cotrefresh [jumlah]` | 🔒 **Khusus admin** — pemicu manual pre-warm cache COT kapan saja (tanpa menunggu jadwal Jumat malam); argumen opsional = batas jumlah instrumen |

## ⚠️ Disclaimer

Bot ini adalah **alat edukasi**, bukan penyedia sinyal trading atau rekomendasi investasi. Harga Forex & Gold diambil real-time dari OANDA (demo); instrumen lain (IDR, DXY, index, crypto) berbasis Yahoo Finance yang bisa delay 15–20 menit. Keputusan trading sepenuhnya tanggung jawab pengguna.
