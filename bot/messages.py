"""
Message templates untuk bot Telegram.
Semua format balasan bot ada di sini untuk konsistensi.
"""
import time
from datetime import datetime
from typing import Dict, Optional


# Cache TTL hasil get_data_sources_status (detik): status data source melibatkan
# panggilan jaringan (Yahoo/OANDA) yang bisa lambat — /status berulang tidak
# boleh membayar network tiap kali. 120s = cukup segar untuk monitoring.
_DATA_STATUS_CACHE: Dict[str, object] = {"ts": 0.0, "value": ""}
_DATA_STATUS_TTL = 120.0


# ===================== WELCOME & HELP =====================

WELCOME_MESSAGE = """
🎯 *Selamat datang di MarketAI Analyst!*

Asisten pasar *real-time* untuk trader Indonesia 🇮🇩 — harga Forex & Gold *live* dari OANDA, dianalisis oleh 7-agent AI 🧠.

📊 *Coba langsung:*
• 💱 Tanya harga — "harga gold sekarang"
• 📈 Analisis penuh — "analisis teknikal EUR/USD"
• 🏛️ Data makro — CPI, NFP, Fed Rate
• 🧠 Sentimen retail trader — /sentimen
• 👀 Watchlist + riwayat harga — /watch, /riwayat
• 📅 Kalender ekonomi high-impact — /calendar

🔔 *Notifikasi otomatis:*
• 🌅 Morning Brief setiap pagi — /subscribe
• 📰 Analisis dampak event ke DXY — /alert on
• 🎯 Alert harga target — /pa eurusd 1.0900

👇 *Ketuk tombol di bawah* untuk mulai — atau langsung kirim pertanyaan apa pun.

⚠️ Analisis edukasi, *bukan* sinyal trading atau rekomendasi investasi. Keputusan trading sepenuhnya tanggung jawab Anda.
"""

HELP_MESSAGE = """
📚 *BANTUAN & PANDUAN*

💬 *Cara pakai:* kirim pertanyaan pasar apa pun — bot menjawab dengan data real-time.

🔍 *Topik yang bisa ditanyakan:*

*Forex* 💱
• Harga & analisis EUR/USD, GBP/USD, USD/JPY, dll
• Support/resistance & prediksi pergerakan

*Gold & Emas* 🥇
• Harga gold real-time, faktor penggerak, korelasi DXY

*Makroekonomi* 🏛️
• NFP, CPI/Inflasi, Fed Rate, GDP, Unemployment

*Berita & Sentimen* 📰
• Berita terkini, sentimen pasar, sentimen retail OANDA

📅 *Kalender Ekonomi* — rilis high-impact bulan ini + Forecast/Previous

⚙️ *Perintah:*
/start - 🎯 Menu utama
/help - 📚 Bantuan ini
/morning - 🌅 Morning Brief harian
/sentiment - 🧠 Sentimen pasar (contoh: /sentiment eurusd)
/sentimen - 🧠 Sentimen retail OANDA — Position/Order Book
/watch - 👀 Watchlist instrumen persisten
/riwayat - 📜 Riwayat harga tersimpan tiap 30 mnt
/calendar - 📅 Kalender ekonomi high-impact
/aftermath - 🎯 Analisis dampak event (contoh: /aftermath nfp)
/overview - 🌍 Overview semua instrumen (instan)
/alert - 🔔 Notifikasi event + analisis aftermath DXY
/pa - 🎯 Alert harga (contoh: /pa eurusd 1.0900)
/chart - 📈 Grafik harga (contoh: /chart gold)
/subscribe - 🔔 Langganan Morning Brief
/unsubscribe - 🔕 Berhenti langganan
/status - ✅ Status sistem & API
/clear - 🧹 Bersihkan konteks percakapan
/memory - 🧠 Lihat & hapus riwayat percakapan (24 jam)
/about - ℹ️ Tentang bot

⚠️ Analisis edukasi, *bukan* sinyal trading atau rekomendasi investasi. Keputusan trading sepenuhnya tanggung jawab Anda.
"""

