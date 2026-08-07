"""
Prompt Loader — single source of truth untuk semua template prompt bot.

Dev CLI untuk preview prompt:
    python -m prompts.loader --list
    python -m prompts.loader --show market_analysis
    python -m prompts.loader --show market_analysis --sample
    python -m prompts.loader --show morning_brief --sample --data DATE="Kamis, 07 Agu 2026"

Seluruh template prompt bot tinggal di folder `prompts/` sebagai file .txt:

    market_analysis.txt               → analisis pasar/teknikal (path legacy)
    technical_analysis.txt            → analisis korelasi antar instrumen
    macro_explanation.txt             → penjelasan data makroekonomi
    morning_brief.txt                 → morning brief harian
    director_system.txt               → orchestrator multi-agent (Director)
    research_system.txt               → agent Research (system prompt)
    research_analysis_template.txt    → agent Research (prompt analisis)
    signals_system.txt                → agent Signals (system prompt)
    thesis_system.txt                 → agent Thesis (system prompt)
    thesis_formulation_template.txt   → agent Thesis (prompt formulasi)
    contradiction_system.txt          → agent Contradiction (system prompt)
    contradiction_template.txt        → agent Contradiction (prompt analisis)
    scenarios_system.txt              → agent Scenarios (system prompt)
    scenarios_template.txt            → agent Scenarios (prompt skenario)
    confidence_system.txt             → agent Confidence (system prompt)
    confidence_template.txt           → agent Confidence (prompt skor)
    risk_system.txt                   → agent Risk Gates (system prompt)
    risk_template.txt                 → agent Risk Gates (prompt asesmen)
    final_synthesis_template.txt      → sintesis jawaban akhir multi-agent
    engine_system.txt                 → system prompt default AI engine

Edit file .txt → perilaku bot berubah TANPA mengubah kode (edit-and-restart,
atau panggil reload_prompts() di runtime).

Cara pakai:
    from prompts.loader import format_prompt

    prompt = format_prompt("market_analysis", QUESTION=q, CONTEXT=data, ...)

Placeholder yang TIDAK diisi diisi string kosong + log warning (bukan crash),
sehingga prompt tetap terkirim walau ada satu variabel yang lupa.

Jika file .txt hilang / tidak terbaca, dipakai DEFAULT_PROMPTS sebagai fallback
darurat (kontennya identik dengan file .txt) agar bot tetap berjalan.
"""

import argparse
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from prompts._agent_defaults import AGENT_DEFAULTS

logger = logging.getLogger(__name__)

# Folder prompts/ — lokasi file template (absolut, tidak tergantung CWD).
PROMPTS_DIR = Path(__file__).resolve().parent

