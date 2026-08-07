"""
FACT CHECK — Verifikasi deterministik angka jawaban AI terhadap data terhitung.

Jaring pengaman anti-halusinasi lapis TERAKHIR: prompt sudah melarang model
mengarang angka, tetapi model kecil/cepat (free tier) kadang tetap menyebut
harga/level yang tidak ada di data. Modul ini mengekstrak semua angka yang
"mirip harga/level" dari jawaban AI lalu mencocokkannya dengan angka yang
BENAR-BENAR ada di data (hasil perhitungan indikator lokal + data pasar +
pertanyaan & riwayat user). Angka yang tidak cocok dilaporkan sebagai catatan
peringatan yang ditambahkan ke akhir jawaban.

Desain:
- Murni deterministik (regex + aritmetika) — tidak ada panggilan LLM, cepat, gratis.
- Toleransi relatif kecil (0.3%) agar pembulatan wajar (1.0855 vs 1.0850) tidak
  dianggap salah, tetapi angka yang jelas beda (error 50 pip / target karangan)
  tetap terdeteksi.
- Hanya memeriksa angka "mirip harga/level": persentase, tahun, jam, dan angka
  kecil (< 20 bulat) yang umumnya indeks/hitungan diabaikan.
- Koma/titik ditafsirkan ganda (ribuan vs desimal) agar format "2,350" (gold)
  maupun "58,3" (desimal Indonesia) tetap dikenali.
"""

import re
from datetime import datetime
from typing import List, Sequence, Set, Tuple

# Token angka: 123, 1.0850, 2350.5, 1,085.50, 58,3 — tangkap juga suffix % untuk
# mengecualikan persentase. Lookbehind mencegah match pada jam ("15" di "15:30").
_NUMBER_TOKEN_RE = re.compile(r"(?<![\w:])(\d+(?:[.,]\d+)+|\d+)(%?)")

# Rentang nilai yang wajar untuk harga/level (di luar ini bukan harga).
_MIN_PRICE = 0.5
_MAX_PRICE = 1_000_000

# Toleransi relatif (fraksi) untuk mencocokkan angka jawaban vs data.
# 0.3% — cukup untuk pembulatan kecil (1.0855 vs 1.0850), tapi error 50 pip
# (1.0850 vs 1.0900) tetap terdeteksi.
REL_TOLERANCE = 0.003
# Toleransi absolut minimal (untuk nilai kecil agar tidak terlalu sensitif).
ABS_TOLERANCE = 0.001

# Jumlah angka mencurigakan maksimal yang dilaporkan dalam satu catatan.
MAX_REPORTED = 6

# Awalan catatan verifikasi (dipakai untuk memotong catatan dari jawaban
# sebelum disimpan ke conversation memory — biar meta-info tidak jadi konteks).
FACT_CHECK_PREFIX = "\n\n🔎 Verifikasi Data:"


def _candidate_values(token: str) -> List[float]:
    """Kandidat nilai numerik untuk satu token (menangani koma/titik ambigu).

    "1.0850"  → [1.085]        (desimal)
    "2350"    → [2350]
    "2,350"   → [2350, 2.35]   (koma ribuan ATAU desimal)
    "58,3"    → [583, 58.3]
    "1,085.50"→ [1085.5]      (koma ribuan, titik desimal)
    "2.350,50"→ [2350.5]      (titik ribuan, koma desimal gaya Indonesia)
    "0.00123" → [0.00123]      (awalan 0. → pasti desimal, tanpa interpretasi ribuan)
    """
    if not token:
        return []

    vals: List[float] = []
    n_comma = token.count(",")
    n_dot = token.count(".")

    if n_comma == 0 and n_dot == 0:
        vals.append(float(token))
    if n_comma >= 2 and n_dot == 0:
        # "1,234,567" → ribuan
        vals.append(float(token.replace(",", "")))
    elif n_comma == 1 and n_dot == 0:
        vals.append(float(token.replace(",", "")))   # "2,350" → 2350
        vals.append(float(token.replace(",", ".")))  # "58,3"  → 58.3
    if n_dot >= 2 and n_comma == 0:
        # "1.234.567" → ribuan
        vals.append(float(token.replace(".", "")))
    elif n_dot == 1 and n_comma == 0:
        vals.append(float(token))                     # "1.0850" → 1.085
        if not token.startswith("0."):
            vals.append(float(token.replace(".", "")))  # "1.085" → 1085
    if n_dot == 1 and n_comma >= 1:
        # "1,085.50" (gaya AS) → 1085.5 | "2.350,50" (gaya ID) → 2350.5
        vals.append(float(_normalize_both_separators(token)))

    # Dedupe sambil menjaga urutan
    seen: Set[float] = set()
    out: List[float] = []
    for v in vals:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _sep_is_thousands(s: str, sep: str) -> bool:
    """True bila `sep` pertama di s bertindak sebagai pemisah RIBUAN (diikuti
    tepat 3 digit lalu bukan digit lagi): ',' di "1,085.50", '.' di "2.350,50"."""
    idx = s.find(sep)
    if idx < 0:
        return False
    tail = s[idx + 1: idx + 4]
    after = s[idx + 4: idx + 5] if len(s) > idx + 4 else ""
    return len(tail) == 3 and tail.isdigit() and not after.isdigit()