ABOUT_MESSAGE = """
🤖 *Tentang MarketAI Analyst*

*Versi:* 1.0
*Platform:* Python + Telegram Bot API
*Tanggal:* Juli 2026

*Fitur Utama:*
• ✅ Multi-AI Provider (OpenRouter primary — model gratis, Groq, Gemini, Cerebras, Mistral)
• ✅ Multi-Source Data (OANDA real-time utk Forex & Gold + Yahoo Finance, Alpha Vantage, Finnhub)
• ✅ Data Makroekonomi (FRED, World Bank)
• ✅ Berita & Sentimen Pasar
• ✅ Morning Brief Harian
• ✅ 100% Gratis (open source)

*Prinsip:*
Bot ini adalah alat edukasi untuk memahami dinamika pasar global. Bukan untuk eksekusi trading.

*Teknologi:*
• python-telegram-bot 20.x
• OANDA v20 API (real-time) + yfinance untuk data pasar
• Multi-AI fallback engine
• In-memory caching

*Sumber Terbuka:*
Kode bot ini terbuka untuk dipelajari dan dikembangkan.

⚠️ *Disclaimer:* Semua analisis bersifat edukasi. Bukan rekomendasi investasi atau trading.
"""

STATUS_MESSAGE_TEMPLATE = """
✅ *STATUS SISTEM*

🤖 *Bot Status:* {bot_status}
⏱ *Uptime:* {uptime}
📊 *Total Pertanyaan Dijawab:* {total_questions}

🔌 *AI Providers:*
{ai_providers_status}

📡 *Data Sources:*
{data_sources_status}

🧠 *Analysis Engine:*
{analysis_engine_status}

💾 *Cache:* {cache_stats}

📅 *Waktu (WIB):* {server_time}
"""


def get_ai_providers_status(ai_engine) -> str:
    """Format status AI providers.

    Menggunakan statistik pemakaian saja — TANPA tes koneksi live per provider.
    Test live memanggil API tiap provider (timeout 30s x 5 provider) sehingga
    perintah /status bisa hang berlarut-larut saat provider sedang down.
    """
    stats = ai_engine.get_stats()
    degraded = set(stats.get("degraded_providers", []))
    lines = []
    for provider in stats.get("available_providers", []):
        name = stats.get("provider_names", {}).get(provider, provider)
        usage = stats.get("provider_usage", {}).get(provider, 0)
        # Tidak ada live test: cukup tampilkan konfigurasi & pemakaian.
        if provider in degraded:
            icon = "⚠️"  # baru kena rate-limit (cooldown)
        else:
            icon = "✅" if usage > 0 else "🟡"
        # Pemakaian token per provider (dari usage API — hemat token terlihat).
        tok = stats.get("usage", {}).get("by_provider", {}).get(provider, {})
        tok_total = (tok.get("prompt_tokens") or 0) + (tok.get("completion_tokens") or 0)
        tok_part = f" · {tok_total:,} token" if tok_total else ""
        lines.append(f"  {icon} {name}: {usage}x dipakai{tok_part}")
    if degraded:
        lines.append("")
        lines.append(f"  ⚠️ Rate-limited sementara: {', '.join(sorted(degraded))}")
    if not lines:
        lines.append("  ❌ Tidak ada provider terkonfigurasi")
    # Ringkasan total token (budget tracking ala LiteLLM)
    tot = stats.get("usage", {}) or {}
    if (tot.get("total_tokens") or 0) > 0:
        lines.append("")
        lines.append(
            f"  💰 Total token: {tot.get('total_tokens', 0):,} "
            f"(input {tot.get('prompt_tokens', 0):,} / output {tot.get('completion_tokens', 0):,})"
        )
    return "\n".join(lines)


