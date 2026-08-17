"""
COT (Commitments of Traders) — data posisi institusional dari CFTC.

Sumber data: CFTC merilis laporan COT GRATIS setiap Jumat 15:30 ET (data posisi
per Selasa). Bot mengambil file arsip tahun berjalan berformat *legacy futures
only* (paling umum, mencakup FX, logam, energi, index, crypto):

    https://www.cftc.gov/files/dea/history/deacot{YYYY}.zip
    (isi: annual.txt — SEMUA market, SEMUA minggu dalam setahun)

Modul ini MURNI (tanpa Telegram / tanpa AI): download → unzip → parse CSV →
cocokkan instrumen → pilih minggu terbaru + minggu sebelumnya. Cache hasil
parsing di memori (per proses, TTL pendek) agar /cot untuk market lain di hari
yang sama tidak mendownload ulang; cache jangka panjang (7 hari) di Supabase
ditangani lapisan bot (lihat Database.set_cot_cache).

Format kolom legacy (normalisasi: huruf kecil + buang non-alphanumeric):
    marketandexchangenames, cftccontractmarketcode, asofdate...,
    openinterestall, noncommercialpositionslongall, ..., commercial...,
    changeincommittments/changeincommitments..., %ofoi-...

Catatan keterbatasan:
- COT adalah data FUTURES (bukan spot forex) — tidak semua pair tersedia.
- Nama market bisa berubah (mis. "CRUDE OIL, LIGHT SWEET - NYMEX"); pencocokan
  memakai kata kunci + tie-break open interest terbesar (kontrak standar).
"""

import csv
import io
import logging
import re
import threading
import time
import zipfile
from datetime import date, datetime
from typing import Dict, List, Optional

from data.http_session import get_requests_session as _session

logger = logging.getLogger(__name__)

# ===================== URL & CACHE =====================

# Basis URL arsip tahunan. Beberapa varian dicoba berurutan (www vs tanpa www,
# https vs http) karena CFTC kadang mengubah redirect.
# - legacy: deacot{year}.zip → annual.txt (Noncommercial/Commercial)
# - tff:    fut_fin_txt_{year}.zip → FinFutYY.txt (Dealer/Leveraged Funds —
#   berisi USD INDEX, treasury, FX, index)
_ARCHIVE_URLS = [
    "https://www.cftc.gov/files/dea/history/{prefix}{year}.zip",
    "https://cftc.gov/files/dea/history/{prefix}{year}.zip",
    "http://www.cftc.gov/files/dea/history/{prefix}{year}.zip",
]

_ARCHIVE_PREFIX = {
    "legacy": "deacot",
    "tff": "fut_fin_txt_",
}

# Cache parsing dalam memori: {(report_type, year): (fetched_at_ts, rows)} —
# dipakai lintas permintaan /cot di proses yang sama (hindari download ulang).
_mem_cache: Dict[tuple, tuple] = {}
_mem_lock = threading.Lock()
_MEM_TTL_SECONDS = 12 * 3600  # arsip hanya berubah 1x/minggu — 12 jam cukup

DOWNLOAD_TIMEOUT = 30

