"""
OpenRouter API Client - Wrapper untuk OpenRouter API.
OpenRouter menyediakan akses ke 100+ model dari berbagai provider.
"""
import json
import logging
from typing import Dict, Optional, List

import requests

from config.settings import OPENROUTER_API_KEY

logger = logging.getLogger(__name__)


class OpenRouterClient:
    """
    Client untuk OpenRouter API.
    Memberikan akses ke berbagai model premium secara gratis (rate-limited).
    """

    BASE_URL = "https://openrouter.ai/api/v1"

    FREE_MODELS = [
        "openrouter/free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
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