def get_analysis_engine_status() -> str:
    """Format status analysis engine."""
    from analysis.monitoring import metrics
    try:
        report = metrics.get_report()
        lines = [
            f"  • Multi-Agent: {'✅ AKTIF' if report['total_analyses'] > 0 else '✅ SIAP'}",
        ]
        if report['total_analyses'] > 0:
            lines.append(f"  • Total analisis: {report['total_analyses']}")
            lines.append(f"  • Rata-rata: {report['avg_duration_ms']:.0f}ms")
            lines.append(f"  • Cache hits: {report['total_cache_hits']}")

            # Agent stats
            agent_stats = report.get('agent_performance', {})
            if agent_stats:
                agent_strs = []
                for agent, stats in agent_stats.items():
                    agent_display = agent.replace("_", " ").title()
                    agent_strs.append(f"{agent_display}: {stats['total_calls']}x")
                lines.append(f"  • Agents: {' | '.join(agent_strs)}")
        else:
            lines.append("  • Menunggu analisis pertama...")

        return "\n".join(lines)
    except Exception:
        return "  • ⬜ Multi-Agent: Tidak tersedia"


def get_data_sources_status(market_data, macro_data, news_fetcher) -> str:
    """Format status data sources (di-cache 120s — lihat _DATA_STATUS_CACHE)."""
    now = time.time()
    if now - float(_DATA_STATUS_CACHE["ts"]) < _DATA_STATUS_TTL:
        return str(_DATA_STATUS_CACHE["value"])
    lines = []

    # OANDA (real-time forex & gold) — get_yahoo_data otomatis memakai OANDA
    # untuk EURUSD=X bila terkonfigurasi, jadi satu call sudah tes keduanya.
    if market_data.oanda.is_configured:
        test = market_data.get_yahoo_data("EURUSD=X")
        src = test.get("source", "?")
        if "error" not in test and test.get("current_price"):
            if "OANDA" in src:
                lines.append(f"  ✅ OANDA {market_data.oanda.env_name} (real-time) — EUR/USD via {src}")
            else:
                lines.append(f"  ⚠️ OANDA {market_data.oanda.env_name} terkonfigurasi tapi gagal — pakai {src}")
        else:
            lines.append("  ⚠️ OANDA terkonfigurasi tapi error — fallback Yahoo aktif")
        # Yahoo tetap jadi fallback instrumen non-OANDA (IDR, DXY, index, crypto)
        test_other = market_data.get_yahoo_data("USDIDR=X")
        if "error" not in test_other:
            lines.append("  🟡 Yahoo Finance (fallback: IDR, DXY, index, crypto)")
        else:
            lines.append("  ⚠️ Yahoo Finance: error")
    else:
        lines.append("  ⬜ OANDA (belum dikonfigurasi — pakai Yahoo, delayed 15-20 mnt)")
        # Check Yahoo Finance
        test = market_data.get_yahoo_data("EURUSD=X")
        if "error" not in test:
            lines.append("  ✅ Yahoo Finance (unlimited)")
        else:
            lines.append("  ⚠️ Yahoo Finance: error")

    # Check Alpha Vantage
    if market_data.alpha_key:
        lines.append("  ✅ Alpha Vantage (terkonfigurasi)")
    else:
        lines.append("  ⬜ Alpha Vantage (belum dikonfigurasi)")

    # Check Finnhub
    if market_data.finnhub_key:
        lines.append("  ✅ Finnhub (terkonfigurasi)")
    else:
        lines.append("  ⬜ Finnhub (belum dikonfigurasi)")

    # Check FRED
    if macro_data.fred_key:
        lines.append("  ✅ FRED (terkonfigurasi)")
    else:
        lines.append("  ⬜ FRED (belum dikonfigurasi)")

    result = "\n".join(lines)
    _DATA_STATUS_CACHE["ts"] = now
    _DATA_STATUS_CACHE["value"] = result
    return result


# ===================== ERROR MESSAGES =====================

ERROR_MESSAGE = """
❌ *Maaf, terjadi kesalahan saat memproses pertanyaan Anda.*

Kemungkinan penyebab:
• API sedang rate limit
• Data pasar tidak tersedia untuk instrumen tersebut
• Koneksi internet bermasalah

Silakan coba lagi dengan pertanyaan yang berbeda.
Jika masalah berlanjut, gunakan /status untuk mengecek kondisi sistem.
"""

