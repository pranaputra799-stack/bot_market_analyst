"""
Groq API Client - Wrapper khusus untuk Groq API.
Groq dikenal dengan kecepatan super tinggi (800+ token/detik).
Provider utama karena paling responsif.
"""
import json
import logging
from typing import Dict, Optional, List

import requests

from config.settings import GROQ_API_KEY

logger = logging.getLogger(__name__)


class GroqClient:
    """
    Client khusus untuk interaksi dengan Groq API.
    Menyediakan akses ke berbagai model Groq.
    """

    BASE_URL = "https://api.groq.com/openai/v1"

    MODELS = {
        "llama-3.3-70b": "llama-3.3-70b-versatile",
        "llama-3.1-8b": "llama-3.1-8b-instant",
        "gpt-oss-120b": "openai/gpt-oss-120b",
        "gpt-oss-20b": "openai/gpt-oss-20b",
    }

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or GROQ_API_KEY
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat_completion(
        self,
        messages: List[Dict],
        model: str = "llama-3.3-70b",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> Optional[Dict]:
        """
        Kirim chat completion ke Groq.

        Args:
            messages: List of message dicts [{"role": "...", "content": "..."}]
            model: Model name key from MODELS dict
            temperature: Creativity (0-1), lower = lebih faktual
            max_tokens: Max token response

        Returns:
            Response dict atau None
        """
        model_id = self.MODELS.get(model, self.MODELS["llama-3.3-70b"])

        payload = {
            "model": model_id,
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
                logger.warning("Groq rate limit reached")
                return None
            else:
                logger.warning(f"Groq error: {resp.status_code} - {resp.text[:200]}")
                return None

        except Exception as e:
            logger.error(f"Groq request failed: {e}")
            return None

    def get_available_models(self) -> List[str]:
        """Dapatkan daftar model yang tersedia."""
        try:
            resp = requests.get(
                f"{self.BASE_URL}/models",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.error(f"Failed to get Groq models: {e}")
        return list(self.MODELS.keys())