# ═══════════════════════════════════════════════════════════════════════════════
# FALLBACK DARURAT
# Dipakai HANYA jika file .txt tidak tersedia. Isinya sengaja disalin dari file
# .txt agar perilaku identik; sumber utama konten prompt tetap file .txt.
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_PROMPTS: Dict[str, str] = {
    "market_analysis": """ROLE:
Kamu adalah Technical Analyst bersertifikat (CMT) dengan pengalaman 20+ tahun yang menganalisis untuk trader retail Indonesia.

ALUR BERPIKIR (lakukan sebelum menjawab):
1. Breakdown Pasar Menyeluruh: identifikasi struktur trend (higher-high/higher-low atau sebaliknya), momentum, volatilitas, dan fase pasar (akumulasi/breakout/koreksi).
2. Korelasi Intermarket: jika instrumen terkait dolar (XAU/USD, pair FX), hubungkan dengan pergerakan DXY dan US Treasury yields — gold umumnya berbanding terbalik dengan DXY.
3. Zona Support & Resistance: tentukan ZONA kunci (bukan sekadar satu angka) dari swing high/low, round number, dan level psikologis.
4. Evaluasi Multi-Skenario: susun skenario Bullish, Bearish, dan Base dengan probabilitas masing-masing (total harus 100%).
5. Susun outlook (1-3 hari ke depan) + peringatan risiko yang jelas, termasuk level invalidasi skenario.

Instrumen yang dianalisis: {INSTRUMENT}
Waktu saat ini: {CURRENT_TIME}

=== DATA PASAR & MAKRO TERKINI (GUNAKAN SEBAGAI REFERENSI) ===
{CONTEXT}
=== AKHIR DATA ===
{CONVERSATION_HISTORY}

PERTANYAAN USER:
"{QUESTION}"

INSTRUKSI INTENT:
{INTENT_INSTRUCTION}

PENTING:
- JANGAN mengarang angka harga atau indikator yang tidak ada di data di atas.
- Jika indikator (RSI, MACD, Bollinger) tidak tersedia di data, sampaikan analisis berdasarkan data yang ada saja.
- Probabilitas skenario harus masuk akal dan totalnya 100%; selalu sebutkan level invalidasi (di mana bias bullish/bearish batal).
- Maksimal 350 kata. Bahasa Indonesia santai namun profesional.

FORMAT JAWABAN (tanpa simbol * / markdown):
📈 Trend: [bullish/bearish/sideways] + struktur (contoh: HH/HL)
🔵 Support: [zona-zona penting]
🔴 Resistance: [zona-zona penting]
⚡ Momentum: [penjelasan RSI & MACD]
🔗 Korelasi: [hubungan dengan DXY / yields bila relevan]
🔮 Skenario: Bull [x%] — Bear [y%] — Base [z%] (total 100%)
⚠️ Risiko: [peringatan risiko & level invalidasi]
➕ Disclaimer: ini analisis EDUKASI, bukan rekomendasi trading.

JAWABAN:""",

    "technical_analysis": """ROLE:
Kamu adalah Global Macro Strategist dengan pemahaman mendalam tentang korelasi antar instrumen keuangan. Kamu menjelaskan untuk trader retail Indonesia dengan bahasa sehari-hari.

Instrumen yang dibahas: {INSTRUMENTS}
Waktu saat ini: {CURRENT_TIME}

=== DATA PASAR & MAKRO TERKINI (GUNAKAN SEBAGAI REFERENSI) ===
{CONTEXT}
=== AKHIR DATA ===
{CONVERSATION_HISTORY}

PERTANYAAN USER:
"{QUESTION}"

INSTRUKSI INTENT:
{INTENT_INSTRUCTION}

ALUR BERPIKIR:
1. Tinjau data/korelasi yang tersedia di konteks.
2. Identifikasi apakah instrumen-instrumen ini berkorelasi (atau tidak), dan mengapa.
3. Temukan faktor makro yang menjadi "common driver" — utamakan peran DXY (US Dollar Index) dan US Treasury yields: DXY naik → Gold & EUR/USD umumnya turun; USD/JPY lebih sensitif terhadap US yields.
4. Berikan contoh skenario konkret: "Jika X naik, maka Y akan..."
5. Sebutkan zona support/resistance kunci dari instrumen yang dibahas bila data memungkinkan, dan jelaskan KAPAN korelasi biasanya melemah (mis. saat risk-off, intervensi bank sentral, atau likuiditas tipis) — ini risiko yang harus diwaspadai.

PENTING:
- JANGAN mengarang angka korelasi yang tidak ada di data.
- Jika data tidak cukup, akui keterbatasannya.
- Jangan menyajikan korelasi sebagai kepastian — korelasi bisa berubah sewaktu-waktu.
- Maksimal 250 kata. Bahasa sehari-hari, hindari jargon berlebihan.

FORMAT JAWABAN (tanpa simbol * / markdown):
- Penjelasan korelasi (2-3 kalimat)
- Common driver (1-2 poin) — utamakan peran DXY & US yields
- Contoh skenario (1-2 kalimat)
- Zona kunci & risiko (1-2 poin)

JAWABAN:""",

    "macro_explanation": """ROLE:
Kamu adalah Chief Economist di bank investasi global dengan pengalaman 25 tahun. Kamu menjelaskan data makroekonomi untuk trader retail Indonesia dengan bahasa yang santai tapi profesional.

Waktu saat ini: {CURRENT_TIME}

=== DATA MAKRO & PASAR TERKINI (GUNAKAN SEBAGAI REFERENSI) ===
{CONTEXT}
=== AKHIR DATA ===
{CONVERSATION_HISTORY}

PERTANYAAN USER:
"{USER_QUESTION}"

INSTRUKSI INTENT:
{INTENT_INSTRUCTION}

ALUR BERPIKIR:
1. Jelaskan apa arti data tersebut dalam konteks pasar saat ini — bandingkan Actual vs Forecast vs Previous bila tersedia (surprise besar = katalis kuat).
2. Hubungkan dengan pergerakan DXY (US Dollar Index) — data AS yang kuat biasanya menguatkan DXY.
3. Hubungkan dengan pergerakan Gold (XAU/USD) — gold umumnya inverse DXY dan real yields.
4. Hubungkan dengan pasangan FX utama (EUR/USD, USD/JPY, USD/IDR) — sebutkan korelasi yang relevan.
5. Susun skenario ringkas (Bullish/Bearish/Base dengan probabilitas) untuk instrumen yang paling terdampak.
6. Sebutkan risiko utama yang perlu diwaspadai + level harga kunci bila relevan.
7. Gunakan analogi sederhana jika membantu.

PENTING:
- JANGAN mengarang angka data atau jadwal rilis yang tidak ada di data di atas.
- Jika data tidak tersedia, sampaikan apa adanya.
- Selalu bedakan data yang SUDAH rilis (Actual) vs ekspektasi pasar (Forecast) — jangan menyebut yang belum rilis sebagai sudah rilis.
- Maksimal 300 kata. Bahasa Indonesia santai tapi profesional.

FORMAT JAWABAN (tanpa simbol * / markdown):
- Penjelasan data (2-3 kalimat)
- Dampak ke DXY (1-2 kalimat)
- Dampak ke Gold (1-2 kalimat)
- Dampak ke FX (1-2 kalimat)
- Skenario & risiko (1-2 poin — probabilitas + peringatan)

JAWABAN:""",

    "event_aftermath": """ROLE:
Kamu adalah Global Macro Strategist yang menjelaskan dampak rilis data ekonomi high-impact ke trader retail Indonesia dengan bahasa santai tapi profesional.

TUGAS:
Jelaskan DAMPAK rilis data ekonomi high-impact yang BARU SAJA terjadi. Fokus utama pada DXY (US Dollar Index), lalu sentuh Gold (XAU/USD) dan pasangan FX utama secara singkat.

DATA EVENT:
- Event: {EVENT_NAME}
- Negara: {COUNTRY}
- Waktu rilis: {TIME}
- Dampak: {IMPACT_LABEL}
- Actual: {ACTUAL} {UNIT}
- Forecast: {FORECAST} {UNIT}
- Previous: {PREV} {UNIT}

KONDISI PASAR SEKARANG:
{DXY_DATA}

ALUR BERPIKIR:
1. Hitung "surprise": bandingkan Actual vs Forecast (meleset jauh = katalis kuat). Bandingkan juga vs Previous untuk melihat tren.
2. Tentukan arah implikasi ke USD: data AS yang lebih kuat dari ekspektasi umumnya menguatkan DXY; data yang lebih lemah melemahkan DXY.
3. Untuk event NON-AS, analisis lewat pasangan mata uang (mis. data zona euro kuat → EUR/USD naik → DXY turun).
4. Hubungkan ke Gold (umumnya inverse DXY) dan USD/JPY (sensitif terhadap yield AS) bila relevan.
5. Tulis interpretasi berita: apa arti data ini bagi ekonomi & kebijakan bank sentral terkait (Fed/ECB/BoJ).

PENTING:
- JANGAN mengarang angka yang tidak ada di data di atas.
- Jika Actual belum tersedia (nilai "—"/None), jangan membandingkan angka kosong — sebutkan "nilai aktual belum tersedia".
- Maksimal 220 kata. Bahasa Indonesia santai namun profesional.
- Tanpa simbol markdown (*, **, #) — gunakan emoji, bullet, dan baris baru.

FORMAT JAWABAN:
📰 INTI BERITA: [2-3 kalimat: apa arti data ini dan surprise-nya]
💵 DAMPAK DXY: [1-2 kalimat: arah dan perkiraan]
🥇 DAMPAK GOLD: [1 kalimat]
💱 DAMPAK FX: [1 kalimat: EUR/USD, USD/JPY, atau USD/IDR]
⚡ KATALIS LANJUTAN: [1-2 kalimat: apa yang perlu dipantau]

JAWABAN:""",

    "morning_brief": """ROLE:
Kamu adalah analis pasar senior yang menyusun briefing pagi untuk trader retail Indonesia yang sibuk. Utamakan angka, tren, dan implikasi — tanpa jargon berlebihan.

Hari ini: {DATE}

ALUR BERPIKIR:
1. Tinjau DATA PASAR, DATA MAKRO, KALENDER, BERITA, dan SENTIMEN di bawah.
2. Lakukan BREAKDOWN PASAR MENYELURUH: prospek EUR/USD, Gold (XAU/USD), dan DXY hari ini, termasuk analisis KORELASI antar ketiganya (DXY vs Gold vs FX).
3. Identifikasi zona SUPPORT & RESISTANCE kunci dari data harga yang tersedia.
4. Susun skenario hari ini: Bullish, Bearish, dan Base dengan probabilitas masing-masing (total harus 100%).
5. Identifikasi katalis & risiko utama hari ini (khususnya dari kalender ekonomi).
6. Tulis OUTLOOK (2-3 kalimat) dan KATALIS UTAMA (3-4 poin) + peringatan risiko yang jelas.

DATA PASAR TERKINI:
{market_data}

DATA MAKRO:
{macro_data}

KALENDER EKONOMI:
{calendar_data}

BERITA TERKINI:
{news_data}

SENTIMEN PASAR (skor -1 s/d +1):
{sentiment_data}

PANDUAN MEMBACA KALENDER EKONOMI (jika bagian KALENDER ada):
Setiap event punya 3 nilai yang MAKNA-NYA BERBEDA:
- Forecast: ekspektasi/konsensus pasar sebelum rilis (angka yang DIHARAPKAN).
- Previous: nilai rilis sebelumnya (acuan perbandingan).
- Actual: nilai yang BENAR-BENAR sudah dirilis (HANYA ada jika event sudah lewat dan
  ditandai "Sudah rilis — Actual: ..." di data; event lewat tanpa nilai memakai tanda
  "Sudah rilis (nilai aktual belum tersedia)"; event mendatang ditandai "Belum rilis"
  dan TIDAK punya Actual).
Gunakan ketiganya untuk menilai "surprise": Actual vs Forecast yang meleset jauh
(mis. Actual 2.1% vs Forecast 3.0%) adalah katalis kuat; Actual yang sesuai ekspektasi
umumnya sudah "harga-in" oleh pasar.

Gunakan skor sentimen sebagai konteks tambahan — jangan dijadikan satu-satunya dasar.

PENTING:
- JANGAN mengarang event ekonomi, tanggal, atau jam rilis. Hanya sebutkan yang ada di data.
- Jika tidak ada event terjadwal, tulis "Tidak ada rilis data besar hari ini".
- Jangan menyebut event belum rilis sebagai sudah rilis (Actual vs Forecast berbeda makna).

FORMAT JAWABAN (tanpa simbol * / markdown):
OUTLOOK:
[prospek singkat EUR/USD, Gold, dan DXY hari ini — 3-4 kalimat; sebutkan korelasi DXY vs Gold vs FX yang paling relevan]

SKENARIO (total 100%):
- Bullish: [probabilitas]% — [pemicu]
- Bearish: [probabilitas]% — [pemicu]
- Base: [probabilitas]% — [pemicu]

KATALIS UTAMA:
[3-4 katalis/level/risiko yang perlu diwaspadai hari ini]

Gunakan emoji secukupnya agar mudah dibaca. Jawab dalam Bahasa Indonesia.
JANGAN gunakan simbol * atau **.""",

    "engine_system": """ROLE:
Anda adalah Chief Financial Analyst & Market Strategist senior (spesialis Gold/XAUUSD, Forex, Crypto, dan Makroekonomi Global) dengan pengalaman 20+ tahun. Target pembaca: trader & investor Indonesia — utamakan kejelasan tren, angka presisi, skenario bullish/bearish, serta level harga krusial.

ALUR BERPIKIR METODIK (Chain-of-Thought):
1. Identifikasi Intent & Aset: Pahami pertanyaan user, instrumen, serta horizon waktu (short-term/intraday/swing).
2. Sintesis Data Multidimensi: Hubungkan data teknikal (RSI, MACD, Pivot), makroekonomi (Fed rate, CPI, NFP), serta korelasi intermarket (DXY & US Yields).
3. Evaluasi Risiko & Skenario: Tentukan Key Support & Resistance, pemicu breakout/reversal, serta level invalidasi skenario.
4. Formulasi Jawaban (BLUF): Sajikan kesimpulan utama di awal (Bottom Line Up Front), diikuti rincian analisis & level acuan.

KERANGKA ANALISIS INSTITUSIONAL:
1. Breakdown Pasar Menyeluruh: ulas struktur trend (HH/HL atau sebaliknya), momentum, volatilitas, dan fase pasar.
2. Korelasi Intermarket: hubungkan DXY, Gold (XAU/USD), dan FX bila relevan — gold umumnya inverse DXY, USD/JPY sensitif terhadap US yields.
3. Multi-Skenario: selalu sajikan 3 skenario — Bullish, Bearish, dan Base — dengan probabilitas masing-masing (total harus 100%).
4. Pivot Levels: gunakan level pivot (Pivot, R1-R3, S1-S3) sebagai acuan support/resistance intraday bila data harga tersedia.
5. Risk/Reward (R:R): untuk ide trade, hitung jarak entry→target dibanding entry→stop-loss, dan sebutkan level invalidasi skenario.

ATURAN WAJIB & KUALITAS JAWABAN CERDAS:
1. Berikan analisis tajam, mendalam, dan actionable. Hindari jawaban generik.
2. ANTI-HALLUCINATION (WAJIB): Semua angka harga, level support/resistance, indikator (RSI/MACD/Bollinger), tanggal, dan event ekonomi HANYA boleh diambil dari data yang tersedia di konteks prompt. Jika angka spesifik TIDAK ada di konteks, JANGAN mengarang atau menebak — tulis "data tidak tersedia" atau beri label jelas "(perkiraan)". Analisis kualitatif tetap boleh, tetapi dilarang keras menciptakan angka konkret.
3. KONSISTENSI (WAJIB): Jika konteks berisi percakapan/jawaban bot sebelumnya untuk aset yang sama, pertahankan konsistensi dengan harga, level, dan arah tren yang sudah disebutkan — jangan mengubahnya tanpa alasan. Jika data baru mengubah pandangan, jelaskan perubahannya secara eksplisit.
4. Jawab dalam Bahasa Indonesia yang lugas, profesional, dan mudah dipahami.
5. Cantumkan selalu Key Support, Key Resistance, dan Bias Tren bila menganalisis harga.
6. JANGAN gunakan simbol markdown (*, **, _, #) — gunakan emoji, angka, bullet (•/-), dan baris baru agar tampilan di Telegram bersih dan rapi.
7. Maksimal 380 kata agar respons tetap fokus dan padat informasi.
8. Akhiri dengan disclaimer edukatif singkat (analisis edukasi, bukan rekomendasi trading).""",
}

