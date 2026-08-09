"""
News Prediction Store - Penyimpanan prediksi arah emas (XAU/USD) terhadap
event ekonomi high-impact.

Dua lapis:
- Memori (source of truth saat runtime, thread-safe) — tetap berfungsi walau
  Supabase belum dikonfigurasi (riwayat hanya seumur proses).
- Supabase (tabel news_predictions) — sinkronisasi best-effort agar riwayat
  & win rate bertahan setelah restart/deploy.

Record berbentuk dict dengan field:
event_key (str unik), event_name, event_time, event_dt_utc (iso UTC),
country, country_emoji, direction ('naik'/'turun'), price_at_prediction,
reasoning, market_line, predicted_at (iso), status ('pending'/'settled'),
actual_direction ('naik'/'turun'/'flat'), result ('benar'/'salah'/'flat'),
price_after, move_pct, result_reasoning, settled_at (iso),
actual, forecast, prev, unit.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from data.database import db

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_SETTLED = "settled"

RESULT_BENAR = "benar"
RESULT_SALAH = "salah"
RESULT_FLAT = "flat"


class NewsPredictionStore:
    """Store prediksi news dalam memori + sinkronisasi best-effort ke Supabase."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: Dict[str, dict] = {}
        self._loaded = False

    # ===================== LOADING =====================

    def load(self) -> None:
        """Muat riwayat prediksi dari Supabase (no-op bila tidak dikonfigurasi)."""
        if self._loaded:
            return
        with self._lock:
            self._loaded = True  # cegah re-entry saat db lambat
        if not db.is_connected():
            return
        try:
            rows = db.get_news_predictions(limit=1000)
            with self._lock:
                for row in rows:
                    key = row.get("event_key")
                    if key:
                        self._records[key] = row
            logger.info("News predictions loaded from Supabase: %d", len(rows))
        except Exception as e:
            logger.warning("Gagal memuat news predictions dari Supabase: %s", e)

    def ensure_loaded(self) -> None:
        """Pastikan riwayat sudah dimuat (idempotent, aman dipanggil berkali-kali)."""
        if not self._loaded:
            self.load()

    # ===================== WRITE =====================

    def add_prediction(
        self,
        *,
        event_key: str,
        event_name: str,
        event_time: str = "",
        event_dt_utc=None,
        country: str = "",
        country_emoji: str = "",
        direction: str,
        price_at_prediction: Optional[float] = None,
        reasoning: str = "",
        market_line: str = "",
        actual=None,
        forecast=None,
        prev=None,
        unit: str = "",
    ) -> dict:
        """Simpan prediksi baru (status pending). Mengembalikan record yang disimpan."""
        now = datetime.now(timezone.utc)
        if event_dt_utc is not None and getattr(event_dt_utc, "tzinfo", None) is not None:
            dt_iso = event_dt_utc.astimezone(timezone.utc).isoformat()
        else:
            dt_iso = None
        record = {
            "event_key": event_key,
            "event_name": event_name,
            "event_time": event_time,
            "event_dt_utc": dt_iso,
            "country": country,
            "country_emoji": country_emoji,
            "direction": direction if direction in ("naik", "turun") else "naik",
            "price_at_prediction": price_at_prediction,
            "reasoning": reasoning,
            "market_line": market_line,
            "predicted_at": now.isoformat(),
            "status": STATUS_PENDING,
            "actual_direction": None,
            "result": None,
            "price_after": None,
            "move_pct": None,
            "result_reasoning": None,
            "settled_at": None,
            "actual": actual,
            "forecast": forecast,
            "prev": prev,
            "unit": unit,
        }
        with self._lock:
            self._records[event_key] = record
        return record

    def settle(
        self,
        *,
        event_key: str,
        result: str,
        actual_direction: Optional[str] = None,
        price_after: Optional[float] = None,
        move_pct: Optional[float] = None,
        reasoning: str = "",
    ) -> Optional[dict]:
        """Tandai prediksi selesai (settled) dengan hasil evaluasi. Idempotent."""
        if result not in (RESULT_BENAR, RESULT_SALAH, RESULT_FLAT):
            result = RESULT_FLAT
        with self._lock:
            rec = self._records.get(event_key)
            if rec is None or rec.get("status") == STATUS_SETTLED:
                return rec  # sudah selesai / tidak ada — idempotent
            rec["status"] = STATUS_SETTLED
            rec["result"] = result
            rec["actual_direction"] = actual_direction
            rec["price_after"] = price_after
            rec["move_pct"] = move_pct
            rec["result_reasoning"] = reasoning
            rec["settled_at"] = datetime.now(timezone.utc).isoformat()
            return dict(rec)

    # ===================== READ =====================

    def get_prediction(self, event_key: str) -> Optional[dict]:
        with self._lock:
            rec = self._records.get(event_key)
            return dict(rec) if rec else None

    def get_pending(self, now_utc: Optional[datetime] = None, settle_minutes: int = 15) -> List[dict]:
        """Prediksi pending yang sudah melewati waktu rilis + settle_minutes menit."""
        now = now_utc or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=settle_minutes)
        out = []
        with self._lock:
            for rec in self._records.values():
                if rec.get("status") != STATUS_PENDING:
                    continue
                dt = self._event_dt(rec)
                if dt is not None and dt <= cutoff:
                    out.append(dict(rec))
        out.sort(key=lambda r: r.get("event_dt_utc") or "")
        return out

    def get_stats(self) -> dict:
        with self._lock:
            records = list(self._records.values())
        total = len(records)
        settled = [r for r in records if r.get("status") == STATUS_SETTLED]
        benar = sum(1 for r in settled if r.get("result") == RESULT_BENAR)
        salah = sum(1 for r in settled if r.get("result") == RESULT_SALAH)
        flat = sum(1 for r in settled if r.get("result") == RESULT_FLAT)
        decided = benar + salah
        return {
            "total": total,
            "settled": len(settled),
            "pending": total - len(settled),
            "benar": benar,
            "salah": salah,
            "flat": flat,
            "win_rate": (benar / decided * 100.0) if decided else None,
        }

    def get_recent(self, limit: int = 10) -> List[dict]:
        with self._lock:
            records = [dict(r) for r in self._records.values()]
        records.sort(
            key=lambda r: (r.get("predicted_at") or "", r.get("event_key") or ""),
            reverse=True,
        )
        return records[:limit]

    def _event_dt(self, record: dict) -> Optional[datetime]:
        raw = record.get("event_dt_utc")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
