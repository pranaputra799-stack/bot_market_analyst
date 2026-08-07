"""
AI Fallback Engine - Inti dari sistem AI dengan multi-provider fallback.
Mekanisme:
  OpenRouter (PRIMARY, model GRATIS :free / $0) -> Groq -> Gemini -> Cerebras -> Mistral

Setiap provider punya rate limit dan karakteristik berbeda.
Bot secara otomatis mencoba provider berikutnya jika satu provider gagal.
OpenRouter memakai daftar model free (suffix :free / $0) yang di-discovery
langsung dari API sehingga biaya AI tetap $0 selama model free tersedia.
"""
import asyncio
import json
import logging
import random
import threading
import time
from typing import Dict, List, Optional, Callable, Any

import requests

from config.settings import (
    GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY,
    CEREBRAS_API_KEY, MISTRAL_API_KEY, AI_FALLBACK_ORDER,
    AI_MAX_TOTAL_WAIT_SECONDS, AI_REQUEST_TIMEOUT, AI_MIN_INTERVAL_SECONDS,
    AI_TEMPERATURE,
)
from config.providers import PROVIDER_CONFIGS
from data.cache import get_cached_ai_response, set_cached_ai_response, safe_hash
from ai.openrouter_client import get_free_models
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)


class AIFallbackEngine:
    """
    Engine AI dengan mekanisme fallback multi-provider.
    Mencoba provider satu per satu sampai ada yang sukses.
    """

    @property
    def DEFAULT_SYSTEM(self) -> str:
        # System prompt default — konten DIAMBIL dari prompts/engine_system.txt
        # (single source of truth; loader memakai cache + fallback ke template
        # bawaan bila file tidak ada). Di-load per-akses sehingga
        # reload_prompts() (hot-reload dev) ikut menyegarkan prompt ini.
        return load_prompt("engine_system")

    # Backoff dasar (detik) sebelum dikali 2^attempt + jitter saat retry.
    BACKOFF_BASE_SECONDS = 1.0

    # Model yang pernah 404 (tidak ada / tidak gratis / diblokir guardrail)
    # di-skip selama TTL ini — mencegah request berulang ke model mati yang
    # hanya menghasilkan spam log + latensi (tanpa memperbaiki hasil).
    # Setelah TTL lewat, model dicoba lagi (berguna bila katalog/privacy berubah
    # tanpa restart bot).
    _DEAD_MODEL_TTL = 600  # 10 menit

    def __init__(self, fallback_order: Optional[List[str]] = None):
        self.fallback_order = fallback_order or AI_FALLBACK_ORDER
        self.api_keys = {
            "groq": GROQ_API_KEY,
            "gemini": GEMINI_API_KEY,
            "openrouter": OPENROUTER_API_KEY,
            "cerebras": CEREBRAS_API_KEY,
            "mistral": MISTRAL_API_KEY,
        }
        self.stats = {
            "total_requests": 0,
            "successful": 0,
            "failed": 0,
            "provider_usage": {p: 0 for p in self.fallback_order},
            "last_error": None,
        }
        # State rate-limit per provider (aman dibaca/ditulis lintas thread di GIL):
        # - _provider_cooldown: provider → timestamp sampai kapan dihindari (429).
        # - _rate_limit_hints:   provider → durasi tunggu (detik) yang diminta server.
        self._provider_cooldown: Dict[str, float] = {}
        self._rate_limit_hints: Dict[str, float] = {}
        # Blacklist model mati: model → timestamp 404 terakhir (lihat _DEAD_MODEL_TTL).
        self._dead_models: Dict[str, float] = {}
        # Throttle per-provider: jeda minimum antar request ke provider yang sama.
        # Pipeline multi-agent memanggil generate() PARALEL (asyncio.gather +
        # asyncio.to_thread) dan banyak user bisa bertanya bersamaan — tanpa
        # jeda ini, request serentak ke free tier (RPM ketat) langsung 429 dan
        # cepat menghabiskan kuota harian.
        self._throttle_locks: Dict[str, threading.Lock] = {}
        self._last_request_at: Dict[str, float] = {}
        # Override jeda antar request (detik). None = pakai min_interval_seconds
        # dari config provider; 0 = nonaktif (dipakai test agar cepat).
        # Bisa di-override via env AI_MIN_INTERVAL_SECONDS tanpa redeploy.
        self.throttle_min_interval_override: Optional[float] = None
        if AI_MIN_INTERVAL_SECONDS and AI_MIN_INTERVAL_SECONDS > 0:
            self.throttle_min_interval_override = AI_MIN_INTERVAL_SECONDS
        self._order_index: Dict[str, int] = {p: i for i, p in enumerate(self.fallback_order)}
        # Catatan thread-safety: system_override & max_tokens dikirim PER-REQUEST
        # (bukan state instance), sehingga generate() aman dipanggil paralel dari
        # banyak thread (asyncio.to_thread di handlers/sentiment/agents) tanpa
        # risiko saling menimpa system prompt.

        # Warm up daftar model free OpenRouter di background (non-blocking)
        # agar fallback ke OpenRouter langsung pakai daftar model terbaru.
        if self.api_keys.get("openrouter"):
            try:
                threading.Thread(
                    target=get_free_models,
                    kwargs={"refresh": True},
                    daemon=True,
                    name="openrouter-warmup",
                ).start()
            except Exception:
                pass

    def generate(self, prompt: str, max_retries: int = 3, use_cache: bool = True, system_override: Optional[str] = None, max_tokens: int = 4096, max_total_wait: Optional[float] = None) -> str:
        """
        Generate response dengan fallback otomatis.

        Args:
            prompt: Prompt yang akan dikirim ke AI
            max_retries: Max retry per provider
            use_cache: Apakah menggunakan cache untuk pertanyaan identik
            system_override: System prompt khusus untuk menggantikan default
            max_tokens: Batas token output (4096 agar teks tidak terpotong)
            max_total_wait: Batas waktu total (detik) sebelum menyerah. Default
                dari AI_MAX_TOTAL_WAIT_SECONDS — menjamin user tidak menunggu
                menit-menit saat semua provider down/rate-limit.

        Returns:
            String response dari AI
        """
        self.stats["total_requests"] += 1

        system = system_override or self.DEFAULT_SYSTEM

        # Cek cache dulu (include system in cache key)
        cache_key = prompt
        if system_override:
            cache_key = f"{safe_hash(system_override)}:{prompt}"

        if use_cache:
            cached = get_cached_ai_response(cache_key)
            if cached:
                logger.info("Using cached AI response")
                return cached

        # Budget waktu total: berhenti mencoba provider lain setelah deadline
        # tercapai agar latensi maksimal respons tetap wajar (user tidak menunggu
        # terlalu lama saat semua provider sedang gangguan).
        effective_budget = max_total_wait if max_total_wait and max_total_wait > 0 else AI_MAX_TOTAL_WAIT_SECONDS
        deadline = time.time() + effective_budget

        # Coba provider satu per satu. system & max_tokens dikirim per-request
        # sehingga request paralel dari thread berbeda tidak saling menimpa.
        # Urutan provider bersifat dinamis: provider yang baru kena rate-limit
        # (429) dipindah ke belakang antrean agar request berikutnya langsung
        # mencoba provider yang sehat.
        for provider in self._ordered_providers():
            for attempt in range(max_retries):
                # Cek deadline sebelum tiap attempt
                if time.time() >= deadline:
                    logger.warning(
                        f"AI total wait budget ({effective_budget:.0f}s) exceeded — stopping fallback"
                    )
                    break

                try:
                    logger.info(f"Trying provider: {provider} (attempt {attempt + 1}/{max_retries})")

                    config = PROVIDER_CONFIGS.get(provider)
                    if not config:
                        logger.warning(f"Provider {provider} not found in config")
                        continue

                    key = self.api_keys.get(provider)
                    if not key:
                        logger.warning(f"No API key for {provider}")
                        break  # Skip to next provider

                    response = self._call_provider(provider, prompt, system, max_tokens)
                    if response:
                        # Provider sehat — bersihkan state rate-limit lama (jika ada)
                        self._rate_limit_hints.pop(provider, None)
                        self._provider_cooldown.pop(provider, None)

                        self.stats["successful"] += 1
                        self.stats["provider_usage"][provider] += 1

                        # Only wrap with via tag if NOT using system_override (internal agent call)
                        if system_override:
                            formatted = response
                        else:
                            formatted = f"[via {config['name']}] 🤖\n\n{response}"

                        # Cache response
                        if use_cache:
                            set_cached_ai_response(cache_key, formatted)

                        return formatted

                    # 429 = kuota AKUN habis (bukan masalah model/prompt). Retry
                    # dalam hitungan detik hanya memperparah rate limit & membakar
                    # request — langsung pindah ke provider berikutnya.
                    # Catatan: check cooldown aktif dari 429 MANA PUN (termasuk
                    # thread paralel lain) — kalau provider baru saja 429,
                    # me-retry sekarang tetap kemungkinan besar gagal.
                    if self._provider_cooldown.get(provider, 0.0) > time.time():
                        logger.info(
                            f"{provider} rate-limited (429) — skip retry, pindah provider"
                        )
                        break

                    # Provider merespon kosong (bukan 429). Backoff dihitung
                    # SATU KALI di sini (hint Retry-After server bila ada, atau
                    # exponential + jitter) dan dibatasi sisa budget waktu total.
                    if attempt < max_retries - 1:
                        wait = self._backoff_wait(provider, attempt, deadline)
                        if wait <= 0:
                            logger.info(f"{provider} — sisa budget habis, pindah provider")
                            break
                        logger.info(f"{provider} returned empty response, retrying in {wait:.0f}s...")
                        time.sleep(wait)

                except Exception as e:
                    logger.warning(f"{provider} attempt {attempt + 1} failed: {e}")
                    self.stats["last_error"] = f"{provider}: {e}"
                    if attempt < max_retries - 1:
                        wait = self._backoff_wait(provider, attempt, deadline)
                        if wait <= 0:
                            break
                        logger.info(f"Retrying in {wait:.0f}s...")
                        time.sleep(wait)

            # Jika provider ini gagal total, log dan lanjut ke berikutnya
            logger.info(f"{provider} exhausted, trying next provider...")
            if time.time() >= deadline:
                break

        self.stats["failed"] += 1
        error_msg = (
            "Maaf, semua AI provider sedang tidak tersedia saat ini. "
            "Silakan coba lagi nanti.\n\n"
            "Tips:\n"
            "• Coba beberapa menit lagi (rate limit mungkin sudah reset)\n"
            "• Gunakan perintah /status untuk melihat status sistem\n"
            "• Pastikan API keys sudah diisi di file .env"
        )
        return error_msg

    async def generate_async(self, prompt: str, max_retries: int = 3, use_cache: bool = True, system_override: Optional[str] = None, max_tokens: int = 4096, max_total_wait: Optional[float] = None) -> str:
        """Async version of generate."""
        return await asyncio.to_thread(
            self.generate, prompt, max_retries, use_cache, system_override, max_tokens, max_total_wait
        )

    def _call_provider(self, provider: str, prompt: str, system: str, max_tokens: int) -> Optional[str]:
        """
        Panggil provider AI dengan format yang sesuai.

        Args:
            provider: Nama provider (groq, gemini, openrouter, cerebras)
            prompt: Prompt text
            system: System prompt untuk request ini (per-request, bukan state)
            max_tokens: Batas token output untuk request ini

        Returns:
            Response text atau None jika gagal
        """
        config = PROVIDER_CONFIGS[provider]
        key = self.api_keys[provider]

        if config["payload_template"] == "gemini":
            return self._call_gemini(provider, config, key, prompt, system, max_tokens)
        else:
            return self._call_openai_compatible(provider, config, key, prompt, system, max_tokens)

    def _ordered_providers(self) -> List[str]:
        """
        Urutan fallback dinamis: provider yang sedang cooldown (baru kena 429)
        dipindah ke belakang antrean, provider sehat dicoba lebih dulu.
        """
        now = time.time()
        # Bersihkan cooldown (dan hint-nya) yang sudah lewat agar dict tetap ramping
        stale = [p for p, ts in self._provider_cooldown.items() if ts <= now]
        for p in stale:
            self._provider_cooldown.pop(p, None)
            self._rate_limit_hints.pop(p, None)
        return sorted(
            self.fallback_order,
            key=lambda p: (self._provider_cooldown.get(p, 0.0) > now, self._order_index.get(p, 0)),
        )

    def _backoff_wait(self, provider: str, attempt: int, deadline: float) -> float:
        """
        Hitung durasi tunggu sebelum retry (detik).

        Prioritas:
        1. Hint Retry-After dari server (429) — dipakai bersama (TIDAK dikonsumsi)
           agar thread lain ikut menghormati Retry-After; dibersihkan saat provider
           berhasil atau cooldown-nya lewat.
        2. Exponential backoff (2^attempt) + jitter acak agar request paralel
           dari banyak user tidak retry serentak (thundering herd).
        Hasil dibatasi sisa budget waktu total; 0 berarti tidak ada sisa waktu.
        """
        remaining = deadline - time.time()
        if remaining <= 0:
            return 0.0
        hint = self._rate_limit_hints.get(provider)
        if hint:
            # Tidak cukup sisa budget untuk menghormati Retry-After server →
            # langsung pindah ke provider berikutnya (bukan menyia-nyiakan retry).
            if hint >= remaining:
                return 0.0
            wait = hint
        else:
            wait = min(
                self.BACKOFF_BASE_SECONDS * (2 ** attempt) * random.uniform(0.5, 1.0),
                remaining,
            )
        return max(0.0, wait)

    def _throttle(self, provider: str):
        """
        Jeda minimum antar request HTTP ke provider yang sama (anti thundering herd).

        Pipeline multi-agent menjalankan banyak generate() PARALEL dari banyak
        thread; tanpa jeda ini, request serentak ke free tier (RPM ketat)
        langsung kena 429 dan cepat menghabiskan kuota harian.

        Spacing dihitung dari waktu MULAI request sebelumnya. Pengecekan + jeda
        dikunci dengan threading.Lock agar antar thread tidak menembus jeda
        bersamaan. (Request sendiri TIDAK di-pegang lock — cukup spacing awal.)
        """
        if self.throttle_min_interval_override is not None:
            min_interval = self.throttle_min_interval_override
        else:
            min_interval = PROVIDER_CONFIGS.get(provider, {}).get("min_interval_seconds", 1.0)
        if not min_interval or min_interval <= 0:
            return

        with self._throttle_locks.setdefault(provider, threading.Lock()):
            try:
                elapsed = time.time() - self._last_request_at.get(provider, 0.0)
                wait = min_interval - elapsed
                if wait > 0:
                    # Batas 8 dtk agar throttle tidak menelan seluruh budget
                    # waktu (AI_MAX_TOTAL_WAIT_SECONDS) saat banyak thread antre.
                    time.sleep(min(wait, 8.0))
            finally:
                self._last_request_at[provider] = time.time()

    def _call_openai_compatible(self, provider: str, config: Dict, key: str, prompt: str, system: str, max_tokens: int) -> Optional[str]:
        """
        Panggil API dengan format OpenAI-compatible.
        Digunakan oleh: Groq, OpenRouter, Cerebras, Mistral

        Mencoba model utama, lalu fallback_models satu per satu jika gagal
        (404 model tidak ada, 429 rate limit, error, dll).
        """
        models = [config["model"]] + list(config.get("fallback_models", []))

        # OpenRouter: perbanyak kandidat dengan daftar free model terkini dari API
        if config.get("auto_discover_free"):
            for m in get_free_models():
                if m not in models:
                    models.append(m)
            models = models[:20]  # batasi agar fallback tidak lambat saat semua gagal

        # Skip model yang masih dalam blacklist 404 (hemat waktu & spam log).
        now = time.time()
        stale = [m for m, ts in self._dead_models.items() if now - ts > self._DEAD_MODEL_TTL]
        for m in stale:
            self._dead_models.pop(m, None)
        models = [m for m in models if now - self._dead_models.get(m, 0.0) > self._DEAD_MODEL_TTL]
        if not models:
            return None

        payload = {
            "model": models[0],
            "messages": [
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            # Temperature rendah (AI_TEMPERATURE, default 0.1) agar jawaban
            # lebih deterministik & konsisten untuk pertanyaan yang sama —
            # model tidak "kreatif" mengarang variasi angka.
            "temperature": AI_TEMPERATURE,
            "max_tokens": max_tokens,
        }

        for model in models:
            payload["model"] = model
            # Jeda minimum antar request ke provider yang sama (paralel-safe)
            self._throttle(provider)
            try:
                resp = requests.post(
                    config["url"],
                    json=payload,
                    headers=config["headers"](key),
                    timeout=AI_REQUEST_TIMEOUT,
                )

                if resp.status_code == 429:
                    logger.warning(f"{config['name']} rate limited (429) with {model}")
                    # 429 adalah kuota per-akun (TPM/RPM) — ganti model TIDAK membantu.
                    # Catat hint Retry-After + cooldown provider; backoff (termasuk
                    # tunggu) dijalankan SATU KALI di generate() agar tidak double-wait.
                    wait = self._retry_after_wait(resp)
                    self._rate_limit_hints[provider] = wait
                    self._provider_cooldown[provider] = time.time() + wait
                    logger.info(f"{config['name']} rate limited — retry hint {wait:.0f}s recorded")
                    return None

                if resp.status_code != 200:
                    if resp.status_code == 404:
                        # Model hilang / tidak gratis / diblokir guardrail privacy
                        # ("No endpoints available...") — blacklist sementara agar
                        # request berikutnya langsung skip model ini.
                        self._dead_models[model] = time.time()
                    logger.warning(f"{config['name']} error {resp.status_code} with {model}: {resp.text[:200]}")
                    continue

                data = resp.json()

                # Handle different response structures
                if "choices" in data and len(data["choices"]) > 0:
                    message = data["choices"][0].get("message", {})
                    content = message.get("content", "")
                    if content:
                        return content

                if "error" in data:
                    logger.warning(f"{config['name']} API error with {model}: {data['error']}")
                    continue

            except requests.exceptions.Timeout:
                logger.warning(f"{config['name']} timeout with {model}")
                # Timeout biasanya berlaku untuk semua model — langsung lanjut ke provider berikutnya
                return None
            except requests.exceptions.ConnectionError:
                logger.warning(f"{config['name']} connection error with {model}")
                return None
            except Exception as e:
                logger.warning(f"{config['name']} unexpected error with {model}: {e}")
                continue

        return None

    def _call_gemini(self, provider: str, config: Dict, key: str, prompt: str, system: str, max_tokens: int) -> Optional[str]:
        """
        Panggil Google Gemini API.
        Format berbeda dari OpenAI-compatible.

        Mencoba model utama, lalu fallback_models jika gagal (404 model hilang, dll).
        """
        models = [config["model"]] + list(config.get("fallback_models", []))

        for model in models:
            url = f"{config['url']}{model}:generateContent?key={key}"

            payload = {
                "system_instruction": {
                    "parts": [
                        {
                            "text": system,
                        }
                    ]
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": prompt
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    # Temperature rendah (AI_TEMPERATURE) — deterministik & faktual.
                    "temperature": AI_TEMPERATURE,
                    "maxOutputTokens": max_tokens,
                    "topP": 0.95,
                    "topK": 40,
                },
                "safetySettings": [
                    {
                        "category": "HARM_CATEGORY_HARASSMENT",
                        "threshold": "BLOCK_NONE"
                    },
                    {
                        "category": "HARM_CATEGORY_HATE_SPEECH",
                        "threshold": "BLOCK_NONE"
                    },
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "threshold": "BLOCK_NONE"
                    },
                    {
                        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                        "threshold": "BLOCK_NONE"
                    }
                ]
            }

            # Jeda minimum antar request ke provider yang sama (paralel-safe)
            self._throttle(provider)
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers=config["headers"](key),
                    timeout=AI_REQUEST_TIMEOUT,
                )

                if resp.status_code == 429:
                    logger.warning(f"Gemini rate limited (429) with {model}")
                    # Sama seperti provider OpenAI-compatible: 429 = kuota akun,
                    # catat hint + cooldown; backoff di generate() (hindari double-wait).
                    wait = self._retry_after_wait(resp)
                    self._rate_limit_hints[provider] = wait
                    self._provider_cooldown[provider] = time.time() + wait
                    logger.info(f"Gemini rate limited — retry hint {wait:.0f}s recorded")
                    return None

                if resp.status_code != 200:
                    logger.warning(f"Gemini error {resp.status_code} with {model}: {resp.text[:200]}")
                    continue

                data = resp.json()

                if "candidates" in data and len(data["candidates"]) > 0:
                    candidate = data["candidates"][0]
                    if "content" in candidate:
                        parts = candidate["content"].get("parts", [])
                        if parts:
                            text = parts[0].get("text", "")
                            if text:
                                return text

                if "promptFeedback" in data and "blockReason" in data["promptFeedback"]:
                    logger.warning(f"Gemini blocked with {model}: {data['promptFeedback']['blockReason']}")
                    continue

            except requests.exceptions.Timeout:
                logger.warning(f"Gemini timeout with {model}")
                # Timeout biasanya berlaku untuk semua model — langsung lanjut ke provider berikutnya
                return None
            except requests.exceptions.ConnectionError:
                logger.warning(f"Gemini connection error with {model}")
                return None
            except Exception as e:
                logger.warning(f"Gemini unexpected error with {model}: {e}")
                continue

        return None

    @staticmethod
    def _retry_after_wait(resp) -> float:
        """
        Ambil durasi tunggu dari header Retry-After (detik), dengan batas wajar.

        Args:
            resp: Response requests/httpx yang punya atribut headers

        Returns:
            Jumlah detik tunggu (1-10 detik)
        """
        retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        try:
            wait = float(retry_after)
        except (TypeError, ValueError):
            # Tanpa header Retry-After: cooldown 5 dtk (sebelumnya 2 dtk) —
            # free tier butuh jeda lebih panjang agar kuota sempat pulih.
            wait = 5.0
        return min(max(wait, 1.0), 10.0)

    def get_stats(self) -> Dict:
        """Dapatkan statistik penggunaan AI engine."""
        return {
            **self.stats,
            "available_providers": [
                p for p in self.fallback_order
                if self.api_keys.get(p)
            ],
            "provider_names": {
                p: PROVIDER_CONFIGS[p]["name"]
                for p in self.fallback_order
                if p in PROVIDER_CONFIGS
            },
            "degraded_providers": [
                p for p in self.fallback_order
                if self._provider_cooldown.get(p, 0.0) > time.time()
            ],
        }

    def test_connection(self, provider: str) -> Dict:
        """Test koneksi ke provider tertentu."""
        if provider not in PROVIDER_CONFIGS:
            return {"status": "error", "message": f"Provider {provider} tidak dikenal"}

        key = self.api_keys.get(provider)
        if not key:
            return {"status": "error", "message": f"API key untuk {provider} tidak ditemukan"}

        try:
            response = self._call_provider(provider, "Halo, balas dengan 'OK' saja.", self.DEFAULT_SYSTEM, 1024)
            if response:
                return {"status": "ok", "message": f"{PROVIDER_CONFIGS[provider]['name']} berfungsi normal"}
            else:
                return {"status": "error", "message": f"{PROVIDER_CONFIGS[provider]['name']} merespon kosong"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