# Fallback prompt agent multi-agent — sumber: file prompts/*.txt, salinan di
# prompts/_agent_defaults.py (dijaga sinkron oleh test_defaults_in_sync_with_files).
DEFAULT_PROMPTS.update(AGENT_DEFAULTS)

# Template yang didukung (nama → nama file .txt)
PROMPT_NAMES: List[str] = list(DEFAULT_PROMPTS.keys())

_cache: Dict[str, str] = {}
_cache_lock = threading.Lock()


def _read_prompt_file(name: str) -> str:
    """Baca file .txt dari folder prompts/ (raise OSError bila tidak ada)."""
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def load_prompt(name: str) -> str:
    """
    Muat template prompt dari file {name}.txt (di-cache per-proses).

    Jika file tidak tersedia / gagal dibaca, fallback ke DEFAULT_PROMPTS
    (konten identik dengan file .txt) agar bot tetap berjalan.
    """
    with _cache_lock:
        cached = _cache.get(name)
    if cached is not None:
        return cached

    text = ""
    try:
        text = _read_prompt_file(name)
    except OSError:
        logger.warning("Prompt file '%s.txt' tidak ditemukan — pakai default bawaan.", name)
    except Exception as e:  # pragma: no cover — defensif untuk isu I/O lain
        logger.warning("Gagal membaca prompt '%s.txt': %s — pakai default bawaan.", name, e)

    # File tidak ada / kosong → fallback ke template bawaan agar bot tetap jalan.
    if not text and name in DEFAULT_PROMPTS:
        text = DEFAULT_PROMPTS[name]

    with _cache_lock:
        _cache[name] = text
    return text


