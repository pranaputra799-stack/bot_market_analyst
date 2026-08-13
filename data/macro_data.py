"""
Macroeconomic Data Fetcher - Mengambil data makroekonomi dari multiple sources.
Sources: FRED (St. Louis Fed), World Bank, Finnhub, Trading Economics.

Data makroekonomi adalah kunci untuk memahami mengapa harga bergerak.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from data.http_session import get_aiohttp_session, get_requests_session

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
            resp = get_requests_session().get(url, params=params, timeout=15)
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

            resp = get_requests_session().get(url, params=params, timeout=15)
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

            session = get_aiohttp_session()
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

    # Mapping nama event kalender → series FRED + cara hitung Actual/Previous.
    # Dipakai untuk mengisi nilai real-time event yang sudah rilis (FRED gratis,
    # observasi resmi BLS/BEA) agar indikator "Sudah rilis" punya angka, bukan cuma teks.
    # Mode: level (nilai langsung), mom_change (perubahan absolut), mom_pct (MoM %),
    # yoy_pct (YoY %), qoq_pct (QoQ %).
    FRED_EVENT_SERIES = {
        "Non-Farm Payrolls (NFP) & Unemployment Rate": ("PAYEMS", "mom_change", "K"),
        "CPI / Inflasi AS (YoY)": ("CPIAUCSL", "yoy_pct", "%"),
        "PPI / Harga Produsen AS (MoM)": ("PPIFIS", "mom_pct", "%"),
        "GDP AS (QoQ)": ("GDP", "qoq_pct", "%"),
        "Fed Funds Rate Decision (FOMC)": ("FEDFUNDS", "level", "%"),
        "Initial Jobless Claims (US)": ("ICSA", "level", "K"),
    }

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
                resp = await asyncio.to_thread(
                    lambda: get_requests_session().get(url, params=params, timeout=15)
                )
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

    def format_calendar_text(
        self,
        events: List[Dict],
        max_events: int = 10,
        only_high: bool = False,
        numbered: bool = False,
    ) -> str:
        """
        Format daftar event kalender ekonomi menjadi teks yang rapi.
        Waktu ditampilkan dalam WIB (UTC+7).

        Args:
            events: Daftar event dari get_economic_calendar*()
            max_events: Jumlah event maksimal yang ditampilkan
            only_high: Jika True, HANYA tampilkan event high impact (untuk /calendar)
            numbered: Jika True, tiap event diberi nomor urut (1., 2., ...) agar
                mudah dipetakan ke tombol '📊 Analisis Dampak' di /calendar
        """
        tz_wib = ZoneInfo("Asia/Jakarta")
        today = datetime.now(tz_wib).strftime("%A, %d %B %Y")

        if not events:
            scope = "bulan ini" if only_high else "7 hari ke depan"
            impact_note = "data high-impact " if only_high else ""
            return (
                f"📅 *KALENDER EKONOMI* (Waktu: WIB/UTC+7)\n📆 {today}\n\n"
                f"✅ Tidak ada rilis {impact_note}terjadwal {scope}.\n"
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

        title = "📅 *KALENDER EKONOMI* — HIGH IMPACT" if only_high else "📅 *KALENDER EKONOMI*"
        lines = [f"{title} (Waktu: WIB/UTC+7)\n📆 {today}"]
        if source:
            lines.append(source)
        lines.append("")

        # Kelompokkan berdasarkan impact
        high_events = [e for e in events if e["impact"] == "high"]
        medium_events = [e for e in events if e["impact"] == "medium"]
        low_events = [e for e in events if e["impact"] == "low"]

        # only_high: hanya group high yang ditampilkan
        groups = [high_events] if only_high else [high_events, medium_events, low_events]

        shown = 0
        for group in groups:
            if not group:
                continue
            for event in group[:max_events]:
                if shown >= max_events:
                    break

                # Baris 1: Nomor urut (opsional) + Impact + Flag + Event Name
                country = event.get("country_emoji", "")
                imp = event.get("impact_label", "📊")
                evt = event.get("event", "")
                num = f"{shown + 1}. " if numbered else ""
                lines.append(f"{num}{imp} {country} *{evt}*")

                # Baris 2: Waktu + status (sudah rilis / belum)
                evt_time = event.get("time", "")
                if evt_time:
                    lines.append(f"   🕐 _{evt_time}_")

                # Baris 3: Forecast vs Previous (label konsisten: Forecast/Previous)
                estimate = event.get("estimate")
                prev = event.get("prev")
                unit = event.get("unit", "")

                parts = []
                if estimate is not None and estimate != "":
                    parts.append(f"📊 Forecast: {estimate}{unit}")
                if prev is not None and prev != "":
                    parts.append(f"📉 Previous: {prev}{unit}")
                if parts:
                    lines.append(f"   {' | '.join(parts)}")

                # Baris 4: Actual (jika sudah rilis) + indikator status waktu
                actual = event.get("actual")
                released = self._is_event_released(event)
                if released:
                    if actual is not None and actual != "":
                        lines.append(f"   ✅ Sudah rilis — Actual: *{actual}{unit}*")
                    else:
                        lines.append("   ✅ Sudah rilis (nilai aktual belum tersedia)")
                else:
                    lines.append("   ⏳ Belum rilis")

                lines.append("")
                shown += 1

        if not high_events and not medium_events and not low_events:
            lines.append("✅ Tidak ada rilis data ekonomi besar terjadwal dalam 7 hari ke depan.\n")
        elif only_high and not high_events:
            lines.append("✅ Tidak ada rilis data high-impact terjadwal dalam periode ini.\n")

        return "\n".join(lines)

    @staticmethod
    def _is_event_released(event: Dict) -> bool:
        """
        Cek apakah event sudah lewat waktu rilisnya (berdasarkan _dt_utc WIB-aware).
        Jika _dt_utc tidak ada, fallback: ada nilai actual berarti sudah rilis.
        """
        dt_utc = event.get("_dt_utc")
        if dt_utc is not None:
            try:
                return dt_utc <= datetime.now(timezone.utc)
            except TypeError:
                pass
        actual = event.get("actual")
        return actual is not None and actual != ""

    # ===================== ACTUAL/PREVIOUS REAL-TIME (via FRED) =====================

    @cached(ttl=CACHE_MACRO_TTL)
    def _get_fred_observations(self, series_id: str, limit: int = 16) -> List[Dict]:
        """
        Ambil observasi terbaru sebuah series FRED (descending by date).
        Dipakai untuk mengisi nilai Actual/Previous event kalender yang sudah rilis.

        Args:
            series_id: ID series FRED (PAYEMS, CPIAUCSL, PPIFIS, GDP, FEDFUNDS, ICSA)
            limit: Jumlah observasi maksimal (16 cukup untuk hitung YoY)

        Returns:
            List[Dict] dengan key date (YYYY-MM-DD) & value (float), terurut desc
        """
        if not self.fred_key:
            return []
        try:
            url = "https://api.stlouisfed.org/fred/series/observations"
            params = {
                "series_id": series_id,
                "api_key": self.fred_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            }
            resp = get_requests_session().get(url, params=params, timeout=15)
            data = resp.json()
            obs = []
            for o in data.get("observations", []):
                if o.get("value") in (None, ".", ""):
                    continue
                try:
                    obs.append({"date": o["date"], "value": float(o["value"])})
                except (TypeError, ValueError):
                    continue
            return obs
        except Exception as e:
            logger.warning(f"FRED observations error ({series_id}): {e}")
            return []

    @staticmethod
    def _find_obs_index(obs: List[Dict], event_dt_utc) -> Optional[int]:
        """
        Cari indeks observasi (terurut descending) yang tanggalnya paling dekat
        dengan tanggal rilis event dari masa lalu (obs.date <= release date).
        Dipakai agar tiap event (mis. Initial Claims mingguan) mendapat nilai
        observasi yang SESUAI dengan periode rilisnya, bukan selalu yang terbaru.

        Returns:
            Indeks observasi atau None jika tidak ada yang cocok
        """
        if not obs:
            return None
        if event_dt_utc is None:
            return 0  # fallback: observasi terbaru
        try:
            release_date = event_dt_utc.date()
        except (TypeError, AttributeError):
            return 0
        for i, o in enumerate(obs):
            try:
                odate = datetime.strptime(o["date"], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if odate <= release_date:
                return i
        return None  # semua observasi lebih baru dari rilis (data belum ada)

    @staticmethod
    def _compute_fred_event_value(obs: List[Dict], mode: str, start_idx: int = 0):
        """
        Hitung (actual, previous) dari observasi FRED pada posisi start_idx
        (observasi terurut descending: start_idx = periode yang sedang dinilai):
        - level:       nilai pada start_idx & sebelumnya
        - mom_change:  selisih absolut (mis. NFP ribuan)
        - mom_pct:     perubahan % MoM (PPI)
        - yoy_pct:     perubahan % YoY (CPI, butuh 13 observasi dari start_idx)
        - qoq_pct:     perubahan % QoQ ANNUALIZED (GDP, konvensi rilis AS)

        Returns:
            (actual, previous) float atau None
        """
        v = [o["value"] for o in obs]

        def val(i):
            return v[i] if 0 <= i < len(v) else None

        def pct(new, old):
            if old in (None, 0):
                return None
            return round((new / old - 1) * 100, 2)

        try:
            if mode == "level":
                return val(start_idx), val(start_idx + 1)
            if mode == "mom_change":
                a, b = val(start_idx), val(start_idx + 1)
                if a is None or b is None:
                    return None, None
                return round(a - b, 2), (round(val(start_idx + 1) - val(start_idx + 2), 2) if val(start_idx + 2) is not None else None)
            if mode == "mom_pct":
                return pct(val(start_idx), val(start_idx + 1)), (pct(val(start_idx + 1), val(start_idx + 2)) if val(start_idx + 2) is not None else None)
            if mode == "yoy_pct":
                return pct(val(start_idx), val(start_idx + 12)), (pct(val(start_idx + 1), val(start_idx + 13)) if val(start_idx + 13) is not None else None)
            if mode == "qoq_pct":
                qoq = pct(val(start_idx), val(start_idx + 1))
                qoq_prev = pct(val(start_idx + 1), val(start_idx + 2)) if val(start_idx + 2) is not None else None
                # Annualize QoQ sesuai konvensi rilis GDP AS: (1+q)^4 - 1
                actual = round(((1 + qoq / 100) ** 4 - 1) * 100, 2) if qoq is not None else None
                prev = round(((1 + qoq_prev / 100) ** 4 - 1) * 100, 2) if qoq_prev is not None else None
                return actual, prev
        except (IndexError, TypeError):
            return None, None
        return None, None

    async def _enrich_fred_values(self, events: List[Dict]) -> List[Dict]:
        """
        Isi nilai Actual/Previous event yang SUDAH RILIS dari observasi FRED real-time
        (untuk event dari sumber jadwal resmi/FRED yang tidak punya nilai).
        Event dari Finnhub (yang sudah punya actual/estimate/prev) tidak diubah.
        """
        if not self.fred_key or not events:
            return events

        # Pilih event yang sudah rilis, belum punya actual, dan punya mapping series
        to_enrich = []
        series_needed = {}
        for e in events:
            if not self._is_event_released(e):
                continue
            if e.get("actual") not in (None, ""):
                continue  # sudah punya nilai (Finnhub)
            name = e.get("event", "")
            if name not in self.FRED_EVENT_SERIES:
                continue
            series_id, mode, unit = self.FRED_EVENT_SERIES[name]
            to_enrich.append(e)
            series_needed.setdefault(series_id, []).append(e)

        if not to_enrich:
            return events

        # Fetch observasi untuk semua series yang dibutuhkan (paralel)
        obs_cache = {}
        fetch_tasks = []
        for series_id, evs in series_needed.items():
            fetch_tasks.append(self._fetch_observations_async(series_id))
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        for series_id, res in zip(series_needed.keys(), results):
            if isinstance(res, Exception):
                logger.warning(f"FRED obs fetch failed for {series_id}: {res}")
                obs_cache[series_id] = []
            else:
                obs_cache[series_id] = res

        # Isi nilai actual/prev — cocokkan observasi dengan tanggal rilis tiap event
        for e in to_enrich:
            name = e.get("event", "")
            series_id, mode, unit = self.FRED_EVENT_SERIES[name]
            obs = obs_cache.get(series_id, [])
            idx = self._find_obs_index(obs, e.get("_dt_utc"))
            if idx is None:
                continue  # observasi untuk periode ini belum ada
            actual, prev = self._compute_fred_event_value(obs, mode, start_idx=idx)
            if actual is not None:
                e["actual"] = actual
            if prev is not None:
                e["prev"] = prev
            if actual is not None:
                e["source"] = "fred"  # nilai aktual dari FRED
        return events

    async def _fetch_observations_async(self, series_id: str) -> List[Dict]:
        """Wrapper async untuk _get_fred_observations (yang sync, via requests)."""
        return await asyncio.to_thread(self._get_fred_observations, series_id)

    async def get_economic_calendar(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        refresh: bool = False,
    ) -> List[Dict]:
        """
        Mendapatkan kalender ekonomi. Prioritas sumber:
        1. FRED (gratis, resmi, real-time) - jadwal rilis aktual BLS/BEA
        2. Finnhub (jika API key punya akses)
        3. Fallback: jadwal resmi built-in (BLS/Fed) + event berulang
        Hasil di-cache 10 menit dengan key yang menyertakan tanggal agar tidak basi.

        Args:
            from_date: Tanggal mulai (YYYY-MM-DD). Default: hari ini (WIB)
            to_date: Tanggal akhir (YYYY-MM-DD). Default: +7 hari
            refresh: Jika True, LEWATI cache dan ambil data terbaru dari sumber
                (dipakai tombol '🔁 Refresh' di /calendar).
        """
        tz_wib = ZoneInfo("Asia/Jakarta")
        today = datetime.now(tz_wib)
        if not from_date:
            from_date = today.strftime("%Y-%m-%d")
        if not to_date:
            to_date = (today + timedelta(days=7)).strftime("%Y-%m-%d")

        cache_key = f"economic_calendar:{from_date}:{to_date}"
        if not refresh:
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

        # Isi nilai Actual/Previous real-time untuk event yang sudah rilis (via FRED)
        events = await self._enrich_fred_values(events)

        # Cache 30 menit (bukan 10): kalender ekonomi berubah lambat (jadwal
        # rilis ditentukan jauh hari; nilai actual hanya bertambah setelah rilis
        # dan aftermath punya jendela lookback 6 jam). Cache lebih lama
        # memangkas fetch FRED + enrich ±3x/hari — signifikan untuk job
        # reminder/aftermath yang berjalan berkala di server free tier.
        cache.set(cache_key, events, 1800)
        return events

    def get_month_calendar_range(self) -> tuple:
        """
        Rentang tanggal kalender untuk bulan ini (WIB):
        tanggal 1 s/d akhir bulan. Dipakai /calendar agar menampilkan
        seluruh event high-impact bulan berjalan.

        Returns:
            (from_date, to_date) string YYYY-MM-DD
        """
        tz_wib = ZoneInfo("Asia/Jakarta")
        today = datetime.now(tz_wib)
        from_date = today.replace(day=1).strftime("%Y-%m-%d")
        # Akhir bulan: 1 bulan berikutnya minus 1 hari
        if today.month == 12:
            next_month_first = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month_first = today.replace(month=today.month + 1, day=1)
        to_date = (next_month_first - timedelta(days=1)).strftime("%Y-%m-%d")
        return from_date, to_date

    async def get_economic_calendar_month(self, refresh: bool = False) -> List[Dict]:
        """
        Kalender ekonomi bulan ini (tanggal 1 s/d akhir bulan WIB).
        Digunakan /calendar untuk menampilkan event high-impact bulan berjalan.

        Args:
            refresh: Jika True, lewati cache (dipakai tombol '🔁 Refresh').
        """
        from_date, to_date = self.get_month_calendar_range()
        return await self.get_economic_calendar(
            from_date=from_date, to_date=to_date, refresh=refresh
        )

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