# ===================== INSTRUMEN COT =====================
# Alias input user → kata kunci nama market di laporan CFTC + nama tampilan.
# Pencocokan di extract_market memakai skor (bukan sekadar OI terbesar) agar
# kontrak STANDAR menang atas kontrak kecil dengan nama mirip:
#   + kata kunci cocok persis / prefix / sesudah prefix micro-mini
#   + prefer (bursa utama kontrak standar, mis. CME/COMEX/NYMEX)
#   − avoid (kontrak perp-style, produk derivatif aneh) dan prefiks micro/nano
# Spot forex TIDAK ada di COT — /cot menampilkan disclaimer bila tidak cocok.
COT_INSTRUMENTS: List[Dict] = [
    {"aliases": ["xauusd", "xau/usd", "gold", "emas", "gc"],
     "keywords": ["gold"], "display": "Gold Futures (COMEX)",
     "prefer": ["commodity exchange inc"]},
    {"aliases": ["xagusd", "xag/usd", "silver", "perak", "si"],
     "keywords": ["silver"], "display": "Silver Futures (COMEX)",
     "prefer": ["commodity exchange inc"]},
    {"aliases": ["eurusd", "eur/usd", "eur", "euro"],
     "keywords": ["euro fx"], "display": "Euro FX Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["gbpusd", "gbp/usd", "gbp", "pound"],
     "keywords": ["british pound"], "display": "British Pound Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["usdjpy", "usd/jpy", "jpy", "yen"],
     "keywords": ["japanese yen"], "display": "Japanese Yen Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["usdchf", "usd/chf", "chf"],
     "keywords": ["swiss franc"], "display": "Swiss Franc Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["usdcad", "usd/cad", "cad"],
     "keywords": ["canadian dollar"], "display": "Canadian Dollar Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["audusd", "aud/usd", "aud"],
     "keywords": ["australian dollar"], "display": "Australian Dollar Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["nzdusd", "nzd/usd", "nzd"],
     "keywords": ["new zealand dollar"], "display": "New Zealand Dollar Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["usdmxn", "usd/mxn", "mxn", "peso"],
     "keywords": ["mexican peso"], "display": "Mexican Peso Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["cl", "wti", "oil", "minyak", "crude"],
     "keywords": ["crude oil"], "display": "WTI Crude Oil Futures (NYMEX)",
     "prefer": ["new york mercantile exchange"], "avoid": ["diff"]},
    {"aliases": ["brent", "brent crude"],
     "keywords": ["brent"], "display": "Brent Crude Oil Futures (ICE)",
     "prefer": ["intercontinental exchange"], "avoid": ["diff"]},
    {"aliases": ["copper", "hg", "tembaga"],
     "keywords": ["copper"], "display": "Copper Futures (COMEX)",
     "prefer": ["commodity exchange inc"]},
    {"aliases": ["btc", "bitcoin", "btcusd"],
     "keywords": ["bitcoin"], "display": "Bitcoin Futures (CME)",
     "prefer": ["chicago mercantile exchange"], "avoid": ["perp"]},
    {"aliases": ["dxy", "dollar index", "usd index", "dx"],
     "keywords": ["usd index", "dollar index"], "display": "US Dollar Index Futures (ICE)",
     "prefer": ["ice futures u.s"], "report": "tff"},
    {"aliases": ["sp500", "s&p 500", "spx"],
     "keywords": ["s&p 500"], "display": "S&P 500 E-mini Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["nasdaq", "nq", "nasdaq 100"],
     "keywords": ["nasdaq 100"], "display": "NASDAQ 100 E-mini Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["dow", "djia", "e-mini dow", "emini dow", "dow jones", "ym"],
     "keywords": ["djia"], "display": "DJIA (E-mini Dow) Futures (CBOT)",
     "prefer": ["chicago board of trade"], "avoid": ["micro", "real estate"]},
    {"aliases": ["nikkei", "n225", "nikkei 225"],
     "keywords": ["nikkei stock average"], "display": "Nikkei 225 Futures (CME)",
     "prefer": ["chicago mercantile exchange"], "avoid": ["mini"]},
    {"aliases": ["us2y", "2 year", "2y note", "ust 2y"],
     "keywords": ["ust 2y"], "display": "2-Year T-Note Futures (CBOT)",
     "prefer": ["chicago board of trade"], "avoid": ["eris"], "report": "tff"},
    {"aliases": ["us5y", "5 year", "5y note", "ust 5y"],
     "keywords": ["ust 5y"], "display": "5-Year T-Note Futures (CBOT)",
     "prefer": ["chicago board of trade"], "avoid": ["eris"], "report": "tff"},
    {"aliases": ["us10y", "10y", "10 year", "treasury", "t-note"],
     "keywords": ["ust 10y"], "display": "10-Year T-Note Futures (CBOT)",
     "prefer": ["chicago board of trade"], "avoid": ["eris"], "report": "tff"},
    {"aliases": ["us30y", "30y", "ust bond", "long bond"],
     "keywords": ["ust bond"], "display": "30-Year T-Bond Futures (CBOT)",
     "prefer": ["chicago board of trade"], "avoid": ["ultra"], "report": "tff"},
    {"aliases": ["fed funds", "fedfunds"],
     "keywords": ["fed funds"], "display": "Fed Funds Futures (CBOT)",
     "prefer": ["chicago board of trade"], "report": "tff"},
    {"aliases": ["sofr", "sofr3m", "sofr 3m"],
     "keywords": ["sofr 3m"], "display": "SOFR 3-Month Futures (CME)",
     "prefer": ["chicago mercantile exchange"], "report": "tff"},
    {"aliases": ["sofr1m", "sofr 1m"],
     "keywords": ["sofr 1m"], "display": "SOFR 1-Month Futures (CME)",
     "prefer": ["chicago mercantile exchange"], "report": "tff"},
    {"aliases": ["sp400", "s&p 400", "midcap", "mid cap"],
     "keywords": ["s&p 400"], "display": "S&P 400 Midcap E-mini Futures (CME)",
     "prefer": ["chicago mercantile exchange"]},
    {"aliases": ["russell", "russell 2000", "rty"],
     # 'russell e-mini' = kontrak utama E-mini Russell 2000 ("RUSSELL E-MINI - CME").
     # Jangan pakai keyword 'russell 2000': itu cocok dengan 'RUSSELL 2000 ANNUAL
     # DIVIDEND' (indeks dividen, bukan kontrak) dan micro contract.
     "keywords": ["russell e-mini"], "display": "Russell 2000 E-mini Futures (CME)",
     "prefer": ["chicago mercantile exchange"], "avoid": ["dividend", "1000", "micro"]},
    {"aliases": ["vix", "volatility"],
     "keywords": ["vix"], "display": "VIX Futures (CBOE)",
     "prefer": ["cboe futures exchange"]},
    {"aliases": ["corn", "jagung"],
     "keywords": ["corn"], "display": "Corn Futures (CBOT)",
     "prefer": ["chicago board of trade"]},
    {"aliases": ["wheat", "gandum"],
     "keywords": ["wheat"], "display": "Wheat Futures (CBOT)",
     "prefer": ["chicago board of trade"]},
    {"aliases": ["soybean", "soybeans", "kedelai"],
     "keywords": ["soybeans"], "display": "Soybean Futures (CBOT)",
     "prefer": ["chicago board of trade"]},
    {"aliases": ["natural gas", "ng", "gas", "henry hub"],
     # 'henry hub' mengarahkan ke kontrak utama NYMEX (bukan index regional
     # ICE 'NATURAL GAS INDEX: EP SAN JUAN' yang berhenti dilaporkan).
     "keywords": ["henry hub", "natural gas"], "display": "Natural Gas Futures (NYMEX)",
     "prefer": ["new york mercantile exchange"], "avoid": ["basis", "index", "last day", "penultimate"]},
]