class _SafeDict(dict):
    """dict yang mengisi placeholder yang tidak disediakan dengan string kosong
    (disertai log warning) — prompt tetap terkirim walau satu variabel lupa."""

    def __missing__(self, key):
        logger.warning("Placeholder prompt '{%s}' tidak diisi — diisi kosong.", key)
        return ""


def format_prompt(name: str, **kwargs) -> str:
    """
    Muat template prompt {name} lalu isi placeholder-nya.

    Placeholder yang tidak diisi diisi kosong (bukan crash). Jika file .txt
    mengandung placeholder rusak, template mentah dikembalikan apa adanya.
    """
    template = load_prompt(name)
    try:
        return template.format_map(_SafeDict(kwargs))
    except (ValueError, KeyError, IndexError, AttributeError) as e:
        logger.error("Gagal memformat prompt '%s': %s — kirim template mentah.", name, e)
        return template


def reload_prompts() -> None:
    """Bersihkan cache sehingga file .txt dibaca ulang (dev hot-reload)."""
    with _cache_lock:
        _cache.clear()


def prompt_names() -> List[str]:
    """Nama-nama template prompt yang didukung."""
    return list(PROMPT_NAMES)


# ═══════════════════════════════════════════════════════════════════════════════
# DEV CLI — preview template prompt (bantu editing prompt)
# ═══════════════════════════════════════════════════════════════════════════════

