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
        "user_daily_usage",
        "watchlists",
        "user_profiles",
        "cot_cache",
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
    def _prune_stale_rows(url: str, id_col: str, ids: list, chunk: int, encode: bool = False) -> None:
        """Hapus baris yang TIDAK ada di `ids` (replace-all semantics) — aman
        pada ukuran daftar berapa pun.

        Strategi: ambil daftar existing → hitung stale = existing − ids →
        DELETE `in.(...)` per chunk. DELETE `not.in.(...)` per-chunk
        (implementasi lama) SALAH: setiap chunk menghapus id milik chunk lain,
        sehingga hanya chunk TERAKHIR yang bertahan (subscriber hilang saat
        daftar > chunk size). Bila GET existing gagal, fallback ke SATU DELETE
        `not.in.(semua ids)` — URL panjang bila daftar besar, tapi semantiknya
        benar.

        Args:
            url: URL tabel Supabase (rest/v1/<table>).
            id_col: Nama kolom id (chat_id / key).
            ids: Daftar id aktif (harus bertipe sama dengan nilai di kolom).
            chunk: Ukuran chunk DELETE (agar URL tidak kepanjangan).
            encode: True bila nilai perlu URL-encode (kunci event berisi '|',
                ':', '+'); False untuk chat_id numerik.
        """
        def _fmt(k) -> str:
            return quote(str(k), safe="") if encode else str(k)

        try:
            resp = _session().get(f"{url}?select={id_col}", headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            existing = {row[id_col] for row in resp.json()}
        except Exception:
            existing = None

        if existing is not None:
            stale = sorted(existing - set(ids))
            for i in range(0, len(stale), chunk):
                cond = ",".join(_fmt(k) for k in stale[i:i + chunk])
                _session().delete(
                    f"{url}?{id_col}=in.({cond})",
                    headers=_get_headers(), timeout=10,
                ).raise_for_status()
        else:
            cond = ",".join(_fmt(k) for k in ids)
            _session().delete(
                f"{url}?{id_col}=not.in.({cond})",
                headers=_get_headers(), timeout=10,
            ).raise_for_status()

    @staticmethod
    def save_event_alert_subscribers(subscribers) -> bool:
        """Ganti seluruh daftar subscriber event (strategi upsert + prune).

        1. UPSERT semua chat_id aktif via POST 'resolution=merge-duplicates'
           (butuh PK/UNIQUE constraint pada tabel — sudah ada di migration).
        2. PRUNE: hapus chat_id lama yang TIDAK ada di daftar baru (via
           _prune_stale_rows) — selalu pakai WHERE clause, jadi lolos proteksi
           Supabase "DELETE requires a WHERE clause" (400 code 21000), dan
           TIDAK menghapus id baru (regresi lama: not.in per-chunk hanya
           menyisakan chunk terakhir saat daftar > 200).

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
                Database._prune_stale_rows(url, "chat_id", ids, chunk=200)
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
        'resolution=merge-duplicates', lalu hapus kunci lama yang tidak ada di
        daftar baru (via _prune_stale_rows). DELETE selalu punya WHERE clause
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
                Database._prune_stale_rows(url, "key", keyset, chunk=50, encode=True)
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

    # ===================== DAILY USAGE (kuota persist) =====================
    # Kuota harian per-user disimpan di tabel user_daily_usage agar TIDAK reset
    # saat bot restart / spin-down free tier. Bot memuat ke memori saat boot dan
    # flush batch tiap ~10 menit (numpang job cache cleanup) — tanpa request DB
    # per pesan, konsisten dengan pola update_user_activity.

    @staticmethod
    def update_daily_usage(rows: list) -> bool:
        """
        Upsert batch kuota harian: [(user_id, usage_date, count)].

        Satu request HTTP untuk semua user (Primary Key user_id+usage_date →
        upsert lewat resolution=merge-duplicates). Gagal DB aman: pemanggil
        mengembalikan hitungan ke memori untuk flush berikutnya.
        """
        if not _is_configured():
            return False
        payload = []
        for uid, date_str, count in rows:
            if not (isinstance(uid, int) and not isinstance(uid, bool)) or not date_str:
                continue
            payload.append({
                "user_id": uid,
                "usage_date": date_str,
                "count": int(count),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        if not payload:
            return True
        try:
            url = f"{SUPABASE_URL}/rest/v1/user_daily_usage"
            headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error updating daily usage: {_err_detail(e)}")
            return False

    @staticmethod
    def get_daily_usage(usage_date: str = None) -> dict:
        """
        Ambil kuota harian dari DB → {user_id: count}.

        usage_date: 'YYYY-MM-DD' (UTC). None → semua tanggal (bot menyaring
        sendiri tanggal hari ini saat boot — satu request cukup).
        """
        if not _is_configured():
            return {}
        try:
            url = f"{SUPABASE_URL}/rest/v1/user_daily_usage?select=user_id,usage_date,count"
            if usage_date:
                url += f"&usage_date=eq.{quote(usage_date)}"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            rows = resp.json()
            return {
                int(r["user_id"]): int(r.get("count") or 0)
                for r in rows
                if r.get("usage_date") == (usage_date or r.get("usage_date"))
            }
        except Exception as e:
            logger.error(f"Error fetching daily usage: {_err_detail(e)}")
            return {}

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

    # ===================== WATCHLIST (personal) =====================
    # Daftar pair/instrumen favorit per user (fitur /watchlist). Tabel
    # `watchlists` (lihat migrations/supabase.sql): PRIMARY KEY (user_id, symbol).
    # Tanpa Supabase → semua method return False/[] (konsisten fitur lain).

    @staticmethod
    def add_watchlist_symbol(user_id: int, symbol: str) -> bool:
        """Simpan satu simbol ke watchlist user (idempotent — upsert)."""
        if not _is_configured():
            return False
        symbol = (symbol or "").strip()
        if not symbol or not (isinstance(user_id, int) and not isinstance(user_id, bool)):
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/watchlists"
            data = {"user_id": user_id, "symbol": symbol}
            headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=data, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menambah watchlist: {_err_detail(e)}")
            return False

    @staticmethod
    def remove_watchlist_symbol(user_id: int, symbol: str) -> bool:
        """Hapus satu simbol dari watchlist user."""
        if not _is_configured():
            return False
        symbol = (symbol or "").strip()
        if not symbol:
            return False
        try:
            url = (
                f"{SUPABASE_URL}/rest/v1/watchlists?user_id=eq.{int(user_id)}"
                f"&symbol=eq.{quote(symbol)}"
            )
            resp = _session().delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menghapus watchlist: {_err_detail(e)}")
            return False

    @staticmethod
    def get_watchlist(user_id: int) -> list:
        """Semua simbol di watchlist user, urut abjad."""
        if not _is_configured():
            return []
        try:
            url = f"{SUPABASE_URL}/rest/v1/watchlists?select=symbol&user_id=eq.{int(user_id)}&order=symbol.asc"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return [row["symbol"] for row in resp.json() if row.get("symbol")]
        except Exception as e:
            logger.error(f"Error mengambil watchlist: {_err_detail(e)}")
            return []

    @staticmethod
    def clear_watchlist(user_id: int) -> bool:
        """Kosongkan seluruh watchlist user (DELETE selalu pakai WHERE clause)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/watchlists?user_id=eq.{int(user_id)}"
            resp = _session().delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error mengosongkan watchlist: {_err_detail(e)}")
            return False

    # ===================== USER PROFILE (trading plan) =====================
    # Profil risk & preferensi trading per user (fitur /plan). Tabel
    # `user_profiles` (lihat migrations/supabase.sql), PRIMARY KEY user_id.

    @staticmethod
    def upsert_user_profile(user_id: int, profile: dict) -> bool:
        """Simpan/update profil user (merge-duplicates — kolom lain tidak hilang)."""
        if not _is_configured():
            return False
        if not (isinstance(user_id, int) and not isinstance(user_id, bool)):
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/user_profiles"
            data = {"user_id": user_id, **profile}
            headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=data, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menyimpan user profile: {_err_detail(e)}")
            return False

    @staticmethod
    def get_user_profile(user_id: int) -> dict:
        """Profil user; {} bila belum ada / gagal."""
        if not _is_configured():
            return {}
        try:
            url = f"{SUPABASE_URL}/rest/v1/user_profiles?select=*&user_id=eq.{int(user_id)}&limit=1"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            rows = resp.json()
            return rows[0] if rows else {}
        except Exception as e:
            logger.error(f"Error mengambil user profile: {_err_detail(e)}")
            return {}

    @staticmethod
    def delete_user_profile(user_id: int) -> bool:
        """Hapus profil user (reset /plan)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/user_profiles?user_id=eq.{int(user_id)}"
            resp = _session().delete(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menghapus user profile: {_err_detail(e)}")
            return False

    # ===================== COT CACHE (CFTC report) =====================
    # Cache laporan COT per instrumen (fitur /cot). Tabel `cot_cache`
    # (lihat migrations/supabase.sql): PRIMARY KEY market_key. Data disimpan
    # sebagai JSONB bersama expires_at (7 hari) — CFTC hanya rilis 1x/minggu,
    # jadi tidak perlu download ulang dalam seminggu.

    @staticmethod
    def get_cot_cache(market_key: str) -> dict:
        """Data COT ter-cache untuk satu market ({} bila tidak ada/expired)."""
        if not _is_configured():
            return {}
        try:
            url = f"{SUPABASE_URL}/rest/v1/cot_cache?select=*&market_key=eq.{quote(market_key)}&limit=1"
            resp = _session().get(url, headers=_get_headers(), timeout=10)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return {}
            row = rows[0]
            expires = row.get("expires_at")
            if expires:
                exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if exp_dt <= datetime.now(timezone.utc):
                    return {}
            return row
        except Exception as e:
            logger.error(f"Error mengambil cot cache: {_err_detail(e)}")
            return {}

    @staticmethod
    def set_cot_cache(market_key: str, data: dict, ttl_seconds: int = 7 * 24 * 3600) -> bool:
        """Simpan data COT satu market ke cache (upsert, TTL default 7 hari)."""
        if not _is_configured():
            return False
        try:
            url = f"{SUPABASE_URL}/rest/v1/cot_cache"
            payload = {
                "market_key": market_key,
                "data": data,
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            headers = {**_get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
            resp = _session().post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error menyimpan cot cache: {_err_detail(e)}")
            return False

    # ===================== ASYNC WRAPPERS =====================
    # Handler Telegram (python-telegram-bot 22.x) berjalan di event loop asyncio.
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

    @staticmethod
    async def update_daily_usage_async(rows: list) -> bool:
        return await asyncio.to_thread(Database.update_daily_usage, rows)

    @staticmethod
    async def get_daily_usage_async(usage_date: str = None) -> dict:
        return await asyncio.to_thread(Database.get_daily_usage, usage_date)

    @staticmethod
    async def add_watchlist_symbol_async(user_id: int, symbol: str) -> bool:
        return await asyncio.to_thread(Database.add_watchlist_symbol, user_id, symbol)

    @staticmethod
    async def remove_watchlist_symbol_async(user_id: int, symbol: str) -> bool:
        return await asyncio.to_thread(Database.remove_watchlist_symbol, user_id, symbol)

    @staticmethod
    async def get_watchlist_async(user_id: int) -> list:
        return await asyncio.to_thread(Database.get_watchlist, user_id)

    @staticmethod
    async def clear_watchlist_async(user_id: int) -> bool:
        return await asyncio.to_thread(Database.clear_watchlist, user_id)

    @staticmethod
    async def upsert_user_profile_async(user_id: int, profile: dict) -> bool:
        return await asyncio.to_thread(Database.upsert_user_profile, user_id, profile)

    @staticmethod
    async def get_user_profile_async(user_id: int) -> dict:
        return await asyncio.to_thread(Database.get_user_profile, user_id)

    @staticmethod
    async def delete_user_profile_async(user_id: int) -> bool:
        return await asyncio.to_thread(Database.delete_user_profile, user_id)

    @staticmethod
    async def get_cot_cache_async(market_key: str) -> dict:
        return await asyncio.to_thread(Database.get_cot_cache, market_key)

    @staticmethod
    async def set_cot_cache_async(market_key: str, data: dict, ttl_seconds: int = 7 * 24 * 3600) -> bool:
        return await asyncio.to_thread(Database.set_cot_cache, market_key, data, ttl_seconds)


db = Database()