# ===================== UTILITAS =====================

def _norm(text: str) -> str:
    """Normalisasi teks untuk pencocokan: lowercase + buang non-alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _to_int(value) -> Optional[int]:
    """Parse angka kontrak (tahan format '1,234' / ' 123 ' / kosong)."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s or s in ("-", "--", "N/A"):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _parse_report_date(value: str) -> Optional[datetime.date]:
    """Parse tanggal laporan: 'YYYY-MM-DD' (file baru) atau 'MM/DD/YYYY' (lama)."""
    v = (value or "").strip().strip('"')
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _find_header(reader) -> Optional[List[str]]:
    """Lewati baris kosong/pengantar sampai header kolom COT ditemukan.

    Header dikenali dari keberadaan kolom 'Market and Exchange Names'
    (normalisasi: marketandexchangenames).
    """
    for row in reader:
        norm_row = [_norm(c) for c in row]
        if any("marketandexchangenames" in c for c in norm_row):
            return row
    return None


def _build_column_map(header: List[str]) -> Dict[str, int]:
    """Peta nama kolom ternormalisasi → index (untuk pencarian nama kolom)."""
    return {_norm(c): i for i, c in enumerate(header)}


def _pick_col(colmap: Dict[str, int], *candidates: str) -> Optional[int]:
    """Index kolom pertama yang ada di antara kandidat nama (normalisasi)."""
    for c in candidates:
        idx = colmap.get(_norm(c))
        if idx is not None:
            return idx
    return None


# ===================== PARSER =====================
# Dua format laporan CFTC (CSV, kolom beda):
# - legacy (annual.txt): Noncommercial/Commercial — FX, logam, energi, index
# - tff (FinFutYY.txt):  Dealer / Asset Mgr / Leveraged Funds — USD INDEX,
#   treasury, FX, index. Untuk kemudahan pembaca, "speculative" TFF dipetakan
#   ke Leveraged Funds dan "hedger" ke Dealer/Intermediary.