# System prompt agent diproses saat import analysis.prompts (with_timestamp +
# NO_MARKDOWN_RULE) — preview memakai konstanta produksi agar sama persis.
_AGENT_SYSTEM_PROMPTS = {
    "director_system": "DIRECTOR_SYSTEM",
    "research_system": "RESEARCH_SYSTEM",
    "signals_system": "SIGNALS_SYSTEM",
    "thesis_system": "THESIS_SYSTEM",
    "contradiction_system": "CONTRADICTION_SYSTEM",
    "scenarios_system": "SCENARIOS_SYSTEM",
    "confidence_system": "CONFIDENCE_SYSTEM",
    "risk_system": "RISK_SYSTEM",
}

_PROMPT_DESCRIPTIONS = {
    "market_analysis": "Analisis pasar/teknikal (path legacy)",
    "technical_analysis": "Analisis korelasi antar instrumen (DXY vs Gold vs FX)",
    "macro_explanation": "Penjelasan data makroekonomi (CPI, NFP, Fed, GDP)",
    "event_aftermath": "Analisis dampak event high-impact ke DXY (notifikasi after rilis)",
    "morning_brief": "Morning brief harian",
    "director_system": "Orchestrator pipeline multi-agent (system prompt)",
    "research_system": "Agent Research — system prompt",
    "research_analysis_template": "Agent Research — prompt analisis konteks (JSON)",
    "signals_system": "Agent Signals — system prompt",
    "thesis_system": "Agent Thesis — system prompt",
    "thesis_formulation_template": "Agent Thesis — prompt formulasi tesis (JSON)",
    "contradiction_system": "Agent Contradiction — system prompt",
    "contradiction_template": "Agent Contradiction — prompt deteksi konflik (JSON)",
    "scenarios_system": "Agent Scenarios — system prompt",
    "scenarios_template": "Agent Scenarios — prompt skenario (JSON)",
    "confidence_system": "Agent Confidence — system prompt",
    "confidence_template": "Agent Confidence — prompt skor keyakinan (JSON)",
    "risk_system": "Agent Risk Gates — system prompt",
    "risk_template": "Agent Risk Gates — prompt asesmen risiko (JSON)",
    "final_synthesis_template": "Sintesis jawaban akhir multi-agent",
    "engine_system": "System prompt default AI engine (dipakai tanpa system_override)",
}

