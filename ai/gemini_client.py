"""
Google Gemini API Client - Wrapper untuk Gemini API.
Gemini unggul di konteks panjang (1M token) dan analisis mendalam.
"""
import json
import logging
from typing import Dict, Optional, List

import requests

from config.settings import GEMINI_API_KEY

logger = logging.getLogger(__name__)


class GeminiClient:
    """
    Client khusus untuk interaksi dengan Google Gemini API.
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/"

    MODELS = {
        "gemini-2.0-flash": "gemini-2.0-flash",
        "gemini-1.5-flash": "gemini-2.0-flash",
        "gemini-1.5-pro": "gemini-2.0-flash",
    }

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or GEMINI_API_KEY

    def generate_content(
        self,
        prompt: str,
        model: str = "gemini-2.0-flash",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> Optional[str]:
        """
        Generate content menggunakan Gemini.

        Args:
            prompt: Text prompt
            model: Model name
            temperature: Creativity (0-1)
            max_tokens: Max output tokens

        Returns:
            Generated text atau None
        """
        model_id = self.MODELS.get(model, "gemini-2.0-flash")
        url = f"{self.BASE_URL}{model_id}:generateContent?key={self.api_key}"

        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "topP": 0.95,
            }
        }

        try:
            resp = requests.post(url, json=payload, timeout=30)

            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "")
            elif resp.status_code == 429:
                logger.warning("Gemini rate limit reached")
                return None
            else:
                logger.warning(f"Gemini error: {resp.status_code}")
                return None

        except Exception as e:
            logger.error(f"Gemini request failed: {e}")
            return None

    def count_tokens(self, text: str) -> Optional[int]:
        """Hitung jumlah token dalam text."""
        model_id = "gemini-2.0-flash"
        url = f"{self.BASE_URL}{model_id}:countTokens?key={self.api_key}"

        try:
            resp = requests.post(
                url,
                json={"contents": [{"parts": [{"text": text}]}]},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("totalTokens", 0)
        except Exception as e:
            logger.error(f"Token count failed: {e}")
        return None
