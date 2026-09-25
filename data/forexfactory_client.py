"""
ForexFactory Data Fetcher (via Parse.bot API).

Sumber tunggal untuk KALENDER EKONOMI & BERITA dari ForexFactory:
- get_calendar_filtered  → semua event ekonomi (impact, currency, actual/forecast/previous)
- get_news_latest        → berita terbaru (headline, url, impact, preview)

Parse.bot turns forexfactory.com into a typed JSON API. Auth memakai header
`X-API-Key` (key berawalan `pmx_`) dan key WAJIB dibaca dari environment
(PARSE_API_KEY), tidak pernah di-hardcode.

Output get_calendar() sengaja dinormalisasi ke bentuk yang sama dengan
MacroDataFetcher (event/country/time/_dt_utc/impact/actual/estimate/prev/...)
agar bisa dipakai sebagai sumber utama tanpa mengubah konsumen di hilir.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from config.settings import (
    PARSE_API_KEY,
    PARSE_API_BASE,
    FOREXFACTORY_SCRAPER_ID,
    FOREXFACTORY_ENABLED,
)
from data.cache import cache
from data.http_session import get_aiohttp_session

logger = logging.getLogger(__name__)

# Emoji bendera berdasarkan kode negara ForexFactory (US, UK, SZ, JN, EZ, dll).
_COUNTRY_FLAGS = {
    "US": "🇺🇸", "EU": "🇪🇺", "EZ": "🇪🇺", "GB": "🇬🇧", "UK": "🇬🇧", "JP": "🇯🇵",
    "JN": "🇯🇵", "CN": "🇨🇳", "AU": "🇦🇺", "NZ": "🇳🇿", "CA": "🇨🇦", "CH": "🇨🇭",
    "SZ": "🇨🇭", "SE": "🇸🇪", "NO": "🇳🇴", "DE": "🇩🇪", "GE": "🇩🇪", "FR": "🇫🇷",
    "IT": "🇮🇹", "ES": "🇪🇸", "NL": "🇳🇱", "KR": "🇰🇷", "IN": "🇮🇳", "BR": "🇧🇷",
    "MX": "🇲🇽", "ID": "🇮🇩", "HK": "🇭🇰", "SG": "🇸🇬", "RU": "🇷🇺",
}

_IMPACT_LABELS = {
    "high": "🔥 HIGH",
    "medium": "⚠️ MEDIUM",
    "low": "📊 LOW",
    "holiday": "🎏 HOLIDAY",
}


def _clean(value) -> Optional[str]:
    """ForexFactory mengirim string kosong untuk nilai yang belum ada → jadikan None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class ForexFactoryClient:
    """Client tipis untuk API ForexFactory di Parse.bot."""

    def __init__(self):
        self.api_key = PARSE_API_KEY
        self.base = PARSE_API_BASE.rstrip("/")
        self.scraper_id = FOREXFACTORY_SCRAPER_ID
        self.force_enabled = FOREXFACTORY_ENABLED

    @property
    def enabled(self) -> bool:
        """Aktif bila diizinkan env, API key, & scraper id tersedia."""
        return bool(self.force_enabled and self.api_key and self.scraper_id)

    async def _call(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """
        Panggil satu endpoint Parse.bot. Mengembalikan objek `data` bila sukses,
        atau None bila key kosong / request gagal (caller akan fallback).
        """
        if not self.enabled:
            return None

        url = f"{self.base}/scraper/{self.scraper_id}/{endpoint}"
        try:
            session = get_aiohttp_session()
            async with session.get(
                url,
                params=params or {},
                headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
                timeout=25,
            ) as resp:
                data = await resp.json(content_type=None)
        except Exception as e:  # jaringan/timeout/JSON rusak
            logger.warning(f"ForexFactory {endpoint} request error: {e}")
            return None

        if not isinstance(data, dict) or data.get("status") != "success":
            logger.warning(f"ForexFactory {endpoint} returned non-success: {str(data)[:200]}")
            return None
        payload = data.get("data")
        return payload if isinstance(payload, dict) else {}

    # ===================== ECONOMIC CALENDAR =====================

    @staticmethod
    def _parse_date(value: Optional[str], fallback: datetime) -> datetime.date:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return fallback.astimezone(ZoneInfo("Asia/Jakarta")).date()

    @staticmethod
    def _week_id(week_start: datetime) -> str:
        """Format minggu ForexFactory: 'jun09.2026' (monDD.YYYY, huruf kecil)."""
        return week_start.strftime("%b%d.%Y").lower()

    def _normalize_event(self, item: Dict, week_start: datetime) -> Optional[Dict]:
        """Ubah satu event ForexFactory → dict format MacroDataFetcher."""
        if not isinstance(item, dict) or not item.get("name"):
            return None

        tz_wib = ZoneInfo("Asia/Jakarta")
        raw_utc = item.get("event_time_utc")
        dt_utc = None
        if raw_utc:
            try:
                # Format: "2026-09-22T03:10:00Z"
                dt_utc = datetime.strptime(raw_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                dt_utc = None

        if dt_utc is not None:
            event_time_display = dt_utc.astimezone(tz_wib).strftime("%d %b %Y %H:%M WIB")
        else:
            # Event "All Day" tidak punya jam — pakai tanggal (tanpa tahun) + tahun minggunya.
            event_time_display = ""
            raw_date = item.get("date") or ""
            for fmt in ("%a %b %d", "%b %d"):
                try:
                    parsed = datetime.strptime(raw_date, fmt).replace(year=week_start.year)
                    dt_utc = parsed.replace(tzinfo=timezone.utc)
                    event_time_display = dt_utc.astimezone(tz_wib).strftime("%d %b %Y (Sepanjang Hari)")
                    break
                except ValueError:
                    continue

        impact = str(item.get("impact") or "low").lower()
        impact_label = _IMPACT_LABELS.get(impact, "📊 LOW")
        # format_calendar_text hanya mengelompokkan high/medium/low — holiday masuk "low".
        group_impact = impact if impact in ("high", "medium", "low") else "low"
        country = str(item.get("country") or "")

        return {
            "event": item.get("name", ""),
            "country": country,
            "country_emoji": _COUNTRY_FLAGS.get(country.upper(), "🌍"),
            "time": event_time_display,
            "_dt_utc": dt_utc,
            "impact": group_impact,
            "impact_label": impact_label,
            "actual": _clean(item.get("actual")),
            "estimate": _clean(item.get("forecast")),
            "prev": _clean(item.get("previous")),
            # Nilai FF sudah menyertakan satuan (mis. "39.5K", "4.6%") → unit kosong.
            "unit": "",
            "currency": item.get("currency", ""),
            "source": "forexfactory",
        }

    async def _fetch_week(self, week_start: datetime) -> List[Dict]:
        """Ambil & normalisasi event SATU minggu (semua impact)."""
        data = await self._call(
            "get_calendar_filtered",
            {"week": self._week_id(week_start), "timezone": "UTC"},
        )
        if not data:
            return []
        events = []
        for item in data.get("events") or []:
            normalized = self._normalize_event(item, week_start)
            if normalized:
                events.append(normalized)
        return events

    async def get_calendar(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        Kalender ekonomi ForexFactory untuk rentang tanggal (YYYY-MM-DD).

        API ForexFactory berbasis MINGGU, jadi rentang dipetakan ke satu/lebih
        minggu (Senin s/d Minggu) lalu difilter ke rentang yang diminta.
        Hasil dinormalisasi agar setara get_economic_calendar() di macro_data.
        """
        if not self.enabled:
            return []

        tz_wib = ZoneInfo("Asia/Jakarta")
        today = datetime.now(tz_wib)
        d1 = self._parse_date(from_date, today)
        d2 = self._parse_date(to_date, today + timedelta(days=7))

        cache_key = f"forexfactory_calendar:{d1.isoformat()}:{d2.isoformat()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        # Kumpulkan Senin untuk tiap minggu yang menyentuh rentang d1..d2.
        weeks = []
        cursor = datetime(d1.year, d1.month, d1.day) - timedelta(days=d1.weekday())
        last = datetime(d2.year, d2.month, d2.day)
        while cursor <= last:
            weeks.append(cursor)
            cursor += timedelta(days=7)

        # Fetch minggu-minggu secara paralel (bulan penuh = 5-6 request).
        results = await asyncio.gather(*(self._fetch_week(w) for w in weeks))
        events: List[Dict] = []
        for week_events in results:
            events.extend(week_events or [])

        # Filter ke rentang tanggal yang diminta (pakai _dt_utc; fallback ikut bila None).
        in_range = []
        for e in events:
            dt = e.get("_dt_utc")
            if dt is None or d1 <= dt.date() <= d2:
                in_range.append(e)

        in_range.sort(key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc))
        cache.set(cache_key, in_range, 1800)  # 30 menit (sama seperti macro_data)
        return in_range

    # ===================== NEWS =====================

    async def get_news(self, limit: int = 5) -> Dict:
        """Berita terbaru dari feed news ForexFactory (headline/url/impact/preview)."""
        if not self.enabled:
            return {"source": "ForexFactory", "error": "No API key configured", "articles": []}

        cache_key = f"forexfactory_news:{limit}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        data = await self._call("get_news_latest")
        if not data:
            return {"source": "ForexFactory", "error": "unavailable", "articles": []}

        articles = []
        seen = set()  # feed ForexFactory kadang memuat cerita yang sama berulang
        for story in data.get("stories") or []:
            if not isinstance(story, dict) or not story.get("headline"):
                continue
            url = story.get("url", "")
            key = url or story.get("headline", "")
            if key in seen:
                continue
            seen.add(key)
            articles.append({
                "title": story.get("headline", ""),
                "description": (story.get("preview") or "")[:200],
                "url": url,
                "source": "ForexFactory",
                "impact": story.get("impact", ""),
                "published": "",
            })
            if len(articles) >= limit:
                break

        result = {
            "source": "ForexFactory",
            "total_articles": len(articles),
            "articles": articles,
        }
        cache.set(cache_key, result, 600)  # 10 menit
        return result