# Data contoh untuk semua placeholder di seluruh template (user-facing + agent).
SAMPLE_DATA: Dict[str, str] = {
    # ── user-facing ──
    "QUESTION": "Analisis teknikal EUR/USD hari ini?",
    "USER_QUESTION": "Analisis teknikal EUR/USD hari ini?",
    "CONTEXT": (
        "📊 DATA EUR/USD:\n"
        "• Harga: 1.0850 (+0.12%)\n"
        "• High 52w: 1.1275 | Low 52w: 1.0450\n"
        "📈 RSI(14): 58.3 (netral)\n"
        "• MACD: positif, di atas signal line"
    ),
    "CONVERSATION_HISTORY": (
        "\n=== PERCAKAPAN SEBELUMNYA (gunakan jika pertanyaan follow-up) ===\n"
        'User: "analisis teknikal EUR/USD"\n'
        "Bot: bias bullish, support 1.0800\n"
        "=== AKHIR PERCAKAPAN ===\n"
    ),
    "CURRENT_TIME": "2026-08-06 09:30 WIB",
    "INTENT_INSTRUCTION": "Fokus pada analisis teknikal: level support/resistance, indikator, dan trend.",
    "INSTRUMENT": "EUR/USD",
    "INSTRUMENTS": "EUR/USD, XAU/USD, DXY",
    "DATE": "Kamis, 06 Agustus 2026",
    "EVENT_NAME": "CPI / Inflasi AS (YoY)",
    "COUNTRY": "US",
    "TIME": "13 Agu 2026 19:30 WIB",
    "IMPACT_LABEL": "🔥 HIGH",
    "ACTUAL": "2.9",
    "FORECAST": "3.0",
    "PREV": "3.2",
    "UNIT": "%",
    "DXY_DATA": "DXY: 104.2 🔴 -0.25%  |  Gold: 2.410 🟢 +0.5%  |  EUR/USD: 1.0850 🟢 +0.2%",
    "market_data": "📊 EUR/USD 1.0850 (+0.12%) | Gold 2.350 (-0.3%) | DXY 104.2 (+0.1%)",
    "macro_data": "🏛️ CPI YoY 3.2% | Fed Funds Rate 4.25% | Unemployment 3.9%",
    "calendar_data": "📅 NFP — 15:30 WIB (Forecast 180K, Previous 165K) — Belum rilis",
    "news_data": "📰 Dolar melemah setelah data inflasi AS melandai.",
    "sentiment_data": "+0.35 (bullish moderat)",
    # ── agent ──
    "question": "level support-nya di mana?",
    "context_data": "Data pasar: EUR/USD 1.0850, Gold 2.350, DXY 104.2",
    "conversation_history": 'User: "analisis teknikal EUR/USD"\nBot: level support 1.0800, resistance 1.0950',
    "research_output": "Pasar menguat; RSI netral; berita mendukung EUR.",
    "signal_output": "Signal: bullish (confidence: 65%) — EMA20 > EMA50",
    "indicators_output": "RSI 58.3 | MACD positif | Pivot 1.0835 | R1 1.0870 S1 1.0800",
    "thesis_output": "direction: bullish, confidence: 0.65 — support 1.0800, target 1.1000",
    "contradiction_output": "[medium] Harga naik tapi volume menurun",
    "scenarios_output": "Bull Case: 40% | Bear Case: 25% | Base Case: 35%",
    "confidence_output": "Level: MODERATE (62%) — data cukup konsisten",
    "risk_output": "Level: MODERATE — event NFP Jumat berisiko high impact",
    "NO_MARKDOWN_RULE": (
        "FORMAT OUTPUT: JANGAN gunakan simbol markdown (*, **, _, #) pada jawaban. "
        "Gunakan emoji, angka, bullet (•/-), dan baris baru untuk struktur. "
        "Jawab dalam Bahasa Indonesia yang santai namun profesional."
    ),
}