RATE_LIMIT_MESSAGE = """
⏳ *Mohon tunggu...*

Anda terlalu cepat mengirim pertanyaan. Bot butuh waktu untuk mengumpulkan data dan menganalisis.

Silakan tunggu beberapa detik sebelum mengirim pertanyaan berikutnya.
"""

NOT_IMPLEMENTED_MESSAGE = """
🔧 *Fitur ini sedang dalam pengembangan.*

Fitur yang Anda minta belum tersedia di versi ini.
Silakan coba pertanyaan lain atau gunakan /help untuk melihat daftar fitur yang tersedia.
"""


# ===================== MORNING BRIEF TEMPLATE =====================

MORNING_BRIEF_TEMPLATE = """
🌅 *MORNING BRIEF*
📍 *{date}*

{sentiment_summary}

{market_summary}

{macro_summary}

{calendar_summary}

{news_summary}

🔮 *OUTLOOK HARI INI*
{outlook}

⚡ *KATALIS UTAMA*
{catalysts}

---
⚠️ *Disclaimer:* Analisis edukasi. Bukan rekomendasi trading.
🤖 *MarketAI Analyst* | /help | /status | /calendar | /sentiment
"""


# ===================== ECONOMIC EVENT ALERTS =====================

ALERT_ON_MESSAGE = """
🔔 *Notifikasi Event Ekonomi: AKTIF!*

Kamu akan menerima:
• ☀️ *Digest harian* — daftar event high-impact hari ini (NFP, CPI, FOMC, dll)
• ⏰ *Reminder* — pengingat sebelum event high-impact rilis
• 🎯 *Prediksi* — arah emas (naik/turun) 5 menit sebelum rilis + hasil benar/salah di aftermath
• 📰 *Aftermath* — analisis dampak SETELAH event rilis: angka Actual vs Forecast, pengaruhnya ke DXY, dan penjelasan berita

Kirim `/alert off` untuk berhenti.
"""

ALERT_OFF_MESSAGE = """
🔕 *Notifikasi Event Ekonomi: NONAKTIF*

Kamu tidak akan menerima notifikasi event lagi.
Kirim `/alert on` untuk mengaktifkan kembali.
"""


# ===================== CHART HELP =====================

CHART_HELP_TEXT = """
📈 *GRAFIK HARGA*

Gunakan perintah berikut untuk melihat grafik harga:

`/chart eurusd` - EUR/USD
`/chart gbpusd` - GBP/USD
`/chart gold` - XAU/USD (Gold)
`/chart silver` - XAG/USD (Silver)
`/chart btc` - Bitcoin
`/chart eth` - Ethereum
`/chart dxy` - Dollar Index
`/chart sp500` - S&P 500
`/chart usdidr` - USD/IDR
`/chart vix` - VIX

Atau cukup klik tombol *📈 Chart* di menu utama!
"""

# ===================== DISCLAIMER =====================

DISCLAIMER = """
---
⚠️ *Disclaimer:* Analisis edukasi, bukan rekomendasi trading. Keputusan investasi/trading sepenuhnya tanggung jawab Anda. Harga Forex & Gold real-time via OANDA; instrumen lain dapat delayed 15-20 menit.
"""


def format_price(price: float, instrument: str = "forex") -> str:
    """Format harga sesuai instrumen."""
    if price is None:
        return "N/A"

    if "GC" in instrument or "XAU" in instrument or "SI" in instrument:
        return f"${price:,.2f}"
    elif "IDR" in instrument:
        return f"Rp{price:,.0f}"
    elif "JPY" in instrument:
        return f"¥{price:.3f}"
    elif "BTC" in instrument or "ETH" in instrument:
        return f"${price:,.2f}"
    else:
        if price >= 100:
            return f"{price:.2f}"
        elif price >= 1:
            return f"{price:.4f}"
        else:
            return f"{price:.5f}"