# Kandidat nama kolom per makna (ternormalisasi saat dicari).
_DATE_COLUMNS = [
    "Report Date as YYYY-MM-DD",
    "As of Date in Form YYYY-MM-DD",
    "As of Date in Form MM/DD/YYYY",
    "As of Date",
]

_SCHEMAS = {
    "legacy": {
        "nc_long": ["Noncommercial Positions-Long (All)"],
        "nc_short": ["Noncommercial Positions-Short (All)"],
        "nc_spread": ["Noncommercial Positions-Spread (All)"],
        "c_long": ["Commercial Positions-Long (All)"],
        "c_short": ["Commercial Positions-Short (All)"],
        "total_long": ["Total Reportable Positions-Long (All)"],
        "total_short": ["Total Reportable Positions-Short (All)"],
        "nr_long": ["Nonreportable Positions-Long (All)"],
        "nr_short": ["Nonreportable Positions-Short (All)"],
        "pct_nc_long": ["% of OI-Noncommercial-Long (All)"],
        "pct_nc_short": ["% of OI-Noncommercial-Short (All)"],
        "pct_c_long": ["% of OI-Commercial-Long (All)"],
        "pct_c_short": ["% of OI-Commercial-Short (All)"],
    },
    "tff": {
        # TFF memakai nama kolom lain: "Lev Money" (Leveraged Funds),
        # "Tot Rept", "NonRept" — semua kandidat didaftarkan di bawah.
        "nc_long": ["Leveraged Funds Positions-Long (All)", "Lev Money Positions-Long (All)"],
        "nc_short": ["Leveraged Funds Positions-Short (All)", "Lev Money Positions-Short (All)"],
        "nc_spread": ["Leveraged Funds Positions-Spread (All)", "Lev Money Positions-Spread (All)"],
        "c_long": ["Dealer/Intermediary Positions-Long (All)", "Dealer Positions-Long (All)"],
        "c_short": ["Dealer/Intermediary Positions-Short (All)", "Dealer Positions-Short (All)"],
        "total_long": [
            "Total Reportable Positions-Long (All)",
            "Total Rept Positions-Long (All)",
            "Tot Rept Positions-Long (All)",
        ],
        "total_short": [
            "Total Reportable Positions-Short (All)",
            "Total Rept Positions-Short (All)",
            "Tot Rept Positions-Short (All)",
        ],
        "nr_long": ["Nonreportable Positions-Long (All)", "Non-Rept Positions-Long (All)", "Nonrept Positions-Long (All)"],
        "nr_short": ["Nonreportable Positions-Short (All)", "Non-Rept Positions-Short (All)", "Nonrept Positions-Short (All)"],
    },
}