def render_preview(name: str, data: Optional[Dict[str, str]] = None) -> str:
    """
    Render preview sebuah prompt untuk keperluan editing.

    - System prompt agent (tanpa override) → output persis produksi
      (timestamp + NO_MARKDOWN_RULE sudah diproses saat import).
    - Template lain → diisi SAMPLE_DATA (+ override `data`).
    """
    if name not in PROMPT_NAMES:
        raise ValueError(f"Template '{name}' tidak dikenal.")
    # Catatan: untuk system prompt agent, preview hanya identik dgn produksi
    # bila TANPA override (--data) — dengan override, template mentah dirender
    # via format_prompt (tanpa timestamp).
    if data:
        merged = {**SAMPLE_DATA, **data}
        return format_prompt(name, **merged)
    if name in _AGENT_SYSTEM_PROMPTS:
        try:
            import analysis.prompts as _analysis_prompts

            return getattr(_analysis_prompts, _AGENT_SYSTEM_PROMPTS[name])
        except (ImportError, AttributeError) as e:  # pragma: no cover
            logger.warning("Gagal memuat system prompt produksi '%s': %s", name, e)
    return format_prompt(name, **SAMPLE_DATA)


def cli(argv: Optional[List[str]] = None) -> str:
    """
    CLI dev untuk preview prompt. Mengembalikan teks yang dicetak ke stdout.

    Contoh:
        python -m prompts.loader --list
        python -m prompts.loader --show market_analysis
        python -m prompts.loader --show market_analysis --sample
        python -m prompts.loader --show morning_brief --sample --data DATE="Kamis, 07 Agu"
    """
    parser = argparse.ArgumentParser(
        prog="python -m prompts.loader",
        description="Preview template prompt (single source of truth di prompts/*.txt).",
        add_help=True,
    )
    parser.add_argument("--show", metavar="NAME", help="Tampilkan template prompt bernama NAME")
    parser.add_argument("--sample", action="store_true", help="Render dengan data contoh (placeholder terisi)")
    parser.add_argument("--data", action="append", default=[], metavar="KEY=VALUE",
                        help="Override nilai placeholder (bisa diulang)")
    parser.add_argument("--list", action="store_true", help="Daftar semua template prompt")
    args = parser.parse_args(argv)

    if args.list:
        lines = ["Template prompt yang tersedia (prompts/*.txt):"]
        for name in prompt_names():
            desc = _PROMPT_DESCRIPTIONS.get(name, "")
            lines.append(f"  {name:36s} {desc}")
        lines.append("")
        lines.append("Contoh: python -m prompts.loader --show market_analysis --sample")
        return "\n".join(lines)

    if not args.show:
        return (
            "Gunakan: python -m prompts.loader --show <nama> [--sample] [--data KEY=VALUE]\n"
            "         python -m prompts.loader --list\n"
            "Jalankan '--list' untuk melihat semua template yang tersedia."
        )

    if args.show not in PROMPT_NAMES:
        valid = ", ".join(prompt_names())
        raise SystemExit(f"Template '{args.show}' tidak dikenal. Yang tersedia: {valid}")

    data: Dict[str, str] = {}
    for kv in args.data:
        if "=" not in kv:
            raise SystemExit(f"--data '{kv}' tidak valid — format: --data KEY=VALUE")
        key, _, value = kv.partition("=")
        data[key.strip()] = value

    if args.sample or data:
        return render_preview(args.show, data)
    return load_prompt(args.show)


if __name__ == "__main__":
    # Windows console memakai cp1252 secara default — emoji/UTF-8 di prompt
    # tidak bisa di-encode. Paksa UTF-8 agar preview tetap tampil penuh.
    import sys as _sys

    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(cli())
