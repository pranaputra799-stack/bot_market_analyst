"""
Macroeconomic Data Fetcher - Mengambil data makroekonomi dari multiple sources.
Sources: FRED (St. Louis Fed), World Bank, Finnhub, Trading Economics.

Data makroekonomi adalah kunci untuk memahami mengapa harga bergerak.
"""
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

import requests
import aiohttp

from config.settings import FRED_API_KEY, FINNHUB_KEY
from config.providers import FRED_INDICATORS
from data.cache import cache, cached, CACHE_MACRO_TTL

logger = logging.getLogger(__name__)


class MacroDataFetcher:
    """
    Fetcher untuk data makroekonomi dari berbagai sumber.
    Fokus utama: FRED (Federal Reserve Economic Data) & Finnhub untuk kalender ekonomi.
    """

    def __init__(self):
        self.fred_key = FRED_API_KEY
        self.finnhub_key = FINNHUB_KEY

    # ===================== FRED DATA (Primary) =====================

    @cached(ttl=CACHE_MACRO_TTL)
    def get_fred_data(self, series_id: str, limit: int = 10) -> Dict:
        """
        Ambil data dari FRED API.

        Args:
            series_id: ID series FRED (e.g. PAYEMS, CPIAUCSL, FEDFUNDS)
            limit: Jumlah observasi terakhir

        Returns:
            Dict dengan data series atau error message
        """
        if not self.fred_key:
            return {"source": "FRED", "error": "No API key configured"}

        try:
            url = "https://api.stlouisfed.org/fred/series/observations"
            params = {
                "series_id": series_id,
                "api_key": self.fred_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            }
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()

            if "observations" not in data:
                return {"source": "FRED", "error": data.get("error_message", "Unknown error")}

            observations = []
            for obs in data["observations"]:
                if obs["value"] != ".":  # FRED uses "." for missing values
                    observations.append({
                        "date": obs["date"],
                        "value": float(obs["value"]),
                    })

            # Cari nama indikator
            indicator_name = series_id
            for name, sid in FRED_INDICATORS.items():
                if sid == series_id:
                    indicator_name = name.upper()
                    break

            return {
                "source": "FRED",
                "series_id": series_id,
                "indicator": indicator_name,
                "latest_value": observations[0]["value"] if observations else None,
                "latest_date": observations[0]["date"] if observations else None,
                "previous_value": observations[1]["value"] if len(observations) > 1 else None,
                "change": round(observations[0]["value"] - observations[1]["value"], 2) if len(observations) > 1 else None,
                "change_pct": round(((observations[0]["value"] - observations[1]["value"]) / observations[1]["value"]) * 100, 2) if len(observations) > 1 and observations[1]["value"] != 0 else None,
                "observations": observations[:5],  # 5 data terakhir
                "last_updated": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.warning(f"FRED error for {series_id}: {e}")
            return {"source": "FRED", "series_id": series_id, "error": str(e)}

    def get_multiple_indicators(self, indicators: List[str]) -> Dict[str, Dict]:
        """Ambil beberapa indikator makro sekaligus."""
        results = {}
        for indicator in indicators:
            series_id = FRED_INDICATORS.get(indicator.lower())
            if series_id:
                results[indicator] = self.get_fred_data(series_id)
        return results

    def get_fed_rate_info(self) -> Dict:
        """Dapatkan informasi Fed Funds Rate terkini."""
        data = self.get_fred_data("FEDFUNDS", limit=3)
        if "error" in data:
            return data

        latest = data.get("latest_value")
        previous = data.get("previous_value")

        # Estimasi interpretasi
        if latest is not None:
            if latest <= 0.25:
                stance = "Akomodatif (Ultra Low)"
            elif latest <= 2.0:
                stance = "Akomodatif (Low)"
            elif latest <= 3.5:
                stance = "Netral"
            elif latest <= 5.0:
                stance = "Ketat (Restrictive)"
            else:
                stance = "Sangat Ketat"

            data["interpretation"] = stance

        return data

    def get_inflation_data(self) -> Dict:
        """Dapatkan data inflasi CPI dan Core CPI."""
        cpi = self.get_fred_data("CPIAUCSL", limit=2)
        core_cpi = self.get_fred_data("CPILFESL", limit=2)

        return {
            "source": "FRED",
            "cpi": cpi.get("latest_value"),
            "cpi_date": cpi.get("latest_date"),
            "core_cpi": core_cpi.get("latest_value"),
            "core_cpi_date": core_cpi.get("latest_date"),
            "cpi_change": cpi.get("change"),
            "core_cpi_change": core_cpi.get("change"),
        }

    # ===================== WORLD BANK DATA =====================

    @cached(ttl=CACHE_MACRO_TTL)
    def get_world_bank_data(self, indicator: str, country: str = "US") -> Dict:
        """
        Ambil data dari World Bank API.

        Args:
            indicator: Kode indikator World Bank (e.g. NY.GDP.MKTP.CD, FP.CPI.TOTL.ZG)
            country: Kode negara (e.g. US, ID, JP)
        """
        try:
            url = f"http://api.worldbank.org/v2/country/{country}/indicator/{indicator}"
            params = {"format": "json", "per_page": 5}

            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()

            if len(data) < 2 or not data[1]:
                return {"source": "World Bank", "error": "No data available"}

            observations = []
            for item in data[1]:
                if item["value"]:
                    observations.append({
                        "date": item["date"],
                        "value": float(item["value"]),
                    })

            return {
                "source": "World Bank",
                "indicator": indicator,
                "country": country,
                "observations": observations,
                "latest_value": observations[0]["value"] if observations else None,
                "latest_date": observations[0]["date"] if observations else None,
            }

        except Exception as e:
            logger.warning(f"World Bank error: {e}")
            return {"source": "World Bank", "error": str(e)}

    # ===================== ECONOMIC CALENDAR (via Finnhub API) =====================

    async def get_economic_calendar_finnhub(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None
    ) -> List[Dict]:
        """
        Mendapatkan kalender ekonomi REAL dari Finnhub API.
        Berisi jadwal rilis data ekonomi aktual dengan forecast & previous.

        Args:
            from_date: Tanggal mulai (YYYY-MM-DD). Default: hari ini
            to_date: Tanggal akhir (YYYY-MM-DD). Default: +7 hari

        Returns:
            List[Dict] dengan event ekonomi:
            - event: Nama event
            - country: Negara (US, EU, JP, etc)
            - time: Waktu rilis
            - impact: Tingkat dampak (high/medium/low)
            - actual: Nilai aktual (None jika belum rilis)
            - estimate: Konsensus/forecast
            - prev: Nilai sebelumnya
            - unit: Satuan (%/K/M)
        """
        if not self.finnhub_key:
            logger.warning("Finnhub key not configured, using official schedule calendar")
            return self._get_scheduled_calendar(from_date, to_date)

        try:
            tz_wib = ZoneInfo("Asia/Jakarta")  # UTC+7
            today_wib = datetime.now(tz_wib)

            if not from_date:
                from_date = today_wib.strftime("%Y-%m-%d")
            if not to_date:
                to_date = (today_wib + timedelta(days=7)).strftime("%Y-%m-%d")

            url = "https://finnhub.io/api/v1/calendar/economic"
            params = {
                "from": from_date,
                "to": to_date,
                "token": self.finnhub_key,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=15) as resp:
                    data = await resp.json()

            if "economicCalendar" not in data:
                err_msg = str(data.get("error", ""))
                if "access" in err_msg.lower():
                    logger.warning(
                        "Finnhub economic calendar tidak tersedia untuk API key ini "
                        "(kemungkinan butuh plan berbayar). Menggunakan jadwal resmi BLS/Fed."
                    )
                else:
                    logger.warning(f"Finnhub calendar error: {data}")
                return self._get_scheduled_calendar(from_date, to_date)

            events = []
            for item in data["economicCalendar"]:
                # Map impact level to label
                impact = item.get("impact", "low").lower()
                if impact == "high":
                    impact_label = "🔥 HIGH"
                elif impact == "medium":
                    impact_label = "⚠️ MEDIUM"
                else:
                    impact_label = "📊 LOW"

                # Country flag emoji
                country_emoji = self._get_country_emoji(item.get("country", ""))

                # Finnhub mengembalikan waktu dalam format UTC.
                # Konversi ke WIB (Asia/Jakarta = UTC+7) agar sesuai jadwal sebenarnya.
                raw_time = item.get("time", "")
                event_time_display = ""
                event_dt_utc = None

                if raw_time:
                    # Finnhub bisa kirim format: "2024-08-02 12:30:00" (UTC)
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                        try:
                            dt_naive = datetime.strptime(raw_time[:len(fmt)+2].strip(), fmt)
                            # Tandai sebagai UTC lalu konversi ke WIB
                            dt_utc = dt_naive.replace(tzinfo=timezone.utc)
                            dt_wib = dt_utc.astimezone(tz_wib)
                            event_dt_utc = dt_utc
                            if fmt == "%Y-%m-%d":
                                # Hanya ada tanggal, tidak ada jam
                                event_time_display = dt_wib.strftime("%d %b %Y (Sepanjang Hari)")
                            else:
                                event_time_display = dt_wib.strftime("%d %b %Y %H:%M WIB")
                            break
                        except ValueError:
                            continue

                if not event_time_display:
                    event_time_display = raw_time  # fallback jika parse gagal

                events.append({
                    "event": item.get("event", "Unknown Event"),
                    "country": item.get("country", ""),
                    "country_emoji": country_emoji,
                    "time": event_time_display,
                    "_dt_utc": event_dt_utc,  # untuk sorting
                    "impact": impact,
                    "impact_label": impact_label,
                    "actual": item.get("actual"),
                    "estimate": item.get("estimate"),
                    "prev": item.get("prev"),
                    "unit": item.get("unit", ""),
                    "currency": item.get("currency", ""),
                    "source": "finnhub",
                })

            # Sort by datetime (UTC) agar urutan benar
            events.sort(key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc))

            if not events:
                logger.info("No calendar events from Finnhub, using official schedule")
                return self._get_scheduled_calendar(from_date, to_date)

            logger.info(f"Got {len(events)} economic calendar events from Finnhub (converted to WIB)")
            return events

        except Exception as e:
            logger.warning(f"Finnhub calendar error: {e}")
            return self._get_scheduled_calendar(from_date, to_date)

    def _get_country_emoji(self, country: str) -> str:
        """Dapatkan emoji bendera berdasarkan kode negara."""
        flags = {
            "US": "🇺🇸", "EU": "🇪🇺", "GB": "🇬🇧", "JP": "🇯🇵",
            "CN": "🇨🇳", "AU": "🇦🇺", "NZ": "🇳🇿", "CA": "🇨🇦",
            "CH": "🇨🇭", "SE": "🇸🇪", "NO": "🇳🇴", "DE": "🇩🇪",
            "FR": "🇫🇷", "IT": "🇮🇹", "ES": "🇪🇸", "NL": "🇳🇱",
            "KR": "🇰🇷", "IN": "🇮🇳", "BR": "🇧🇷", "MX": "🇲🇽",
            "ID": "🇮🇩", "HK": "🇭🇰", "SG": "🇸🇬", "RU": "🇷🇺",
        }
        return flags.get(country.upper(), "🌍")

    # Jadwal rilis resmi 2026 (BLS & Federal Reserve) - diambil dari sumber resmi.
    # BLS CPI: https://www.bls.gov/schedule/news_release/cpi.htm
    # BLS PPI: https://www.bls.gov/schedule/news_release/ppi.htm
    # FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
    CPI_RELEASE_DATES_2026 = [
        (1, 13), (2, 13), (3, 11), (4, 10), (5, 12), (6, 10),
        (7, 14), (8, 12), (9, 11), (10, 14), (11, 10), (12, 10),
    ]
    PPI_RELEASE_DATES_2026 = [
        (1, 14), (1, 30), (2, 27), (3, 18), (4, 14), (5, 13), (6, 11),
        (7, 15), (8, 13), (9, 10), (10, 15), (11, 13), (12, 15),
    ]
    # Tanggal keputusan FOMC (hari kedua meeting, statement 14:00 ET)
    FOMC_DECISION_DATES_2026 = [
        (1, 28), (3, 18), (4, 29), (6, 17),
        (7, 29), (9, 16), (10, 28), (12, 9),
    ]

    # Jadwal rilis 2027.
    # FOMC 2027: RESMI dari Federal Reserve (press release 5 Sep 2025),
    #   https://www.federalreserve.gov/newsevents/pressreleases/monetary20250905a.htm
    #   Meeting: Jan 26-27, Mar 16-17, Apr 27-28, Jun 8-9, Jul 27-28,
    #            Sep 14-15, Oct 26-27, Dec 7-8 (keputusan di hari kedua).
    # CPI/PPI 2027: BLS biasanya merilis jadwal resmi ~Okt/Nov 2026, jadi ini
    #   PROYEKSI pola rilis (minggu kedua tiap bulan, 08:30 ET) - ditandai perkiraan.
    CPI_RELEASE_DATES_2027 = [
        (1, 13), (2, 10), (3, 10), (4, 13), (5, 12), (6, 10),
        (7, 13), (8, 11), (9, 14), (10, 13), (11, 10), (12, 10),
    ]
    PPI_RELEASE_DATES_2027 = [
        (1, 13), (2, 11), (3, 17), (4, 14), (5, 13), (6, 17),
        (7, 14), (8, 12), (9, 15), (10, 13), (11, 10), (12, 14),
    ]
    FOMC_DECISION_DATES_2027 = [
        (1, 27), (3, 17), (4, 28), (6, 9),
        (7, 28), (9, 15), (10, 27), (12, 8),
    ]

    # Jadwal rilis real-time via FRED release/dates (gratis, resmi BLS/BEA).
    # Format: (release_id, nama event, impact_label, unit)
    FRED_RELEASE_CALENDAR = [
        (50, "Non-Farm Payrolls (NFP) & Unemployment Rate", "🔥 HIGH", "K"),
        (10, "CPI / Inflasi AS (YoY)", "🔥 HIGH", "%"),
        (46, "PPI / Harga Produsen AS (MoM)", "⚠️ MEDIUM", "%"),
        (53, "GDP AS (QoQ)", "🔥 HIGH", "%"),
    ]

    async def get_economic_calendar_fred(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        Kalender ekonomi REAL-TIME dari FRED API (gratis & resmi).
        Mengambil jadwal rilis aktual BLS/BEA via /fred/release/dates.
        FRED hanya memberi tanggal; waktu rilis standar 08:30 ET dipakai
        dan dikonversi ke WIB secara DST-aware.
        """
        if not self.fred_key:
            return []

        tz_et = ZoneInfo("America/New_York")
        tz_wib = ZoneInfo("Asia/Jakarta")
        today_wib = datetime.now(tz_wib)
        d1 = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else today_wib.date()
        d2 = datetime.strptime(to_date, "%Y-%m-%d").date() if to_date else d1 + timedelta(days=7)

        events: List[Dict] = []

        def add_event(event: str, impact_label: str, dt_et, unit: str = ""):
            """Tambahkan event jika tanggal rilisnya berada dalam rentang d1..d2."""
            if dt_et is None:
                return
            dt_et_aware = dt_et.replace(tzinfo=tz_et)
            if not (d1 <= dt_et_aware.date() <= d2):
                return
            dt_wib = dt_et_aware.astimezone(tz_wib)
            impact = "high" if "HIGH" in impact_label else ("medium" if "MEDIUM" in impact_label else "low")
            events.append({
                "event": event,
                "country": "US",
                "country_emoji": "🇺🇸",
                "time": dt_wib.strftime("%d %b %Y %H:%M WIB"),
                "_dt_utc": dt_et_aware.astimezone(timezone.utc),
                "impact": impact,
                "impact_label": impact_label,
                "actual": None,
                "estimate": None,
                "prev": None,
                "unit": unit,
                "currency": "USD",
                "source": "fred",
            })

        # ===== Jadwal rilis aktual dari FRED untuk tiap release (fetch paralel) =====
        async def fetch_release(release_id: int):
            url = "https://api.stlouisfed.org/fred/release/dates"
            params = {
                "release_id": release_id,
                "include_release_dates_with_no_data": "true",
                "api_key": self.fred_key,
                "file_type": "json",
            }
            try:
                resp = await asyncio.to_thread(requests.get, url, params=params, timeout=15)
                return release_id, resp.json()
            except Exception as e:
                logger.warning(f"FRED release calendar error (release {release_id}): {e}")
                return release_id, None

        release_map = {rid: (name, label, unit) for rid, name, label, unit in self.FRED_RELEASE_CALENDAR}
        results = await asyncio.gather(*(fetch_release(rid) for rid in release_map))

        for release_id, data in results:
            if not data:
                continue
            event_name, impact_label, unit = release_map[release_id]
            for item in data.get("release_dates", []):
                raw = item.get("date", "")
                if not raw:
                    continue
                try:
                    dt_naive = datetime.strptime(raw, "%Y-%m-%d")
                except ValueError:
                    continue
                if dt_naive.date() > d2:
                    break  # tanggal terurut ascending, sisanya lewati
                if dt_naive.date() >= d1:
                    # Rilis makro AS standar jam 08:30 ET
                    add_event(event_name, impact_label, dt_naive.replace(hour=8, minute=30), unit=unit)
                    break  # maksimal 1 event per release dalam jendela 7 hari

        # ===== Event berulang: Initial Claims (tiap Kamis) + FOMC (jadwal resmi) =====
        fred_ok = any(data for _, data in results)
        self._add_recurring_us_events(d1, d2, add_event)

        # Jika semua panggilan FRED gagal, jangan salah menandai sebagai sumber FRED
        if not fred_ok:
            for e in events:
                e["source"] = "jadwal_resmi"

        events.sort(key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc))
        return events

    def _add_recurring_us_events(self, d1, d2, add_event):
        """
        Tambahkan event berulang yang terjadwal pasti:
        - Initial Jobless Claims: setiap Kamis, 08:30 ET
        - FOMC Rate Decision: jadwal resmi Fed 2026-2027, 14:00 ET
        """
        # ===== Initial Jobless Claims - setiap Kamis, 08:30 ET =====
        day = d1
        while day <= d2:
            days_until_thu = (3 - day.weekday()) % 7
            thu = day + timedelta(days=days_until_thu)
            if thu > d2:
                break
            add_event(
                "Initial Jobless Claims (US)",
                "⚠️ MEDIUM",
                datetime(thu.year, thu.month, thu.day, 8, 30),
                unit="K",
            )
            day = thu + timedelta(days=7)

        # ===== FOMC Rate Decision - jadwal resmi Fed 2026 & 2027, 14:00 ET =====
        for year, dates in [
            (2026, self.FOMC_DECISION_DATES_2026),
            (2027, self.FOMC_DECISION_DATES_2027),
        ]:
            for m, d in dates:
                add_event("Fed Funds Rate Decision (FOMC)", "🔥 HIGH", datetime(year, m, d, 14, 0), unit="%")

    def _get_scheduled_calendar(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> List[Dict]:
        """
        Kalender ekonomi berdasarkan jadwal rilis (BLS/Fed). Akurat untuk tahun
        dengan jadwal resmi; proyeksi tahun tanpa jadwal resmi ditandai "(perkiraan)".
        Hanya menampilkan event yang BENAR-BENAR dijadwalkan pada rentang tanggal
        yang diminta (bukan asal taruh di hari ini).

        Sumber:
        - NFP/Unemployment: rilis Jumat pertama tiap bulan, 08:30 ET
        - Initial Claims:   setiap Kamis, 08:30 ET
        - CPI:              jadwal BLS 2026 resmi + 2027 proyeksi, 08:30 ET
        - PPI:              jadwal BLS 2026 resmi + 2027 proyeksi, 08:30 ET
        - FOMC:             jadwal resmi Fed 2026-2027, 14:00 ET
        """
        tz_et = ZoneInfo("America/New_York")
        tz_wib = ZoneInfo("Asia/Jakarta")

        today_wib = datetime.now(tz_wib)
        d1 = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else today_wib.date()
        d2 = datetime.strptime(to_date, "%Y-%m-%d").date() if to_date else d1 + timedelta(days=7)

        events: List[Dict] = []

        def add_event(event: str, impact_label: str, dt_et, unit: str = "", is_estimate: bool = False):
            """Tambahkan event jika tanggal rilisnya berada dalam rentang d1..d2."""
            if dt_et is None:
                return
            dt_et_aware = dt_et.replace(tzinfo=tz_et)
            if not (d1 <= dt_et_aware.date() <= d2):
                return
            dt_wib = dt_et_aware.astimezone(tz_wib)
            impact = "high" if "HIGH" in impact_label else ("medium" if "MEDIUM" in impact_label else "low")
            time_str = dt_wib.strftime("%d %b %Y %H:%M WIB")
            if is_estimate:
                time_str += " (perkiraan)"
            events.append({
                "event": event,
                "country": "US",
                "country_emoji": "🇺🇸",
                "time": time_str,
                "_dt_utc": dt_et_aware.astimezone(timezone.utc),
                "impact": impact,
                "impact_label": impact_label,
                "actual": None,
                "estimate": None,
                "prev": None,
                "unit": unit,
                "currency": "USD",
                "source": "jadwal_resmi",
            })

        # ===== NFP & Unemployment Rate - Jumat pertama tiap bulan, 08:30 ET =====
        month = d1.replace(day=1)
        while month <= d2:
            # Jumat pertama bulan tersebut (BLS mundur ke Jumat berikutnya jika jatuh tanggal 1-2)
            first_friday = month + timedelta(days=(4 - month.weekday()) % 7)
            if first_friday.day <= 2:
                first_friday += timedelta(days=7)
            add_event(
                "Non-Farm Payrolls (NFP) & Unemployment Rate",
                "🔥 HIGH",
                datetime(first_friday.year, first_friday.month, first_friday.day, 8, 30),
                unit="K",
            )
            # Lanjut ke bulan berikutnya
            if month.month == 12:
                month = month.replace(year=month.year + 1, month=1)
            else:
                month = month.replace(month=month.month + 1)

        # ===== CPI & PPI - jadwal BLS 08:30 ET (2026 resmi, 2027 proyeksi perkiraan) =====
        for year, cpi_dates, ppi_dates, is_estimate in [
            (2026, self.CPI_RELEASE_DATES_2026, self.PPI_RELEASE_DATES_2026, False),
            (2027, self.CPI_RELEASE_DATES_2027, self.PPI_RELEASE_DATES_2027, True),
        ]:
            for m, d in cpi_dates:
                add_event("CPI / Inflasi AS (YoY)", "🔥 HIGH", datetime(year, m, d, 8, 30), unit="%", is_estimate=is_estimate)
            for m, d in ppi_dates:
                add_event("PPI / Harga Produsen AS (MoM)", "⚠️ MEDIUM", datetime(year, m, d, 8, 30), unit="%", is_estimate=is_estimate)

        # ===== Event berulang: Initial Claims (tiap Kamis) + FOMC (jadwal resmi) =====
        self._add_recurring_us_events(d1, d2, add_event)

        events.sort(key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc))
        return events

    def format_calendar_text(self, events: List[Dict], max_events: int = 10) -> str:
        """
        Format daftar event kalender ekonomi menjadi teks yang rapi.
        Waktu ditampilkan dalam WIB (UTC+7).
        """
        tz_wib = ZoneInfo("Asia/Jakarta")
        today = datetime.now(tz_wib).strftime("%A, %d %B %Y")

        if not events:
            return (
                f"📅 *KALENDER EKONOMI* (Waktu: WIB/UTC+7)\n📆 {today}\n\n"
                f"✅ Tidak ada rilis data ekonomi besar terjadwal dalam 7 hari ke depan.\n"
                f"ℹ️ Sumber: jadwal resmi (BLS/Fed) - data real-time tidak tersedia."
            )

        # Deteksi sumber data untuk kejujuran label
        source = ""
        sources = {e.get("source") for e in events if e.get("source")}
        if "finnhub" in sources:
            source = "🛰️ Sumber: Finnhub (real-time)"
        elif "fred" in sources:
            source = "🏛️ Sumber: FRED (jadwal rilis resmi real-time)"
        elif "jadwal_resmi" in sources:
            source = "🗓️ Sumber: Jadwal rilis resmi (BLS/Fed)"

        lines = [f"📅 *KALENDER EKONOMI* (Waktu: WIB/UTC+7)\n📆 {today}"]
        if source:
            lines.append(source)
        lines.append("")

        # Kelompokkan berdasarkan impact
        high_events = [e for e in events if e["impact"] == "high"]
        medium_events = [e for e in events if e["impact"] == "medium"]
        low_events = [e for e in events if e["impact"] == "low"]

        shown = 0

        for group in [high_events, medium_events, low_events]:
            if not group:
                continue
            for event in group[:max_events]:
                if shown >= max_events:
                    break

                # Baris 1: Impact + Flag + Event Name
                country = event.get("country_emoji", "")
                imp = event.get("impact_label", "📊")
                evt = event.get("event", "")
                lines.append(f"{imp} {country} *{evt}*")

                # Baris 2: Waktu
                evt_time = event.get("time", "")
                if evt_time:
                    lines.append(f"   🕐 _{evt_time}_")

                # Baris 3: Forecast vs Previous
                estimate = event.get("estimate")
                prev = event.get("prev")
                unit = event.get("unit", "")

                parts = []
                if estimate is not None and estimate != "":
                    parts.append(f"📊 Konsensus: {estimate}{unit}")
                if prev is not None and prev != "":
                    parts.append(f"📉 Sebelumnya: {prev}{unit}")
                if parts:
                    lines.append(f"   {' | '.join(parts)}")

                # Baris 4: Actual value (jika sudah rilis)
                actual = event.get("actual")
                if actual is not None and actual != "":
                    lines.append(f"   ✅ Aktual: *{actual}{unit}*")

                lines.append("")
                shown += 1

        if not high_events and not medium_events and not low_events:
            lines.append("✅ Tidak ada rilis data ekonomi besar terjadwal dalam 7 hari ke depan.\n")

        return "\n".join(lines)

    async def get_economic_calendar(self) -> List[Dict]:
        """
        Mendapatkan kalender ekonomi. Prioritas sumber:
        1. FRED (gratis, resmi, real-time) - jadwal rilis aktual BLS/BEA
        2. Finnhub (jika API key punya akses)
        3. Fallback: jadwal resmi built-in (BLS/Fed) + event berulang
        Hasil di-cache 10 menit dengan key yang menyertakan tanggal agar tidak basi.
        """
        tz_wib = ZoneInfo("Asia/Jakarta")
        today = datetime.now(tz_wib)
        from_date = today.strftime("%Y-%m-%d")
        to_date = (today + timedelta(days=7)).strftime("%Y-%m-%d")

        cache_key = f"economic_calendar:{from_date}:{to_date}"
        cached_result = cache.get(cache_key)
        if cached_result:
            return cached_result

        # 1) FRED (primary - gratis & real-time)
        events = await self.get_economic_calendar_fred(from_date=from_date, to_date=to_date)
        # 2) Finnhub (jika FRED gagal/kunci kosong; endpoint ini butuh plan berbayar)
        if not events:
            events = await self.get_economic_calendar_finnhub(from_date=from_date, to_date=to_date)
        # 3) Fallback built-in (defensif; finnhub sudah punya fallback internal sendiri)
        if not events:
            events = self._get_scheduled_calendar(from_date=from_date, to_date=to_date)

        cache.set(cache_key, events, 600)  # Cache 10 menit
        return events

    # ===================== MACRO SUMMARY =====================

    def get_macro_summary(self) -> str:
        """Mendapatkan ringkasan data makro untuk morning brief."""
        key_indicators = ["fed rate", "cpi", "unemployment", "gdp"]

        lines = ["🏛️ *DATA MAKROEKONOMI*\n"]
        for indicator in key_indicators:
            series_id = FRED_INDICATORS.get(indicator)
            if series_id:
                data = self.get_fred_data(series_id, limit=2)
                if "error" not in data:
                    value = data.get("latest_value")
                    date = data.get("latest_date")
                    lines.append(f"• *{indicator.upper()}*: {value} (terakhir: {date})")

        return "\n".join(lines)
