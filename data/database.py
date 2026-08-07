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
from datetime import datetime, timedelta, timezone
from typing import Optional

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

    # ===================== WATCHLIST =====================

    @staticmethod
    def get_watchlist(chat_id: int) -> list:
        """Daftar watchlist user: [{"symbol", "label"}] terurut dari terlama."""
        if not _is_configured():
            return []
        try:
            url = (
                f"{SUPABASE_URL}/rest/v1/watchlist?chat_id=eq.{chat_id}"
                f"&select=symbol,label&order=created_at.asc"
            )
            resp = requests.get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Error fetching watchlist: {e}")
            return []

    @staticmethod
    def add_watch(chat_id: int, symbol: str, label: str = "") -> bool:
        """Tambah simbol ke watchlist user (duplikat aman — upsert)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/watchlist"
            headers = {
                **_get_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            }
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "symbol": symbol, "label": label or ""},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error adding watch: {e}")
            return False

    @staticmethod
    def remove_watch(chat_id: int, symbol: str) -> bool:
        """Hapus simbol dari watchlist user."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/watchlist?chat_id=eq.{chat_id}&symbol=eq.{symbol}"
            resp = requests.delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error removing watch: {e}")
            return False

    @staticmethod
    def get_all_watched_symbols() -> list:
        """Semua simbol yang di-watch user mana pun (dipakai job recorder harga)."""
        if not _is_configured():
            return []
        try:
            url = f"{SUPABASE_URL}/rest/v1/watchlist?select=symbol"
            resp = requests.get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return sorted({row["symbol"] for row in resp.json()})
        except Exception as e:
            logger.error(f"Error fetching watched symbols: {e}")
            return []

    # ===================== PRICE HISTORY =====================

    @staticmethod
    def save_price_snapshot(
        symbol: str,
        price: float,
        change_pct: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> bool:
        """Simpan satu snapshot harga (dipakai job recorder berkala)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/price_history"
            data = {
                "symbol": symbol,
                "price": price,
                "change_pct": change_pct,
                "bid": bid,
                "ask": ask,
            }
            resp = requests.post(url, json=data, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving price snapshot: {e}")
            return False

    @staticmethod
    def get_price_history(symbol: str, limit: int = 48) -> list:
        """Riwayat snapshot harga terbaru dulu: [{price, bid, ask, change_pct, created_at}]"""
        if not _is_configured():
            return []
        try:
            url = (
                f"{SUPABASE_URL}/rest/v1/price_history?symbol=eq.{symbol}"
                f"&select=price,bid,ask,change_pct,created_at&order=created_at.desc&limit={limit}"
            )
            resp = requests.get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Error fetching price history: {e}")
            return []

    @staticmethod
    def delete_old_price_history(days: int = 30) -> bool:
        """Bersihkan snapshot yang lebih tua dari `days` hari (job harian)."""
        if not _is_configured():
            return False
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            url = f"{SUPABASE_URL}/rest/v1/price_history?created_at=lt.{cutoff}"
            resp = requests.delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error cleaning price history: {e}")
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
            resp = requests.get(url, headers=_get_headers(), timeout=10)
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
            resp = requests.post(url, json={"key": key}, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving reported event: {e}")
            return False

    # ===================== PRICE ALERTS (persisten) =====================
    # Alert harga /pa tersimpan di DB, bukan hanya RAM: handler menulis setiap
    # perubahan (add/clear/del/terpicu), bot memuat ulang saat startup.
    # Strategi replace-all (delete + insert) — total alert dibatasi
    # PRICE_ALERT_MAX_TOTAL (kecil), sehingga sederhana & konsisten.

    @staticmethod
    def get_price_alerts() -> list:
        """Semua alert harga aktif, terurut dari id terkecil:
        [{id, chat_id, user_id, symbol, display_name, target, direction}]"""
        if not _is_configured():
            return []
        try:
            url = f"{SUPABASE_URL}/rest/v1/price_alerts?select=*&order=id.asc"
            resp = requests.get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            alerts = []
            for row in resp.json():
                try:
                    alerts.append({
                        "id": int(row["id"]),
                        "chat_id": int(row["chat_id"]),
                        "user_id": int(row["user_id"]),
                        "symbol": row["symbol"],
                        "display_name": row.get("display_name") or row["symbol"],
                        "target": float(row["target"]),
                        "direction": row.get("direction") or "above",
                    })
                except (KeyError, TypeError, ValueError):
                    logger.warning(f"Skipping malformed price alert row: {row}")
            return alerts
        except Exception as e:
            logger.error(f"Error fetching price alerts: {e}")
            return []

    @staticmethod
    def save_price_alerts(alerts: list) -> bool:
        """Ganti seluruh daftar alert harga (delete semua + insert ulang)."""
        if not _is_configured():
            return False
        try:
            headers = _get_headers()
            url = f"{SUPABASE_URL}/rest/v1/price_alerts"
            requests.delete(url, headers=headers, timeout=10).raise_for_status()
            if not alerts:
                return True
            rows = [
                {
                    "id": a.get("id"),
                    "chat_id": a.get("chat_id"),
                    "user_id": a.get("user_id"),
                    "symbol": a.get("symbol", ""),
                    "display_name": a.get("display_name", "") or "",
                    "target": a.get("target"),
                    "direction": a.get("direction", "above"),
                }
                for a in alerts
            ]
            headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = requests.post(url, json=rows, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving price alerts: {e}")
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
            resp = requests.get(url, headers=_get_headers(), timeout=10)
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
            requests.delete(url, headers=headers, timeout=10).raise_for_status()
            rows = [{"chat_id": c} for c in sorted(set(subscribers))]
            if not rows:
                return True
            headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = requests.post(url, json=rows, headers=headers, timeout=10)
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
            resp = requests.get(url, headers=_get_headers(), timeout=10)
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
            requests.delete(url, headers=headers, timeout=10).raise_for_status()
            rows = [{"key": k} for k in sorted(set(keys))]
            if not rows:
                return True
            headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = requests.post(url, json=rows, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving event_alert_notified: {e}")
            return False

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
    async def get_watchlist_async(chat_id: int) -> list:
        return await asyncio.to_thread(Database.get_watchlist, chat_id)

    @staticmethod
    async def add_watch_async(chat_id: int, symbol: str, label: str = "") -> bool:
        return await asyncio.to_thread(Database.add_watch, chat_id, symbol, label)

    @staticmethod
    async def remove_watch_async(chat_id: int, symbol: str) -> bool:
        return await asyncio.to_thread(Database.remove_watch, chat_id, symbol)

    @staticmethod
    async def get_all_watched_symbols_async() -> list:
        return await asyncio.to_thread(Database.get_all_watched_symbols)

    @staticmethod
    async def save_price_snapshot_async(
        symbol: str,
        price: float,
        change_pct: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> bool:
        return await asyncio.to_thread(
            Database.save_price_snapshot, symbol, price, change_pct, bid, ask
        )

    @staticmethod
    async def get_price_history_async(symbol: str, limit: int = 48) -> list:
        return await asyncio.to_thread(Database.get_price_history, symbol, limit)

    @staticmethod
    async def delete_old_price_history_async(days: int = 30) -> bool:
        return await asyncio.to_thread(Database.delete_old_price_history, days)

    @staticmethod
    async def get_reported_events_async() -> set:
        return await asyncio.to_thread(Database.get_reported_events)

    @staticmethod
    async def save_reported_event_async(key: str) -> bool:
        return await asyncio.to_thread(Database.save_reported_event, key)

    @staticmethod
    async def get_price_alerts_async() -> list:
        return await asyncio.to_thread(Database.get_price_alerts)

    @staticmethod
    async def save_price_alerts_async(alerts: list) -> bool:
        return await asyncio.to_thread(Database.save_price_alerts, alerts)

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


db = Database()
