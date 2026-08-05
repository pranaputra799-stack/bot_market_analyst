"""
Conversation Memory — Memory percakapan per-user (L1 memori + L2 Supabase).

Menyimpan beberapa pertanyaan-jawaban terakhir per user agar pertanyaan
follow-up ("kalau begitu support-nya di mana?") punya konteks percakapan
sebelumnya. Riwayat otomatis kedaluwarsa setelah MEMORY_TTL detik.

Penyimpanan dua lapis:
- L1: memori (cepat) — hanya menyimpan entri aktif, dibatasi MAX_ENTRIES.
- L2: Supabase (persisten, via data.cache.persistent) — agar riwayat tidak
  menambah beban RAM proses bot dan tetap ada saat bot restart.
  Aman no-op bila Supabase tidak dikonfigurasi.

Catatan privasi: TTL 15 menit — data otomatis terhapus; tidak ada penyimpanan
permanen jangka panjang.
"""

import logging
from typing import Dict, List

from data.cache import cache, persistent

logger = logging.getLogger(__name__)

# ===================== KONFIGURASI =====================
MEMORY_TTL = 15 * 60        # 15 menit — cukup untuk follow-up, tidak menumpuk
MAX_ENTRIES = 6             # maksimal pasangan Q&A yang disimpan per user
MAX_ANSWER_CHARS = 300      # potong jawaban agar hemat token prompt
MAX_QUESTION_CHARS = 200
MAX_EXCHANGES_IN_CONTEXT = 4  # berapa pertukaran terakhir yang dimasukkan ke prompt


def _key(user_id: int) -> str:
    return f"conversation:{user_id}"


def get_history(user_id: int) -> List[Dict]:
    """
    Ambil riwayat percakapan user (terbaru di akhir list).
    Cek memori dulu; jika kosong, ambil dari Supabase (L2) dan isi ulang memori.
    """
    key = _key(user_id)
    data = cache.get(key)
    if data is not None:
        return data if isinstance(data, list) else []

    persisted = persistent.get(key)
    if isinstance(persisted, list):
        cache.set(key, persisted, MEMORY_TTL)
        return persisted
    return []


def add_exchange(user_id: int, question: str, answer: str):
    """Simpan satu pertanyaan-jawaban (memori + Supabase), potong agar hemat token."""
    history = get_history(user_id)
    history.append({
        "q": (question or "").strip()[:MAX_QUESTION_CHARS],
        "a": (answer or "").strip()[:MAX_ANSWER_CHARS],
    })
    # Hanya simpan MAX_ENTRIES terakhir
    history = history[-MAX_ENTRIES:]
    cache.set(_key(user_id), history, MEMORY_TTL)
    persistent.set(_key(user_id), history, MEMORY_TTL)
    logger.debug(f"Conversation memory updated for user {user_id}: {len(history)} entries")


def clear(user_id: int):
    """Hapus riwayat percakapan user (memori + Supabase)."""
    cache.delete(_key(user_id))
    persistent.delete(_key(user_id))


def format_history(user_id: int, max_exchanges: int = MAX_EXCHANGES_IN_CONTEXT) -> str:
    """
    Format riwayat menjadi teks untuk prompt LLM.

    Returns:
        String siap-suntik, atau "" jika tidak ada riwayat.
    """
    history = get_history(user_id)[-max_exchanges:]
    if not history:
        return ""

    lines = ["Percakapan sebelumnya (User ↔ Bot):"]
    for ex in history:
        q = ex.get("q", "")
        a = ex.get("a", "")
        if q:
            lines.append(f'User: "{q}"')
        if a:
            lines.append(f"Bot: {a}")
    return "\n".join(lines)
