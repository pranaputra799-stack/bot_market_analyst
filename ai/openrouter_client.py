"""
OpenRouter API Client - Wrapper untuk OpenRouter API.
OpenRouter menyediakan akses ke 100+ model dari berbagai provider,
termasuk banyak model GRATIS (suffix :free atau harga $0).
"""
import json
import logging
import threading
import time
from typing import Dict, Optional, List

import requests

from config.settings import OPENROUTER_API_KEY

logger = logging.getLogger(__name__)


# Keyword model yang BUKAN chat text (musik/audio/image) — hindari dipakai fallback
_NON_CHAT_KEYWORDS = ("lyria", "audio", "tts", "musicgen", "stable-audio")


def _is_zero_price(value) -> bool:
    """Cek apakah harga model $0 (free)."""
    try:
        return value is not None and float(value) == 0.0
    except (TypeError, ValueError):
        return False


class OpenRouterClient:
    """
    Client untuk OpenRouter API.
    Memberikan akses ke berbagai model premium secara gratis (rate-limited).
    """

    BASE_URL = "https://openrouter.ai/api/v1"

    # Daftar model free statis (fallback bila API discovery gagal).
    # Endpoint lain bisa diblokir guardrail privacy akun kecuali data policy
    # diubah di https://openrouter.ai/settings/privacy (Allow all).
    FREE_MODELS = [
        "openrouter/free",  # auto-router: pilih model free terbaik otomatis
        "inclusionai/ling-3.0-flash:free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",  # 1M context
        "nvidia/nemotron-3-super-120b-a12b:free",
        "openai/gpt-oss-20b:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
    ]

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or OPENROUTER_API_KEY
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/market-ai-bot",
            "X-Title": "Market AI Bot",
        }

    def chat_completion(
        self,
        messages: List[Dict],
        model: str = "openrouter/free",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> Optional[Dict]:
        """
        Kirim chat completion ke OpenRouter.

        Args:
            messages: List of message dicts
            model: Model ID (bisa free model)
            temperature: Creativity (0-1)
            max_tokens: Max response tokens

        Returns:
            Response dict atau None
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            resp = requests.post(
                f"{self.BASE_URL}/chat/completions",
                json=payload,
                headers=self.headers,
                timeout=30,
            )

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                logger.warning("OpenRouter rate limit reached")
                return None
            else:
                logger.warning(f"OpenRouter error: {resp.status_code}")
                return None

        except Exception as e:
            logger.error(f"OpenRouter request failed: {e}")
            return None

    def list_models(self) -> List[Dict]:
        """Dapatkan daftar model yang tersedia di OpenRouter."""
        try:
            resp = requests.get(
                f"{self.BASE_URL}/models",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("data", [])
        except Exception as e:
            logger.error(f"Failed to list OpenRouter models: {e}")
        return []


# ===================== FREE MODEL DISCOVERY =====================

_free_models_cache: Dict[str, object] = {"ts": 0.0, "models": []}
_free_models_refreshing = False
_FREE_MODELS_TTL = 24 * 3600  # refresh daftar free model 1x sehari


def _fetch_free_models() -> List[str]:
    """
    Fetch daftar model free dari API OpenRouter (sinkron, dipanggil di thread).
    Hanya model chat text yang dimasukkan (buang model audio/video).

    Returns:
        List model ID free, atau daftar statis jika fetch gagal / kosong
    """
    discovered: List[str] = []
    try:
        resp = requests.get(
            f"{OpenRouterClient.BASE_URL}/models",
            timeout=10,
        )
        if resp.status_code == 200:
            for m in resp.json().get("data", []):
                mid = m.get("id", "")
                if not mid:
                    continue
                low = mid.lower()
                if any(kw in low for kw in _NON_CHAT_KEYWORDS):
                    continue  # model audio/music, bukan chat text
                pricing = m.get("pricing", {}) or {}
                if not (
                    mid.endswith(":free")
                    or _is_zero_price(pricing.get("prompt"))
                    or _is_zero_price(pricing.get("completion"))
                ):
                    continue

                # Hanya model chat text (buang model audio/video murni)
                arch = m.get("architecture", {}) or {}
                in_mods = arch.get("input_modalities") or []
                out_mods = arch.get("output_modalities") or []
                if in_mods and out_mods and ("text" not in in_mods or "text" not in out_mods):
                    continue

                discovered.append(mid)
    except Exception as e:
        logger.warning(f"Failed to fetch OpenRouter free models: {e}")

    if not discovered:
        return list(OpenRouterClient.FREE_MODELS)

    # Dedupe + urutkan, prioritaskan auto-router di depan
    ordered: List[str] = []
    seen: set = set()
    for m in ["openrouter/free"] + discovered:
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered[:20]  # batasi agar fallback tidak lambat saat semua model gagal


def _refresh_free_models_in_background():
    """Refresh daftar free model di thread background (tidak memblokir event loop)."""
    global _free_models_refreshing
    if _free_models_refreshing:
        return
    _free_models_refreshing = True

    def _do_refresh():
        try:
            result = _fetch_free_models()
            _free_models_cache["models"] = result
            _free_models_cache["ts"] = time.time()
            logger.info(f"OpenRouter free models refreshed: {len(result)} models")
        except Exception as e:
            logger.warning(f"Free models refresh failed: {e}")
        finally:
            global _free_models_refreshing
            _free_models_refreshing = False

    threading.Thread(target=_do_refresh, daemon=True, name="openrouter-free-models").start()


def get_free_models(refresh: bool = False) -> List[str]:
    """
    Ambil daftar model free (suffix :free atau harga $0) dari API OpenRouter.

    Fungsi ini TIDAK PERNAH memblokir request path:
    - Jika cache fresh -> langsung pakai cache.
    - Jika cache basi -> refresh dijalankan di thread background, dan request
      saat ini memakai cache lama / daftar statis.
    - refresh=True -> fetch sinkron (dipakai saat startup/test).

    Returns:
        List model ID free (chat text saja, sudah di-dedupe)
    """
    now = time.time()
    cached = _free_models_cache.get("models") or []
    cache_fresh = cached and (now - _free_models_cache.get("ts", 0.0)) < _FREE_MODELS_TTL

    if cache_fresh:
        return list(cached)

    if refresh:
        # Fetch sinkron (startup/test)
        result = _fetch_free_models()
        _free_models_cache["models"] = result
        _free_models_cache["ts"] = time.time()
        return list(result)

    # Refresh background, pakai daftar lama/statis dulu agar tidak memblokir
    _refresh_free_models_in_background()
    return list(cached) or list(OpenRouterClient.FREE_MODELS)
