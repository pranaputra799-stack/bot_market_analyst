"""
Database operations via Supabase REST API.
Menggunakan requests langsung ke REST API untuk menghindari konflik dependency httpx.
"""
import logging
import requests
from config.settings import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)


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
            resp = requests.post(url, json=data, headers=headers, timeout=10)
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
            resp = requests.get(url, headers=_get_headers(), timeout=10)
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
            resp = requests.get(url, headers=_get_headers(), timeout=10)
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
            resp = requests.post(url, json={"chat_id": chat_id}, headers=headers, timeout=10)
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
            resp = requests.delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error removing subscriber: {e}")
            return False


db = Database()
