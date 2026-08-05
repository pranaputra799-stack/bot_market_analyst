"""
Layer caching untuk menyimpan data sementara dan mengurangi jumlah API calls.
Mendukung TTL (Time-To-Live) per kategori data.

Arsitektur dua lapis (hybrid) agar RAM proses bot tidak membengkak:
- L1: MemoryCache — cepat, dibatasi CACHE_MAX_ENTRIES entri + pembersihan berkala.
- L2: SupabaseCache (opsional) — persisten di tabel 'app_cache' (lihat
  migrations/supabase.sql). AI response & conversation memory ditulis di sini
  secara background (fire-and-forget) dan dibaca saat memory miss, sehingga
  cache bertahan lintas restart tanpa menambah beban RAM.
  Aman no-op bila Supabase belum dikonfigurasi / tabel belum dibuat.
"""

import hashlib
import json
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests

from config.settings import (
    CACHE_TTL_SECONDS,
    CACHE_MACRO_TTL,
    CACHE_NEWS_TTL,
    CACHE_AI_TTL,
    CACHE_MAX_ENTRIES,
    SUPABASE_URL,
    SUPABASE_KEY,
    SUPABASE_CACHE_ENABLED,
)

logger = logging.getLogger(__name__)


class MemoryCache:
    """Simple in-memory cache with TTL support + batas jumlah entri (FIFO)."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._max_entries = max_entries
        self._next_order = 0  # urutan insert monotonik untuk eviction FIFO deterministik

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        if key in self._cache:
            entry = self._cache[key]
            if time.time() < entry["expires"]:
                return entry["value"]
            else:
                del self._cache[key]
        return None

    def set(self, key: str, value: Any, ttl: int = CACHE_TTL_SECONDS):
        """Set value in cache with TTL. Evict entri terlama jika cache penuh."""
        if self._max_entries > 0 and key not in self._cache:
            self._evict_if_needed()
        entry = self._cache.get(key)
        if entry is None:
            entry = {"order": self._next_order}
            self._next_order += 1
            self._cache[key] = entry
        entry["value"] = value
        entry["expires"] = time.time() + ttl
        entry["created"] = time.time()

    def delete(self, key: str):
        """Delete a key from cache."""
        self._cache.pop(key, None)

    def clear(self):
        """Clear all cache."""
        self._cache.clear()

    def cleanup_expired(self):
        """Remove all expired entries (dipanggil berkala oleh job scheduler)."""
        now = time.time()
        expired = [k for k, v in self._cache.items() if now >= v["expires"]]
        for k in expired:
            del self._cache[k]
        return len(expired)

    def _evict_if_needed(self):
        """Buang entri expired dulu, lalu yang paling lama di-insert (FIFO)."""
        if len(self._cache) < self._max_entries:
            return
        self.cleanup_expired()
        while self._cache and len(self._cache) >= self._max_entries:
            oldest = min(self._cache, key=lambda k: self._cache[k].get("order", 0))
            del self._cache[oldest]

    def get_stats(self) -> Dict:
        """Get cache statistics."""
        now = time.time()
        total = len(self._cache)
        expired = sum(1 for v in self._cache.values() if now >= v["expires"])
        return {
            "total_entries": total,
            "expired_entries": expired,
            "active_entries": total - expired,
        }


# Global cache instance (L1 — memori, cepat)
cache = MemoryCache()


class SupabaseCache:
    """
    Lapisan cache persisten di Supabase (PostgREST REST API) — L2.

    - get(): dibaca saat memory miss (AI response / conversation memory).
    - set(): ditulis background (thread daemon) agar tidak memblokir request.
    - Aman no-op bila Supabase tidak dikonfigurasi atau tabel 'app_cache'
      belum dibuat (fallback murni memory, bot tetap jalan normal).
    """

    TABLE = "app_cache"

    def __init__(self, url: str = "", key: str = "", enabled: bool = False):
        self.url = url.rstrip("/")
        self.key = key
        self.enabled = bool(enabled and url and key)
        self._warned = False
        self._lock = threading.Lock()

    def _headers(self) -> Dict:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def _warn_once(self, detail: Any):
        """Log warning hanya sekali agar log tidak banjir saat L2 mati."""
        with self._lock:
            if not self._warned:
                self._warned = True
                logger.warning(
                    f"Supabase cache unavailable ({detail}) — fallback ke memory-only"
                )
            else:
                logger.debug(f"Supabase cache masih tidak tersedia: {detail}")

    # ── Baca ───────────────────────────────────────────────────

    def get(self, cache_key: str) -> Optional[Any]:
        """Ambil nilai dari tabel; None jika tidak ada / expired / gagal.

        CATATAN: PostgREST mengembalikan kolom jsonb SUDAH ter-parse (bukan
        string JSON), jadi nilai dari resp.json() langsung dipakai apa adanya
        tanpa json.loads lagi (double-parse justru error untuk str/list).
        """
        if not self.enabled:
            return None
        try:
            q = urllib.parse.quote(cache_key, safe="")
            resp = requests.get(
                f"{self.url}/rest/v1/{self.TABLE}?key=eq.{q}&select=value,expires_at",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code != 200:
                self._warn_once(f"HTTP {resp.status_code}")
                return None
            rows = resp.json()
            if not rows:
                return None
            row = rows[0]
            expires = row.get("expires_at")
            if expires:
                exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if exp_dt <= datetime.now(timezone.utc):
                    return None
            return row.get("value")
        except Exception as e:
            self._warn_once(e)
            return None

    # ── Tulis (fire-and-forget) ────────────────────────────────

    def set(self, cache_key: str, value: Any, ttl: int):
        """Simpan nilai (upsert) di background thread."""
        if not self.enabled:
            return
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        payload = {
            "key": cache_key,
            "value": json.dumps(value, ensure_ascii=False),
            "expires_at": expires_at.isoformat(),
        }
        threading.Thread(
            target=self._set_worker,
            args=(payload,),
            daemon=True,
            name="supabase-cache-set",
        ).start()

    def _set_worker(self, payload: Dict):
        try:
            headers = {**self._headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            requests.post(
                f"{self.url}/rest/v1/{self.TABLE}",
                json=payload,
                headers=headers,
                timeout=10,
            )
        except Exception as e:
            self._warn_once(e)

    def delete(self, cache_key: str):
        """Hapus key di background thread."""
        if not self.enabled:
            return
        threading.Thread(
            target=self._delete_worker,
            args=(cache_key,),
            daemon=True,
            name="supabase-cache-del",
        ).start()

    def _delete_worker(self, cache_key: str):
        try:
            q = urllib.parse.quote(cache_key, safe="")
            requests.delete(
                f"{self.url}/rest/v1/{self.TABLE}?key=eq.{q}",
                headers=self._headers(),
                timeout=10,
            )
        except Exception as e:
            self._warn_once(e)

    def cleanup_expired(self):
        """Hapus baris yang sudah kedaluwarsa (dipanggil job scheduler)."""
        if not self.enabled:
            return
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            requests.delete(
                f"{self.url}/rest/v1/{self.TABLE}?expires_at=lt.{now_iso}",
                headers=self._headers(),
                timeout=10,
            )
        except Exception as e:
            self._warn_once(e)


# Instance global L2 — aktif hanya jika Supabase dikonfigurasi & toggle on
persistent = SupabaseCache(
    url=SUPABASE_URL,
    key=SUPABASE_KEY,
    enabled=SUPABASE_CACHE_ENABLED,
)


def make_cache_key(*args, **kwargs) -> str:
    """Generate a cache key from arguments."""
    raw = str(args) + str(sorted(kwargs.items()))
    return hashlib.md5(raw.encode()).hexdigest()


def cached(ttl: int = CACHE_TTL_SECONDS):
    """Decorator untuk caching function results (memory L1)."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            key = f"{func.__name__}:{make_cache_key(*args, **kwargs)}"
            result = cache.get(key)
            if result is not None:
                return result
            result = func(*args, **kwargs)
            cache.set(key, result, ttl)
            return result
        return wrapper
    return decorator