def parse_cot_csv(text: str, schema: str = "legacy") -> List[Dict]:
    """
    Parse teks CSV laporan COT (legacy ATAU tff) menjadi daftar row dict.

    Row dict keys: name, code, date (datetime.date), oi, nc_long, nc_short,
    nc_spread, c_long, c_short, total_long, total_short, nr_long, nr_short,
    pct_nc_long, pct_nc_short, pct_c_long, pct_c_short (pct mungkin None utk tff).
    Baris tanpa tanggal valid di-skip.
    """
    schema_map = _SCHEMAS.get(schema, _SCHEMAS["legacy"])
    reader = csv.reader(io.StringIO(text or ""))
    header = _find_header(reader)
    if header is None:
        logger.warning("COT: header kolom tidak ditemukan di file")
        return []

    colmap = _build_column_map(header)
    idx_name = _pick_col(colmap, "Market and Exchange Names", "Market_And_Exchange_Names")
    idx_code = _pick_col(colmap, "CFTC Contract Market Code", "CFTC Market Code in Initials")
    idx_date = _pick_col(colmap, *_DATE_COLUMNS)
    idx_oi = _pick_col(colmap, "Open Interest (All)", "Open Interest", "Open_Interest_All")

    if idx_name is None or idx_date is None or idx_oi is None:
        logger.warning("COT: kolom wajib tidak lengkap di header file")
        return []

    idx = {}
    for key, candidates in schema_map.items():
        idx[key] = _pick_col(colmap, *candidates)

    rows: List[Dict] = []
    for row in reader:
        if len(row) <= max(idx_name, idx_date, idx_oi):
            continue
        name = (row[idx_name] or "").strip()
        if not name:
            continue
        date = _parse_report_date(row[idx_date])
        if date is None:
            continue
        oi = _to_int(row[idx_oi]) if idx_oi is not None else None
        rows.append({
            "name": name,
            "code": (row[idx_code].strip() if idx_code is not None and len(row) > idx_code else ""),
            "date": date,
            "oi": oi,
            "nc_long": _to_int(row[idx["nc_long"]]) if idx.get("nc_long") is not None and len(row) > idx["nc_long"] else None,
            "nc_short": _to_int(row[idx["nc_short"]]) if idx.get("nc_short") is not None and len(row) > idx["nc_short"] else None,
            "nc_spread": _to_int(row[idx["nc_spread"]]) if idx.get("nc_spread") is not None and len(row) > idx["nc_spread"] else None,
            "c_long": _to_int(row[idx["c_long"]]) if idx.get("c_long") is not None and len(row) > idx["c_long"] else None,
            "c_short": _to_int(row[idx["c_short"]]) if idx.get("c_short") is not None and len(row) > idx["c_short"] else None,
            "total_long": _to_int(row[idx["total_long"]]) if idx.get("total_long") is not None and len(row) > idx["total_long"] else None,
            "total_short": _to_int(row[idx["total_short"]]) if idx.get("total_short") is not None and len(row) > idx["total_short"] else None,
            "nr_long": _to_int(row[idx["nr_long"]]) if idx.get("nr_long") is not None and len(row) > idx["nr_long"] else None,
            "nr_short": _to_int(row[idx["nr_short"]]) if idx.get("nr_short") is not None and len(row) > idx["nr_short"] else None,
            "pct_nc_long": _to_int(row[idx["pct_nc_long"]]) if idx.get("pct_nc_long") is not None and len(row) > idx["pct_nc_long"] else None,
            "pct_nc_short": _to_int(row[idx["pct_nc_short"]]) if idx.get("pct_nc_short") is not None and len(row) > idx["pct_nc_short"] else None,
            "pct_c_long": _to_int(row[idx["pct_c_long"]]) if idx.get("pct_c_long") is not None and len(row) > idx["pct_c_long"] else None,
            "pct_c_short": _to_int(row[idx["pct_c_short"]]) if idx.get("pct_c_short") is not None and len(row) > idx["pct_c_short"] else None,
        })
    return rows


def parse_legacy_csv(text: str) -> List[Dict]:
    """Wrapper parse_cot_csv dengan format legacy (backward-compat)."""
    return parse_cot_csv(text, schema="legacy")


