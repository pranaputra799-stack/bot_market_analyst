"""
Layer caching untuk menyimpan data sementara dan mengurangi jumlah API calls.
Mendukung TTL (Time-To-Live) per kategori data.
"""
import time
import hashlib
import json
from typing import Any, Dict, Optional
from functools import lru_cache
from config.settings import (
    CACHE_TTL_SECONDS,
    CACHE_MACRO_TTL,
    CACHE_NEWS_TTL,
    CACHE_AI_TTL,
)


class MemoryCache:
    """Simple in-memory cache with TTL support."""

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

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
        """Set value in cache with TTL."""
        self._cache[key] = {
            "value": value,
            "expires": time.time() + ttl,
            "created": time.time()
        }

    def delete(self, key: str):
        """Delete a key from cache."""
        self._cache.pop(key, None)

    def clear(self):
        """Clear all cache."""
        self._cache.clear()

    def cleanup_expired(self):
        """Remove all expired entries."""
        now = time.time()
        expired = [k for k, v in self._cache.items() if now >= v["expires"]]
        for k in expired:
            del self._cache[k]

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


# Global cache instance
cache = MemoryCache()


def make_cache_key(*args, **kwargs) -> str:
    """Generate a cache key from arguments."""
    raw = str(args) + str(sorted(kwargs.items()))
    return hashlib.md5(raw.encode()).hexdigest()


def cached(ttl: int = CACHE_TTL_SECONDS):
    """Decorator untuk caching function results."""
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


# Khusus untuk cache AI response (berdasarkan hash prompt)
def get_cached_ai_response(prompt: str) -> Optional[str]:
    """Get cached AI response for identical prompts."""
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    return cache.get(f"ai:{prompt_hash}")


def set_cached_ai_response(prompt: str, response: str):
    """Cache AI response."""
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    cache.set(f"ai:{prompt_hash}", response, CACHE_AI_TTL)


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