# ===================== CACHE AI RESPONSE (L1 + L2) =====================
# AI response adalah objek cache terbesar (hingga ribuan karakter per entri).
# Disimpan dua lapis: memori (cepat, TTL pendek) + Supabase (persisten).

def get_cached_ai_response(prompt: str) -> Optional[str]:
    """Get cached AI response — cek memori dulu, lalu Supabase (L2)."""
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    key = f"ai:{prompt_hash}"

    cached = cache.get(key)
    if cached is not None:
        return cached

    if persistent.enabled:
        cached = persistent.get(key)
        if cached is not None:
            cache.set(key, cached, CACHE_AI_TTL)  # isi ulang L1
            return cached
    return None


def set_cached_ai_response(prompt: str, response: str):
    """Cache AI response ke memori + Supabase (background, jika L2 aktif)."""
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    key = f"ai:{prompt_hash}"
    cache.set(key, response, CACHE_AI_TTL)
    if persistent.enabled:
        persistent.set(key, response, CACHE_AI_TTL)


def cleanup_all():
    """Bersihkan cache kedaluwarsa di kedua lapisan (L1 + L2)."""
    removed = cache.cleanup_expired()
    persistent.cleanup_expired()
    if removed:
        logger.debug(f"Memory cache cleanup: {removed} expired entries removed")