def _extract_txt_from_zip(raw: bytes) -> str:
    """Ambil konten file annual.txt / FinFutYY.txt (file .txt pertama) dari zip."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        target = next((n for n in names if n.lower().endswith("annual.txt")), None)
        if target is None:
            target = next((n for n in names if n.lower().endswith(".txt")), None)
        if target is None:
            raise ValueError("Zip COT tidak berisi file .txt")
        return zf.read(target).decode("utf-8", errors="replace")


def _fetch_rows(report_type: str, year: Optional[int] = None, force: bool = False) -> List[Dict]:
    """Download + parse arsip tahunan satu tipe laporan (legacy/tff)."""
    year = year or datetime.now().year
    key = (report_type, year)
    with _mem_lock:
        cached = _mem_cache.get(key)
        if cached and not force and time.time() - cached[0] < _MEM_TTL_SECONDS:
            return cached[1]

    prefix = _ARCHIVE_PREFIX[report_type]
    text = None
    last_err = None
    for url_tpl in _ARCHIVE_URLS:
        url = url_tpl.format(prefix=prefix, year=year)
        try:
            resp = _session().get(url, timeout=DOWNLOAD_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                text = _extract_txt_from_zip(resp.content)
                break
        except Exception as e:
            last_err = e
            logger.debug(f"COT: download gagal {url}: {e}")

    if text is None:
        logger.error(f"COT: semua URL arsip {report_type} {year} gagal ({last_err})")
        return []

    rows = parse_cot_csv(text, schema=report_type)
    if not rows:
        logger.warning(f"COT: arsip {report_type} {year} terunduh tapi tidak ada row valid")
        return []

    with _mem_lock:
        _mem_cache[key] = (time.time(), rows)
    return rows


def fetch_year_rows(year: Optional[int] = None, force: bool = False) -> List[Dict]:
    """Arsip COT tahun berjalan format legacy futures-only (annual.txt).

    Cache dalam memori 12 jam (arsip berubah 1x/minggu) — panggilan kedua di
    hari yang sama tidak mendownload ulang. Mengembalikan [] bila gagal total.
    """
    return _fetch_rows("legacy", year, force)


def fetch_tff_rows(year: Optional[int] = None, force: bool = False) -> List[Dict]:
    """Arsip COT tahun berjalan format Traders in Financial Futures (TFF).

    Berisi market financial (USD INDEX, treasury, FX) yang tidak ada di format
    legacy. Cache memori 12 jam sama seperti fetch_year_rows.
    """
    return _fetch_rows("tff", year, force)


# ===================== PENCOCOKAN INSTRUMEN =====================

def resolve_instrument(text: str) -> Optional[Dict]:
    """Cocokkan input user (mis. 'gold', 'XAU/USD', 'eur') ke konfigurasi COT."""
    norm = _norm(text)
    if not norm:
        return None
    for cfg in COT_INSTRUMENTS:
        for alias in cfg["aliases"]:
            if _norm(alias) == norm:
                return cfg
    # Fallback: substring — 'eurusd' cocok walaupun user ketik 'eur'. Hanya
    # untuk alias >= 3 karakter: alias pendek (mis. '1m') terlalu mudah cocok
    # di tengah kata lain ('1000') dan menghasilkan instrumen yang salah.
    for cfg in COT_INSTRUMENTS:
        for alias in cfg["aliases"]:
            a = _norm(alias)
            if len(a) >= 3 and a in norm:
                return cfg
    return None


# Prefiks kontrak kecil yang harus kalah dari kontrak standar (skor -2).
_MINOR_CONTRACT_PREFIXES = ("emicro", "emini", "micro", "mini", "nano")


def _strip_minor_prefix(name_norm: str) -> str:
    """Buang prefiks kontrak kecil (e-mini/micro/nano) untuk penilaian prefix."""
    for p in _MINOR_CONTRACT_PREFIXES:
        if name_norm.startswith(p):
            return name_norm[len(p):]
    return name_norm


def _match_score(name_norm: str, config: Dict) -> int:
    """Skor kecocokan satu market terhadap konfigurasi instrumen.

    Lebih tinggi = lebih representatif. Komponen:
    +4  nama persis kata kunci
    +3  nama dimulai kata kunci (mis. 'GOLD - COMMODITY EXCHANGE INC.')
    +2  dimulai kata kunci setelah prefiks micro/mini/e-mini
    +1  kata kunci muncul di dalam nama
    +2  tiap kata kunci 'prefer' muncul (bursa utama kontrak standar)
    −3  tiap kata kunci 'avoid' muncul (mis. 'perp', 'diff')
    −2  nama diawali prefiks micro/mini/nano (kontrak kecil)
    """
    score = 0
    for kw in config.get("keywords") or []:
        k = _norm(kw)
        if name_norm == k:
            score += 4
        elif name_norm.startswith(k):
            score += 3
        elif _strip_minor_prefix(name_norm).startswith(k):
            score += 2
        elif k in name_norm:
            score += 1
    for pref in config.get("prefer") or []:
        if _norm(pref) in name_norm:
            score += 2
    for av in config.get("avoid") or []:
        if _norm(av) in name_norm:
            score -= 3
    if any(name_norm.startswith(p) for p in _MINOR_CONTRACT_PREFIXES):
        score -= 2
    return score


def _pick_best_row(candidates: List[Dict], config: Dict) -> Optional[Dict]:
    """Pilih kontrak paling representatif: skor kecocokan dulu, lalu OI terbesar."""
    if not candidates:
        return None
    scored = [
        (r, _match_score(_norm(r.get("name", "")), config))
        for r in candidates
    ]
    best_score = max(s for _, s in scored)
    best = [r for r, s in scored if s == best_score]
    if len(best) == 1:
        return best[0]
    with_oi = [r for r in best if r.get("oi")]
    if with_oi:
        return max(with_oi, key=lambda r: r["oi"] or 0)
    return best[0]


def extract_market(rows: List[Dict], config: Dict) -> Optional[Dict]:
    """
    Ekstrak data COT minggu terbaru (+ minggu sebelumnya) untuk satu instrumen.

    Args:
        rows: hasil parse_legacy_csv (SEMUA market, SEMUA minggu).
        config: entri dari COT_INSTRUMENTS.

    Returns:
        Dict ringkas siap diformat, atau None bila market tidak ditemukan.
    """
    keywords = [_norm(k) for k in config["keywords"]]
    candidates = [
        r for r in rows
        if any(kw in _norm(r.get("name", "")) for kw in keywords)
    ]
    if not candidates:
        return None

    dates = sorted({r["date"] for r in candidates if r.get("date")})
    if not dates:
        return None
    latest = dates[-1]
    prev = dates[-2] if len(dates) > 1 else None

    def _row_for(d: Optional[datetime.date]) -> Optional[Dict]:
        if d is None:
            return None
        return _pick_best_row([r for r in candidates if r.get("date") == d], config)

    cur = _row_for(latest)
    prev_row = _row_for(prev)
    if cur is None:
        return None

    def _net(r: Optional[Dict], long_key: str, short_key: str) -> Optional[int]:
        if not r:
            return None
        long_v = r.get(long_key)
        short_v = r.get(short_key)
        if long_v is None or short_v is None:
            return None
        return long_v - short_v

    def _change(cur_net: Optional[int], prev_net: Optional[int]) -> Optional[int]:
        if cur_net is None or prev_net is None:
            return None
        return cur_net - prev_net

    nc_net = _net(cur, "nc_long", "nc_short")
    c_net = _net(cur, "c_long", "c_short")
    nc_prev_net = _net(prev_row, "nc_long", "nc_short")
    c_prev_net = _net(prev_row, "c_long", "c_short")

    return {
        "display": config["display"],
        "keywords": config["keywords"],
        "market_name": cur["name"],
        "report_date": latest,
        "open_interest": cur["oi"],
        "noncommercial": {
            "long": cur.get("nc_long"),
            "short": cur.get("nc_short"),
            "net": nc_net,
            "change": _change(nc_net, nc_prev_net),
            "pct_long": cur.get("pct_nc_long"),
            "pct_short": cur.get("pct_nc_short"),
        },
        "commercial": {
            "long": cur.get("c_long"),
            "short": cur.get("c_short"),
            "net": c_net,
            "change": _change(c_net, c_prev_net),
            "pct_long": cur.get("pct_c_long"),
            "pct_short": cur.get("pct_c_short"),
        },
        "nonreportable": {
            "long": cur.get("nr_long"),
            "short": cur.get("nr_short"),
        },
        "prev_week": {
            "date": prev,
            "noncommercial_net": nc_prev_net,
            "commercial_net": c_prev_net,
        } if prev else None,
    }


# ===================== KONVERSI JSON (cache Supabase) =====================
# Data COT memuat datetime.date yang tidak bisa diserialisasi JSONB. Konversi
# eksplisit saat simpan/baca cache agar round-trip aman.

def cot_data_to_json(data: Dict) -> Dict:
    """Data COT → dict JSON-safe (date → ISO string)."""
    out = dict(data)
    if isinstance(out.get("report_date"), date):
        out["report_date"] = out["report_date"].isoformat()
    prev = out.get("prev_week")
    if isinstance(prev, dict) and isinstance(prev.get("date"), date):
        prev["date"] = prev["date"].isoformat()
    return out


def cot_data_from_json(data: Dict) -> Dict:
    """Kebalikan cot_data_to_json — ISO string → datetime.date."""
    out = dict(data)
    rd = out.get("report_date")
    if isinstance(rd, str):
        try:
            out["report_date"] = datetime.fromisoformat(rd).date()
        except ValueError:
            pass
    prev = out.get("prev_week")
    if isinstance(prev, dict) and isinstance(prev.get("date"), str):
        try:
            prev["date"] = datetime.fromisoformat(prev["date"]).date()
        except ValueError:
            pass
    return out


# ===================== INTERPRETASI & FORMAT =====================

def interpret_cot(data: Dict) -> str:
    """
    Interpretasi berbasis aturan (deterministik, tanpa AI): posisi net
    non-commercial (speculative) & commercial (hedger) + arah perubahan.
    """
    nc = data.get("noncommercial") or {}
    cm = data.get("commercial") or {}
    nc_net = nc.get("net")
    cm_net = cm.get("net")
    lines = []

    if nc_net is not None:
        if nc_net > 0:
            lines.append(f"🟢 *Speculative (managed money):* net LONG {nc_net:+,} kontrak.")
        elif nc_net < 0:
            lines.append(f"🔴 *Speculative (managed money):* net SHORT {nc_net:+,} kontrak.")
        else:
            lines.append("⚪ *Speculative:* posisi net seimbang.")
    chg = nc.get("change")
    if chg is not None and chg != 0:
        direction = "bertambah" if chg > 0 else "berkurang"
        side = "long" if nc_net and nc_net > 0 else "short"
        lines.append(f"📈 Perubahan vs minggu lalu: {direction} {abs(chg):,} kontrak ({side}).")

    if cm_net is not None:
        if cm_net > 0:
            lines.append(f"🏭 *Commercial (hedger):* net LONG {cm_net:+,} kontrak — hedge/eksposur produsen.")
        elif cm_net < 0:
            lines.append(f"🏭 *Commercial (hedger):* net SHORT {cm_net:+,} kontrak — hedge/eksposur produsen.")

    if not lines:
        return "Data posisi belum cukup untuk interpretasi."
    lines.append(
        "💡 *Cara baca:* non-commercial (spekulatif) sering dianggap \"smart money\" "
        "jangka pendek; commercial (hedger) mencerminkan kebutuhan lindung nilai "
        "produsen/pengguna — posisi ekstrem bisa menandakan pembalikan."
    )
    return "\n".join(lines)


def format_cot_message(data: Dict) -> str:
    """Format data COT → teks Telegram (Markdown). Tanpa interpretasi AI."""
    def _fmt(v: Optional[int], suffix: str = "") -> str:
        return f"{v:+,}" if v is not None else "—" + suffix

    nc = data.get("noncommercial") or {}
    cm = data.get("commercial") or {}
    nr = data.get("nonreportable") or {}
    date = data.get("report_date")
    date_str = date.strftime("%A, %d %B %Y") if date else "—"

    lines = [
        f"📊 *COT REPORT — {data.get('display', '')}*",
        f"🗓 Posisi per: {date_str} (rilis Jumat)",
        f"💰 Open Interest: *{data.get('open_interest'):,}* kontrak" if data.get("open_interest") is not None else "💰 Open Interest: —",
        "",
        "🟢 *Non-Commercial* (speculative — managed money/hedge fund)",
        f"• Long: {_fmt(nc.get('long'))}  |  Short: {_fmt(nc.get('short'))}",
        f"• *Net: {_fmt(nc.get('net'))}*",
        f"• Perubahan mingguan: {_fmt(nc.get('change'))}",
        "",
        "🏭 *Commercial* (hedger — produsen/pengguna)",
        f"• Long: {_fmt(cm.get('long'))}  |  Short: {_fmt(cm.get('short'))}",
        f"• *Net: {_fmt(cm.get('net'))}*",
        f"• Perubahan mingguan: {_fmt(cm.get('change'))}",
        "",
        "👤 *Non-Reportable* (retail kecil)",
        f"• Long: {_fmt(nr.get('long'))}  |  Short: {_fmt(nr.get('short'))}",
        "",
    ]
    lines.append(interpret_cot(data))
    lines.append("")
    lines.append("⚠️ COT = data futures AS (bukan spot forex) — edukasi, bukan saran trading.")
    return "\n".join(lines)


def format_cot_summary(data: Dict) -> str:
    """Versi RINGKAS format_cot_message — konteks AI (morning brief / chat).

    Satu instrumen jadi 3 baris padat (posisi & perubahan net speculative +
    commercial) tanpa tabel penuh, agar beberapa instrumen muat dalam satu
    prompt tanpa membakar token.
    """
    nc = data.get("noncommercial") or {}
    cm = data.get("commercial") or {}
    date = data.get("report_date")
    date_str = date.strftime("%d %b %Y") if date else "—"

    def _fmt(v: Optional[int]) -> str:
        return f"{v:+,}" if v is not None else "—"

    return (
        f"📊 {data.get('display', '')} (posisi per {date_str}):\n"
        f"• Speculative (non-commercial): net {_fmt(nc.get('net'))} kontrak "
        f"— perubahan mingguan {_fmt(nc.get('change'))}\n"
        f"• Commercial (hedger): net {_fmt(cm.get('net'))} kontrak"
    )
