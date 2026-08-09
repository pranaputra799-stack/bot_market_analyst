"""
Token Budget — Utilitas budget token untuk prompt LLM.

Teknik yang diadopsi dari repositori terkenal tentang penghematan token
(mis. openai-cookbook / tiktoken): hitung token input SECARA AKURAT dan
potong konteks agar muat di budget, sehingga prompt tidak membengkak dan
biaya token terkendali.

- `estimate_tokens()`: pakai tiktoken (encoding cl100k_base) bila tersedia;
  fallback ke estimasi 1 token ≈ 4 karakter agar modul tetap berfungsi tanpa
  dependensi tambahan.
- `truncate_to_budget()`: potong teks ke budget token (binary search agar
  hasil sedekat mungkin dengan budget) dengan marker "...[truncated]" agar
  jelas bahwa konten terpotong (bukan kesalahan parsing).

Tidak pernah raise: semua kegagalan (tiktoken tidak terpasang, vocab tidak
bisa diunduh, input aneh) jatuh ke estimasi karakter yang aman.
"""
import logging

logger = logging.getLogger(__name__)

# Encoding tiktoken yang paling umum (cl100k_base) — dipakai ChatGPT/GPT-4.
# Di-load LAZY (saat pertama dipakai, bukan saat import) karena
# tiktoken.get_encoding() mengunduh file vocab dari internet pada pemakaian
# pertama — lazy-load mencegah bot hang saat startup di lingkungan offline /
# jaringan lambat (hanya pemakaian pertama yang mungkin tertunda sesaat).
# Bila gagal (library tidak terpasang / vocab tidak bisa diunduh), fallback
# ke estimasi karakter (1 token ≈ 4 karakter).
_ENCODING = None
_ENCODING_TRIED = False


def _get_encoding():
    """Ambil encoding tiktoken (di-load sekali, lazy). None bila tidak tersedia."""
    global _ENCODING, _ENCODING_TRIED
    if _ENCODING_TRIED:
        return _ENCODING
    _ENCODING_TRIED = True
    try:
        import tiktoken  # type: ignore
        _ENCODING = tiktoken.get_encoding("cl100k_base")
        logger.debug("tiktoken aktif — penghitungan token akurat")
    except Exception as e:  # pragma: no cover - tergantung environment
        logger.debug(f"tiktoken tidak tersedia, pakai estimasi karakter: {e}")
        _ENCODING = None
    return _ENCODING


def estimate_tokens(text: str) -> int:
    """
    Estimasi jumlah token untuk sebuah teks.

    Args:
        text: Teks yang akan dihitung

    Returns:
        Jumlah token (>= 1). Akurat bila tiktoken tersedia; perkiraan
        (chars/4) bila tidak.
    """
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception as e:  # pragma: no cover - jaringan/vocab
            logger.debug(f"tiktoken encode gagal, fallback estimasi: {e}")
    return max(1, len(text) // 4)


def truncate_to_budget(text: str, max_tokens: int, label: str = "context") -> str:
    """
    Potong teks agar muat dalam budget token (binary search).

    Args:
        text: Teks lengkap
        max_tokens: Budget token maksimal
        label: Label pada marker "...[truncated: <label>]" untuk memudahkan
            debugging mana bagian prompt yang terpotong

    Returns:
        Teks utuh bila sudah muat, atau teks terpotong + marker.
    """
    if not text:
        return ""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    # Sisakan ruang untuk marker agar hasil akhir (prefix + marker) benar-benar
    # muat di budget token, bukan melebihinya.
    marker = f"\n...[truncated: {label}]"
    budget = max_tokens - estimate_tokens(marker)
    if budget <= 0:
        return ""

    # Binary search: cari panjang prefix terpanjang yang muat di budget.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1

    if lo <= 0:
        return ""
    return text[:lo].rstrip() + marker