def safe_hash(text: str) -> str:
    """
    Generate a deterministic hash string using hashlib.md5.

    Mengapa tidak pakai hash() bawaan Python?
    - Python's built-in hash() bersifat non-deterministik antar proses karena
      PYTHONHASHSEED randomization (default sejak Python 3.3).
    - Menggunakan hash() untuk cache key menyebabkan cache miss setiap restart.
    - hashlib.md5() selalu menghasilkan nilai yang sama untuk input yang sama.

    Args:
        text: String yang akan di-hash

    Returns:
        Hex string MD5 hash (32 karakter)
    """
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def clean_json_response(text: str) -> str:
    """
    Ekstrak JSON bersih dari respons LLM yang mungkin mengandung markdown fences
    atau teks penjelasan tambahan.

    LLM sering membungkus JSON dengan:
    - ```json ... ```
    - ``` ... ```
    - Teks pengantar seperti "Here is the analysis:"
    - Teks penutup seperti "Let me know if you need clarification."

    Strategi:
    1. Coba strip markdown code fences terlebih dahulu.
    2. Jika tidak ada, cari substring mulai dari '{' atau '[' sampai '}' atau ']'.
    3. Return string kosong jika tidak ada JSON yang valid ditemukan.

    Args:
        text: Raw LLM response string

    Returns:
        JSON string yang sudah dibersihkan, atau string kosong jika gagal
    """
    if not text:
        return ""

    stripped = text.strip()

    # Coba extract dari markdown code fence ```json ... ```
    if "```json" in stripped:
        parts = stripped.split("```json", 1)
        if len(parts) > 1:
            inner = parts[1]
            if "```" in inner:
                return inner.split("```", 1)[0].strip()

    # Coba extract dari code fence ``` ... ```
    if "```" in stripped:
        parts = stripped.split("```", 1)
        if len(parts) > 1:
            inner = parts[1]
            if "```" in inner:
                candidate = inner.split("```", 1)[0].strip()
                if candidate.startswith("{") or candidate.startswith("["):
                    return candidate

    # Cari JSON object ({...}) dengan bracket matching
    start = stripped.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(stripped[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
            if not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return stripped[start:i + 1]

    # Cari JSON array ([...]) dengan bracket matching
    start = stripped.find("[")
    if start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(stripped[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
            if not in_string:
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        return stripped[start:i + 1]

    # Fallback: kembalikan string asli (akan gagal di json.loads, tapi logging akan menangkap)
    return stripped
