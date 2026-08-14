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
from urllib.parse import quote

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

def _err_detail(e: Exception) -> str:
    """Format exception untuk log — sertakan body response PostgREST bila ada.

    requests.HTTPError hanya menampilkan status line (mis. '400 Client Error: Bad
    Request'); detail penyebab asli (kode PGRSTxxx / pesan Postgres) ada di body
    response dan tersembunyi tanpa helper ini.
    """
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            body = getattr(resp, "text", "") or ""
        except Exception:
            body = ""
        if body:
            return f"{e} | {body[:500]}"
    return str(e)


class Database:
    # Tabel yang WAJIB ada agar fitur persisten jalan (migrations/supabase.sql).
    # Dipakai check_required_tables() saat boot — bila ada yang hilang, admin
    # dinotifikasi (fitur diam-diam mati adalah penyebab support issue #1).
    REQUIRED_TABLES = [
        "app_cache",
        "users",
        "subscribers",
        "event_reports",
        "event_alert_subscribers",
        "event_alert_notified",
        "news_predictions",
        "journal",
    ]

    @staticmethod
    def is_connected():
        return _is_configured()

    @staticmethod
    def check_required_tables() -> list:
        """Cek tabel penting ada/tidak via REST (best-effort).

        Returns:
            List nama tabel yang HILANG (kosong bila lengkap / Supabase tidak
            dikonfigurasi). Error jaringan BUKAN dianggap tabel hilang — hanya
            HTTP 404 (PostgREST PGRST205: table does not exist) yang dilaporkan.
        """
        if not _is_configured():
            return []
        missing = []
        for table in Database.REQUIRED_TABLES:
            try:
                url = f"{SUPABASE_URL}/rest/v1/{table}?select=*&limit=1"
                resp = _session().get(url, headers=_get_headers(), timeout=10)
                if resp.status_code == 404:
                    missing.append(table)
                else:
                    resp.raise_for_status()
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 404:
                    missing.append(table)
                else:
                    # Jaringan / auth error → jangan salah lapor sebagai tabel hilang
                    logger.debug(f"check_required_tables {table}: {_err_detail(e)}")
        return missing

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
            logger.error(f"Error upserting user: {_err_detail(e)}")
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
            logger.error(f"Error fetching subscribers: {_err_detail(e)}")
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
            logger.error(f"Error checking subscriber: {_err_detail(e)}")
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
            logger.error(f"Error adding subscriber: {_err_detail(e)}")
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
            logger.error(f"Error removing subscriber: {_err_detail(e)}")
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
            logger.error(f"Error fetching reported events: {_err_detail(e)}")
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
            logger.error(f"Error saving reported event: {_err_detail(e)}")
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
            logger.error(f"Error fetching event alert subscribers: {_err_detail(e)}")
            return set()

    @staticmethod
    def save_event_alert_subscribers(subscribers) -> bool:
        """Ganti seluruh daftar subscriber event (strategi upsert + prune).

        1. UPSERT semua chat_id aktif via POST 'resolution=merge-duplicates'
           (butuh PK/UNIQUE constraint pada tabel — sudah ada di migration).
        2. PRUNE: DELETE chat_id yang TIDAK ada di daftar baru lewat filter
           'chat_id=not.in.(...)' — selalu punya WHERE clause, jadi lolos
           proteksi Supabase "DELETE requires a WHERE clause" yang menolak
           DELETE massal tanpa filter dengan 400 (code 21000).

        Keuntungan vs pola lama (DELETE semua lalu insert): tidak ada request
        DELETE tanpa WHERE, dan bila upsert gagal, daftar lama tidak hilang
        (crash-safe). created_at dikirim eksplisit agar insert tetap sukses
        walau kolom di DB ber-NOT NULL tanpa DEFAULT.
        """
        if not _is_configured():
            return False
        try:
            # Guard: hanya chat_id bertipe int (bukan bool) — satu nilai aneh
            # (mis. string) membuat seluruh batch insert ditolak Postgres.
            ids = sorted(
                c for c in set(subscribers)
                if isinstance(c, int) and not isinstance(c, bool)
            )
            url = f"{SUPABASE_URL}/rest/v1/event_alert_subscribers"
            if ids:
                now_iso = datetime.now(timezone.utc).isoformat()
                rows = [{"chat_id": c, "created_at": now_iso} for c in ids]
                headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
                _session().post(url, json=rows, headers=headers, timeout=10).raise_for_status()
                # Prune yang tidak lagi subscribe — chunk agar URL tidak kepanjangan.
                for i in range(0, len(ids), 200):
                    cond = ",".join(str(c) for c in ids[i:i + 200])
                    _session().delete(
                        f"{url}?chat_id=not.in.({cond})",
                        headers=_get_headers(), timeout=10,
                    ).raise_for_status()
            else:
                # Daftar kosong: kosongkan tabel — DELETE tetap pakai WHERE clause.
                _session().delete(
                    f"{url}?chat_id=not.is.null",
                    headers=_get_headers(), timeout=10,
                ).raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving event alert subscribers: {_err_detail(e)}")
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
            logger.error(f"Error fetching event_alert_notified: {_err_detail(e)}")
            return set()

    @staticmethod
    def save_event_alert_notified(keys) -> bool:
        """Ganti seluruh kunci event yang sudah di-notify (strategi upsert + prune).

        Sama seperti save_event_alert_subscribers: upsert dulu via
        'resolution=merge-duplicates', lalu prune kunci yang tidak ada di daftar
        baru lewat filter 'key=not.in.(...)'. DELETE selalu punya WHERE clause
        sehingga lolos proteksi Supabase "DELETE requires a WHERE clause" (400
        code 21000). Nilai kunci di-URL-encode (kunci mengandung karakter seperti
        '|', ':', '+'). Hanya kunci string non-kosong yang dikirim.
        """
        if not _is_configured():
            return False
        try:
            keyset = sorted({k for k in set(keys) if isinstance(k, str) and k})
            url = f"{SUPABASE_URL}/rest/v1/event_alert_notified"
            if keyset:
                now_iso = datetime.now(timezone.utc).isoformat()
                rows = [{"key": k, "created_at": now_iso} for k in keyset]
                headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
                _session().post(url, json=rows, headers=headers, timeout=10).raise_for_status()
                # Prune kunci yang tidak lagi berlaku — chunk kecil (key bisa
                # panjang, mis. 'NFP|2026-08-10T12:30:00+00:00') + URL-encode nilai.
                for i in range(0, len(keyset), 50):
                    cond = ",".join(quote(k, safe="") for k in keyset[i:i + 50])
                    _session().delete(
                        f"{url}?key=not.in.({cond})",
                        headers=_get_headers(), timeout=10,
                    ).raise_for_status()
            else:
                _session().delete(
                    f"{url}?key=not.is.null",
                    headers=_get_headers(), timeout=10,
                ).raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error saving event_alert_notified: {_err_detail(e)}")
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
            logger.error(f"Error saving news prediction: {_err_detail(e)}")
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
            logger.error(f"Error fetching news predictions: {_err_detail(e)}")
            return []

    # ===================== TRADING JOURNAL =====================
    # Catatan transaksi per user (edukasi): tabel `journal` (lihat
    # migrations/supabase.sql). Tanpa Supabase → semua method return False/[]
    # (konsisten dengan fitur lain).

    @staticmethod
    def add_journal_entry(record: dict) -> bool:
        """Simpan satu entri journal baru (status open)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/journal"
            resp = _session().post(
                url, json=record, headers=_get_headers(), timeout=10
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menambah journal entry: {_err_detail(e)}")
            return False

    @staticmethod
    def list_journal_entries(user_id: int, limit: int = 20) -> list:
        """Entri journal milik satu user, terbaru dulu."""
        if not _is_configured():
            return []
        try:
            url = (
                f"{SUPABASE_URL}/rest/v1/journal?select=*"
                f"&user_id=eq.{int(user_id)}&order=id.desc&limit={int(limit)}"
            )
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Error mengambil journal: {_err_detail(e)}")
            return []

    @staticmethod
    def close_journal_entry(entry_id: int, user_id: int, exit_price: float, result: str, pnl_pct: float) -> bool:
        """Tutup entri journal (set exit_price/result/pnl_pct)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/journal?id=eq.{int(entry_id)}&user_id=eq.{int(user_id)}"
            payload = {
                "status": "closed",
                "exit_price": float(exit_price),
                "result": result,
                "pnl_pct": float(pnl_pct),
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            resp = _session().patch(url, json=payload, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menutup journal entry: {_err_detail(e)}")
            return False

    @staticmethod
    def delete_journal_entry(entry_id: int, user_id: int) -> bool:
        """Hapus entri journal (hanya milik user)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/journal?id=eq.{int(entry_id)}&user_id=eq.{int(user_id)}"
            resp = _session().delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menghapus journal entry: {_err_detail(e)}")
            return False

    # ===================== USER ACTIVITY (batched) =====================
    # Aktivitas user (last_active_at + total_questions) di-flush dari memori
    # bot secara batch setiap ~10 menit (numpang job cache cleanup yang sudah
    # ada — tanpa request tambahan per pesan). Kolom ini ditambahkan lewat
    # migrations/supabase.sql (idempotent ALTER TABLE bila tabel lama).

    @staticmethod
    def update_user_activity(rows: list) -> bool:
        """
        Upsert batch aktivitas user: [(user_id, last_active_iso, total_questions)].

        Satu request HTTP untuk SEMUA user (hemat quota Supabase vs 1 request
        per user per pesan). Dipanggil berkala dari job cleanup bot.
        """
        if not _is_configured():
            return False
        if not rows:
            return True
        try:
            url = f"{SUPABASE_URL}/rest/v1/users"
            payload = [
                {
                    "user_id": uid,
                    "last_active_at": last_active,
                    "total_questions": int(count),
                }
                for uid, last_active, count in rows
                if isinstance(uid, int) and not isinstance(uid, bool)
            ]
            if not payload:
                return True
            headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error updating user activity: {_err_detail(e)}")
            return False

    @staticmethod
    def get_user_stats() -> dict:
        """Statistik user untuk admin /stats (best-effort, aman tanpa DB)."""
        if not _is_configured():
            return {}
        stats: dict = {}
        try:
            url = f"{SUPABASE_URL}/rest/v1/users?select=user_id,last_active_at,total_questions"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            rows = resp.json()
            stats["total_users"] = len(rows)
            # User aktif: last_active_at dalam 24 jam terakhir
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            stats["active_24h"] = sum(
                1 for r in rows if r.get("last_active_at") and r["last_active_at"] >= cutoff
            )
            stats["total_questions"] = sum(
                int(r.get("total_questions") or 0) for r in rows
            )
        except Exception as e:
            logger.error(f"Error fetching user stats: {_err_detail(e)}")
            return {}
        return stats

    @staticmethod
    def get_counts() -> dict:
        """Jumlah baris beberapa tabel penting (admin /stats)."""
        counts: dict = {}
        for table in ("subscribers", "event_alert_subscribers"):
            if not _is_configured():
                counts[table] = 0
                continue
            try:
                url = f"{SUPABASE_URL}/rest/v1/{table}?select=chat_id"
                resp = _session().get(url, headers=_get_headers(), timeout=10)
                resp.raise_for_status()
                counts[table] = len(resp.json())
            except Exception as e:
                logger.error(f"Error counting {table}: {_err_detail(e)}")
                counts[table] = 0
        return counts

    # ===================== ASYNC WRAPPERS =====================
    # Handler Telegram (python-telegram-bot v20) berjalan di event loop asyncio.
    # Varian *_async memindahkan operasi sinkron ke thread pool sehingga tidak
    # memblokir event loop saat banyak user berinteraksi bersamaan.

    @staticmethod
    async def check_required_tables_async() -> list:
        return await asyncio.to_thread(Database.check_required_tables)

    @staticmethod
    async def upsert_user_async(user_id: int, username: str, first_name: str) -> bool:
        return await asyncio.to_thread(Database.upsert_user, user_id, username, first_name)

    @staticmethod
    async def update_user_activity_async(rows: list) -> bool:
        return await asyncio.to_thread(Database.update_user_activity, rows)

    @staticmethod
    async def get_user_stats_async() -> dict:
        return await asyncio.to_thread(Database.get_user_stats)

    @staticmethod
    async def get_counts_async() -> dict:
        return await asyncio.to_thread(Database.get_counts)

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

    @staticmethod
    async def add_journal_entry_async(record: dict) -> bool:
        return await asyncio.to_thread(Database.add_journal_entry, record)

    @staticmethod
    async def list_journal_entries_async(user_id: int, limit: int = 20) -> list:
        return await asyncio.to_thread(Database.list_journal_entries, user_id, limit)

    @staticmethod
    async def close_journal_entry_async(entry_id: int, user_id: int, exit_price: float, result: str, pnl_pct: float) -> bool:
        return await asyncio.to_thread(
            Database.close_journal_entry, entry_id, user_id, exit_price, result, pnl_pct
        )

    @staticmethod
    async def delete_journal_entry_async(entry_id: int, user_id: int) -> bool:
        return await asyncio.to_thread(Database.delete_journal_entry, entry_id, user_id)


db = Database()
