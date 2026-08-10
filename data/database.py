"""
Database operations via Supabase REST API.
Menggunakan requests langsung ke REST API untuk menghindari konflik dependency httpx.

Async-safe: semua operasi yang dipanggil dari handler Telegram (asyncio)
memakai varian *_async yang menjalankan request sinkron di thread terpisah
(asyncio.to_thread) agar event loop tetap responsif saat banyak user mengirim
pesan bersamaan.
"""
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests
from config.settings import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

# Session per-thread (connection pooling): setiap thread pekerja (asyncio.to_thread)
# memakai ulang koneksi TCP/TLS ke Supabase, hemat handshake per panggilan REST.
_session_local = threading.local()


def _session() -> "requests.Session":
    """Session requests khusus thread — aman dipakai konkuren (tiap thread sendiri)."""
    s = getattr(_session_local, "session", None)
    if s is None:
        s = requests.Session()
        _session_local.session = s
    return s


def _get_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

def _is_configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


class Database:
    @staticmethod
    def is_connected():
        return _is_configured()

    @staticmethod
    def upsert_user(user_id: int, username: str, first_name: str):
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/users"
            data = {
                "user_id": user_id,
                "username": username or "",
                "first_name": first_name or "",
            }
            headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=data, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error upserting user: {e}")
            return False

    @staticmethod
    def get_all_subscribers() -> list:
        if not _is_configured():
            return []
        try:
            url = f"{SUPABASE_URL}/rest/v1/subscribers?select=chat_id"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return [row["chat_id"] for row in resp.json()]
        except Exception as e:
            logger.error(f"Error fetching subscribers: {e}")
            return []

    @staticmethod
    def is_subscribed(chat_id: int) -> bool:
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/subscribers?chat_id=eq.{chat_id}&select=chat_id"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return len(resp.json()) > 0
        except Exception as e:
            logger.error(f"Error checking subscriber: {e}")
            return False

    @staticmethod
    def add_subscriber(chat_id: int) -> bool:
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/subscribers"
            headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json={"chat_id": chat_id}, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error adding subscriber: {e}")
            return False

    @staticmethod
    def remove_subscriber(chat_id: int) -> bool:
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/subscribers?chat_id=eq.{chat_id}"
            resp = _session().delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error removing subscriber: {e}")
            return False

    # ===================== EVENT REPORTS (aftermath dedup) =====================

    @staticmethod
    def get_reported_events() -> set:
        """Kunci event yang sudah pernah dilaporkan (7 hari terakhir)."""
        if not _is_configured():
            return set()
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            url = f"{SUPABASE_URL}/rest/v1/event_reports?select=key&created_at=gte.{cutoff}"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return {row["key"] for row in resp.json()}
        except Exception as e:
            logger.error(f"Error fetching reported events: {e}")
            return set()

    @staticmethod
    def save_reported_event(key: str) -> bool:
        """Tandai satu event sudah dilaporkan (idempotent — upsert)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/event_reports"
            headers = {
                **_get_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            }
            resp = _session().post(url, json={"key": key}, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving reported event: {e}")
            return False

    # ===================== EVENT ALERT SUBSCRIBERS (persisten) =====================
    # Chat yang subscribe notifikasi event (/alert on). Sebelumnya RAM-only,
    # sekarang persisten agar tidak hilang saat restart/deploy.

    @staticmethod
    def get_event_alert_subscribers() -> set:
        """Semua chat_id yang subscribe notifikasi event."""
        if not _is_configured():
            return set()
        try:
            url = f"{SUPABASE_URL}/rest/v1/event_alert_subscribers?select=chat_id"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return {int(row["chat_id"]) for row in resp.json()}
        except Exception as e:
            logger.error(f"Error fetching event alert subscribers: {e}")
            return set()

    @staticmethod
    def save_event_alert_subscribers(subscribers) -> bool:
        """Ganti seluruh daftar subscriber event (delete semua + insert ulang)."""
        if not _is_configured():
            return False
        try:
            headers = _get_headers()
            url = f"{SUPABASE_URL}/rest/v1/event_alert_subscribers"
            _session().delete(url, headers=headers, timeout=10).raise_for_status()
            rows = [{"chat_id": c} for c in sorted(set(subscribers))]
            if not rows:
                return True
            headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=rows, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving event alert subscribers: {e}")
            return False

    # ===================== EVENT ALERT NOTIFIED (dedup reminder) =====================
    # Kunci event yang sudah diberi reminder (jendela lead). Sebelumnya RAM-only;
    # sekarang persisten agar reminder tidak terkirim dobel setelah restart.

    @staticmethod
    def get_event_alert_notified() -> set:
        """Semua kunci event yang sudah mendapat reminder."""
        if not _is_configured():
            return set()
        try:
            url = f"{SUPABASE_URL}/rest/v1/event_alert_notified?select=key"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return {row["key"] for row in resp.json()}
        except Exception as e:
            logger.error(f"Error fetching event_alert_notified: {e}")
            return set()

    @staticmethod
    def save_event_alert_notified(keys) -> bool:
        """Ganti seluruh kunci event yang sudah di-notify (delete + insert)."""
        if not _is_configured():
            return False
        try:
            headers = _get_headers()
            url = f"{SUPABASE_URL}/rest/v1/event_alert_notified"
            _session().delete(url, headers=headers, timeout=10).raise_for_status()
            rows = [{"key": k} for k in sorted(set(keys))]
            if not rows:
                return True
            headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=rows, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving event_alert_notified: {e}")
            return False

    # ===================== NEWS PREDICTIONS (XAU/USD) =====================
    # Prediksi arah emas terhadap event ekonomi high-impact + hasil benar/salah.
    # Tabel news_predictions (event_key UNIQUE). Disimpan upsert per baris saat
    # prediksi dibuat/dievaluasi; dibaca seluruhnya saat bot start ke memori
    # (data/news_predictions.py) agar win rate bertahan setelah restart.

    @staticmethod
    def save_news_prediction(record: dict) -> bool:
        """Upsert satu record prediksi news (idempotent per event_key)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/news_predictions"
            headers = {
                **_get_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            }
            resp = _session().post(url, json=record, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving news prediction: {e}")
            return False

    @staticmethod
    def get_news_predictions(limit: int = 1000) -> list:
        """Semua prediksi news, terurut terbaru (untuk dimuat ke memori)."""
        if not _is_configured():
            return []
        try:
            url = (
                f"{SUPABASE_URL}/rest/v1/news_predictions?select=*"
                f"&order=predicted_at.desc&limit={int(limit)}"
            )
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Error fetching news predictions: {e}")
            return []

    # ===================== ASYNC WRAPPERS =====================
    # Handler Telegram (python-telegram-bot v20) berjalan di event loop asyncio.
    # Varian *_async memindahkan operasi sinkron ke thread pool sehingga tidak
    # memblokir event loop saat banyak user berinteraksi bersamaan.

    @staticmethod
    async def upsert_user_async(user_id: int, username: str, first_name: str) -> bool:
        return await asyncio.to_thread(Database.upsert_user, user_id, username, first_name)

    @staticmethod
    async def get_all_subscribers_async() -> list:
        return await asyncio.to_thread(Database.get_all_subscribers)

    @staticmethod
    async def is_subscribed_async(chat_id: int) -> bool:
        return await asyncio.to_thread(Database.is_subscribed, chat_id)

    @staticmethod
    async def add_subscriber_async(chat_id: int) -> bool:
        return await asyncio.to_thread(Database.add_subscriber, chat_id)

    @staticmethod
    async def remove_subscriber_async(chat_id: int) -> bool:
        return await asyncio.to_thread(Database.remove_subscriber, chat_id)

    @staticmethod
    async def get_reported_events_async() -> set:
        return await asyncio.to_thread(Database.get_reported_events)

    @staticmethod
    async def save_reported_event_async(key: str) -> bool:
        return await asyncio.to_thread(Database.save_reported_event, key)

    @staticmethod
    async def get_event_alert_subscribers_async() -> set:
        return await asyncio.to_thread(Database.get_event_alert_subscribers)

    @staticmethod
    async def save_event_alert_subscribers_async(subscribers) -> bool:
        return await asyncio.to_thread(Database.save_event_alert_subscribers, subscribers)

    @staticmethod
    async def get_event_alert_notified_async() -> set:
        return await asyncio.to_thread(Database.get_event_alert_notified)

    @staticmethod
    async def save_event_alert_notified_async(keys) -> bool:
        return await asyncio.to_thread(Database.save_event_alert_notified, keys)

    @staticmethod
    async def save_news_prediction_async(record: dict) -> bool:
        return await asyncio.to_thread(Database.save_news_prediction, record)

    @staticmethod
    async def get_news_predictions_async(limit: int = 1000) -> list:
        return await asyncio.to_thread(Database.get_news_predictions, limit)


db = Database()