def _normalize_both_separators(token: str) -> str:
    """Normalisasi token dengan KEDUA pemisah (koma + titik)."""
    if _sep_is_thousands(token, ","):
        return token.replace(",", "")            # "1,085.50" → "1085.50"
    if _sep_is_thousands(token, "."):
        return token.replace(".", "").replace(",", ".")  # "2.350,50" → "2350.50"
    return token.replace(",", "")                # fallback: koma ribuan


_CURRENT_YEAR = datetime.now().year

def _is_year(token: str) -> bool:
    """True untuk tahun yang mungkin disebut dalam jawaban (tahun berjalan ±5).

    Rentang sengaja SEMPIT agar harga emas 4 digit (mis. 1990/2050/2080) tetap
    diperiksa sebagai harga — hanya tahun yang dekat dengan tanggal sekarang
    yang dianggap tanggal, bukan harga.
    """
    if not token.isdigit() or len(token) != 4:
        return False
    return _CURRENT_YEAR - 5 <= int(token) <= _CURRENT_YEAR + 5


def _is_integer_token(token: str) -> bool:
    return "." not in token and "," not in token


def extract_number_tokens(text: str) -> List[Tuple[str, List[float]]]:
    """Ekstrak token angka + kandidat nilai dari teks.

    Menyaring: persentase, tahun, jam, angka kecil bulat (< 20 = indeks/hitungan),
    dan nilai di luar rentang harga wajar. Mengembalikan list (token, [nilai...]).
    """
    if not text:
        return []

    out: List[Tuple[str, List[float]]] = []
    for m in _NUMBER_TOKEN_RE.finditer(text):
        token = m.group(1)
        suffix = m.group(2)

        # Persentase (40%) — bukan harga
        if suffix == "%":
            continue
        # Tahun (2026) — bukan harga
        if _is_year(token):
            continue
        # Jam "19:30" — angka sebelum/berdampingan titik dua
        if m.end() < len(text) and text[m.end()] == ":":
            continue

        vals = _candidate_values(token)
        # Angka bulat kecil (indeks/hitungan seperti "3 skenario", "2 jam")
        if _is_integer_token(token) and all(v < 20 for v in vals):
            continue
        # Di luar rentang harga wajar
        vals = [v for v in vals if _MIN_PRICE <= v <= _MAX_PRICE]
        if not vals:
            continue

        out.append((token, vals))
    return out


def build_ground_truth(data_texts: Sequence[str]) -> Set[float]:
    """Kumpulkan semua nilai angka dari data yang DIBERIKAN ke LLM."""
    gt: Set[float] = set()
    for text in data_texts:
        if not text:
            continue
        for _, vals in extract_number_tokens(text):
            gt.update(vals)
    return gt


def _close_enough(value: float, ground: float) -> bool:
    return abs(value - ground) <= max(
        REL_TOLERANCE * max(abs(value), abs(ground)), ABS_TOLERANCE
    )


def find_suspicious(answer: str, data_texts: Sequence[str]) -> List[str]:
    """Angka di jawaban yang TIDAK cocok dengan data (potensi karangan).

    Args:
        answer: Jawaban AI yang akan diverifikasi.
        data_texts: Teks data yang diberikan ke LLM (indikator, data pasar,
            pertanyaan, riwayat percakapan).

    Returns:
        List token angka mencurigakan (maksimal MAX_REPORTED), kosong jika aman.
    """
    gt = build_ground_truth(data_texts)
    if not gt:
        return []

    suspicious: List[str] = []
    for token, vals in extract_number_tokens(answer):
        matched = any(
            _close_enough(v, g) for v in vals for g in gt
        )
        if matched:
            continue
        if token not in suspicious:
            suspicious.append(token)
        if len(suspicious) >= MAX_REPORTED:
            break
    return suspicious


def build_fact_check_note(answer: str, data_texts: Sequence[str]) -> str:
    """Catatan peringatan bila jawaban memuat angka yang tidak ada di data.

    Returns:
        String catatan siap-append (dimulai baris kosong), atau "" bila aman /
        tidak ada data pembanding.
    """
    if not answer or not data_texts:
        return ""

    suspicious = find_suspicious(answer, data_texts)
    if not suspicious:
        return ""

    items = ", ".join(suspicious[:MAX_REPORTED])
    more = f" (dan {len(suspicious) - MAX_REPORTED} angka lain)" if len(suspicious) > MAX_REPORTED else ""
    return (
        f"{FACT_CHECK_PREFIX} angka berikut tidak ditemukan di data terhitung "
        f"(harga/indikator/level): {items}{more}. Kemungkinan ini perkiraan analisis, "
        f"bukan data real — mohon cek ulang sebelum mengambil keputusan."
    )


def strip_fact_check_note(text: str) -> str:
    """Potong catatan verifikasi dari jawaban (untuk disimpan ke memory bersih)."""
    if not text:
        return text
    idx = text.find(FACT_CHECK_PREFIX)
    if idx == -1:
        return text
    return text[:idx]
