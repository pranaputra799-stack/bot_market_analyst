from telegram.ext import (
    Application,
    ContextTypes,
)
from bot.messages import (
    DISCLAIMER,
    format_price,
    MORNING_BRIEF_TEMPLATE,
)
from typing import Dict, List, Optional, Tuple
from config.settings import (
    MORNING_BRIEF_TIMEZONE,
    MORNING_BRIEF_CHAT_IDS,
    ECONOMIC_ALERT_LEAD_HOURS,
    EVENT_AFTERMATH_ENABLED,
    EVENT_AFTERMATH_LOOKBACK_HOURS,
    NEWS_PREDICTION_ENABLED,
    NEWS_PREDICTION_LEAD_MINUTES,
    NEWS_PREDICTION_SETTLE_MINUTES,
    NEWS_PREDICTION_MIN_MOVE_PCT,
    NEWS_PREDICTION_MAX_PER_RUN,
    COT_PREWARM_HOUR,
    COT_PREWARM_DAYS,
)
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
import asyncio
from datetime import datetime, timedelta, timezone
from data.cot import (
    COT_INSTRUMENTS,
    cot_data_to_json,
    extract_market,
    fetch_tff_rows,
    fetch_year_rows,
)
from data.database import db
from prompts.loader import format_prompt
from utils.admin_alerts import notify_admins
from utils.sessions import sessions_just_opened, format_session_text
import hashlib
import logging

from bot.handlers_utils import (
    _strip_provider_prefix,
    safe_edit_message_text,
    safe_reply_text,
    safe_send_message,
    strip_markdown_asterisks,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)

class SchedulerJobsMixin:
    """Job terjadwal & prediksi news/aftermath — dipanggil main.py (JobQueue)."""

    async def send_session_alerts(self, application: Application):
        """Kirim alert sesi market baru buka ke subscriber (job terjadwal)."""
        try:
            now_utc = datetime.now(timezone.utc)
            opened = sessions_just_opened(now_utc)
            if not opened:
                return
            subscribers = await db.get_all_subscribers_async()
            if not subscribers:
                return
            # Dedup per (sesi, tanggal) — prune kunci lama agar set tidak membengkak
            today = now_utc.strftime("%Y-%m-%d")
            yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
            sent_keys = set(application.bot_data.get("session_alert_sent", set()))
            sent_keys = {k for k in sent_keys if k.split("|")[-1] in (today, yesterday)}
            to_send = []
            for s in opened:
                key = f"{s.key}|{today}"
                if key not in sent_keys:
                    sent_keys.add(key)
                    to_send.append(s)
            application.bot_data["session_alert_sent"] = sent_keys
            for s in to_send:
                text = format_session_text(s, MORNING_BRIEF_TIMEZONE)
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot,
                            chat_id=chat_id,
                            text=text,
                            parse_mode="Markdown",
                        )
                    except Exception as ex:
                        logger.error(f"Gagal kirim alert sesi ke {chat_id}: {ex}")
        except Exception as e:
            logger.warning(f"Session alert error: {e}")
    async def _generate_morning_brief(self, watchlist: Optional[List[str]] = None) -> str:
        """
        Generate morning brief dengan data terkini.
        Menggabungkan data pasar, makro, kalender ekonomi, berita, dan AI-generated outlook.

        Args:
            watchlist: Daftar instrumen favorit user — bila diisi, prompt brief
                difokuskan ke instrumen ini (tetap 1 panggilan AI; data global
                disajikan sebagai konteks). None/kosong = brief global biasa.

        Defensif penuh: SATU sumber data error TIDAK boleh menggagalkan seluruh
        brief (tiap bagian punya fallback teks), dan kegagalan AI menghasilkan
        placeholder ramah — bukan exception yang membuat /morning tidak merespons.
        """
        # Tanggal harus sesuai zona WIB, bukan waktu server (yang bisa UTC)
        today = datetime.now(ZoneInfo(MORNING_BRIEF_TIMEZONE)).strftime("%A, %d %B %Y")
        watchlist_text = ", ".join(watchlist) if watchlist else ""

        # Gather data secara parallel — return_exceptions: satu sumber gagal
        # (jaringan Yahoo/FRED/news) tidak membatalkan bagian lain.
        results = await asyncio.gather(
            asyncio.to_thread(self.market.get_market_summary),
            asyncio.to_thread(self.macro.get_macro_summary),
            self.macro.get_economic_calendar(),
            self.news.get_news_summary("FOREX"),
            self._get_sentiment_text("FOREX"),
            return_exceptions=True,
        )
        market_summary, macro_summary, calendar_events, news_summary, sentiment_text = results

        # Normalisasi exception → fallback teks per-bagian
        if isinstance(market_summary, Exception):
            logger.warning(f"Morning brief: market summary gagal: {market_summary}")
            market_summary = "📊 Data pasar tidak tersedia saat ini."
        if isinstance(macro_summary, Exception):
            logger.warning(f"Morning brief: macro summary gagal: {macro_summary}")
            macro_summary = "🏛️ Data makro tidak tersedia saat ini."
        if isinstance(calendar_events, Exception):
            logger.warning(f"Morning brief: kalender ekonomi gagal: {calendar_events}")
            calendar_events = []
        if isinstance(news_summary, Exception):
            logger.warning(f"Morning brief: berita gagal: {news_summary}")
            news_summary = "📰 Berita tidak tersedia saat ini."
        if isinstance(sentiment_text, Exception):
            sentiment_text = ""

        # Format kalender ekonomi untuk morning brief (top 3 high impact)
        try:
            calendar_text = self.macro.format_calendar_text(calendar_events, max_events=3)
        except Exception as e:
            logger.warning(f"Morning brief: format kalender gagal: {e}")
            calendar_text = "📅 Tidak ada event terjadwal yang tersedia."

        # AI-powered outlook & catalysts using multi-agent analysis
        if self.analysis_director:
            try:
                # Gunakan multi-agent untuk analisis yang lebih dalam
                analysis_prompt = self._build_morning_brief_prompt(
                    today, market_summary, macro_summary, calendar_text, news_summary,
                    sentiment_text, watchlist_text,
                )

                result = await self.analysis_director.analyze(analysis_prompt)

                # Analisis GAGAL (mis. semua AI provider rate-limit) → jangan
                # tampilkan pesan error yang meng-echo prompt morning brief
                # sebagai "outlook" — jatuh ke path legacy yang lebih ringan.
                # getattr: result bisa AnalysisResult (punya .error) atau objek
                # serupa lain (test/fake) yang tidak punya atribut itu.
                if getattr(result, "error", None):
                    logger.warning(
                        f"Multi-agent morning brief gagal: {result.error} — fallback legacy"
                    )
                    raise RuntimeError(f"Multi-agent analysis failed: {result.error}")

                # Extract from analysis result (bersihkan prefix [via ...] + simbol *)
                ai_content = strip_markdown_asterisks(_strip_provider_prefix(result.final_response or ""))
                outlook_part, catalysts_part = self._split_outlook_catalysts(ai_content)

                return MORNING_BRIEF_TEMPLATE.format(
                    date=today,
                    market_summary=market_summary,
                    macro_summary=macro_summary,
                    calendar_summary=calendar_text,
                    news_summary=news_summary,
                    sentiment_summary=sentiment_text or "Sentimen pasar tidak tersedia.",
                    outlook=outlook_part,
                    catalysts=catalysts_part,
                )
            except Exception as e:
                logger.warning(f"Multi-agent morning brief failed: {e}, falling back to legacy")

        # Fallback: legacy single-prompt method — pakai generate_async agar tidak
        # memblokir event loop (generate sync bisa berjalan 60s+ saat provider
        # down). Kegagalan AI → placeholder ramah, bukan exception.
        try:
            outlook_prompt = self._build_morning_brief_prompt(
                today, market_summary, macro_summary, calendar_text, news_summary,
                sentiment_text, watchlist_text,
            )
            ai_response = await self.ai.generate_async(
                outlook_prompt, use_cache=True, max_tokens=2048
            )
            ai_content = strip_markdown_asterisks(_strip_provider_prefix(ai_response))
            outlook, catalysts = self._split_outlook_catalysts(ai_content)
        except Exception as e:
            logger.warning(f"Legacy morning brief AI failed: {e}")
            outlook = "Analisis AI tidak tersedia saat ini — data pasar tetap disajikan di atas."
            catalysts = "Coba lagi dalam beberapa menit."

        return MORNING_BRIEF_TEMPLATE.format(
            date=today,
            market_summary=market_summary,
            macro_summary=macro_summary,
            calendar_summary=calendar_text,
            news_summary=news_summary,
            sentiment_summary=sentiment_text or "Sentimen pasar tidak tersedia.",
            outlook=outlook,
            catalysts=catalysts,
        )
    @staticmethod
    def _split_outlook_catalysts(ai_content: str) -> Tuple[str, str]:
        """Pisah konten AI → (outlook, catalysts) TANPA memotong konten.

        Marker `KATALIS UTAMA:` memisahkan dua bagian. Bila marker tidak ada,
        seluruh konten dianggap outlook (catalysts → placeholder).
        """
        ai_content = (ai_content or "").strip()
        if "KATALIS UTAMA" in ai_content:
            sections = ai_content.split("KATALIS UTAMA:")
            outlook = sections[0].replace("OUTLOOK:", "").replace("OUTLOOK", "").strip()
            catalysts = sections[1].strip() if len(sections) > 1 else ""
        else:
            outlook = ai_content
            catalysts = ""
        if not outlook:
            outlook = "Belum ada data analisis untuk hari ini."
        if not catalysts:
            catalysts = "Belum ada katalis utama yang teridentifikasi hari ini."
        return outlook, catalysts
    def _build_morning_brief_prompt(
        self,
        today: str,
        market_summary: str,
        macro_summary: str,
        calendar_text: str,
        news_summary: str,
        sentiment_text: str = "",
        watchlist: str = "",
    ) -> str:
        """
        Bangun prompt morning brief (dipakai path multi-agent & legacy).

        Args:
            watchlist: Daftar instrumen favorit user (string gabungan) — diisi
                placeholder {WATCHLIST}; kosong = analisis pasar global.

        Konten prompt DIAMBIL dari `prompts/morning_brief.txt` (single source
        of truth) — edit file tersebut untuk mengubah perilaku tanpa mengubah
        kode. Fallback ke template bawaan bila file tidak tersedia.
        """
        sentiment_section = sentiment_text or "Sentimen pasar tidak tersedia."
        return format_prompt(
            "morning_brief",
            DATE=today,
            WATCHLIST=watchlist or "(tidak ada — analisis pasar secara umum)",
            market_data=market_summary,
            macro_data=macro_summary,
            calendar_data=calendar_text,
            news_data=news_summary,
            sentiment_data=sentiment_section,
        )
    def _get_alert_subscribers(self, application: Application) -> set:
        """Dapatkan daftar chat_id yang subscribe notifikasi event."""
        return set(application.bot_data.get("event_alert_subscribers", set()))
    async def send_scheduled_event_digest(self, application: Application):
        """
        Kirim digest harian event ekonomi HIGH-IMPACT hari ini ke semua subscriber.
        Dipanggil scheduler setiap pagi (ECONOMIC_ALERT_DIGEST_HOUR).
        """
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            events = await self.macro.get_economic_calendar()
            tz_wib = ZoneInfo(MORNING_BRIEF_TIMEZONE)
            today = datetime.now(tz_wib).date()

            # Filter event HIGH impact yang rilis hari ini
            high_today = []
            for e in events:
                if e.get("impact") != "high":
                    continue
                dt = e.get("_dt_utc")
                if dt and dt.tzinfo is not None and dt.astimezone(tz_wib).date() == today:
                    high_today.append(e)

            if not high_today:
                message = (
                    f"📅 *EVENT EKONOMI HARI INI*\n📆 {today.strftime('%A, %d %B %Y')}\n\n"
                    f"✅ Tidak ada rilis data high-impact terjadwal hari ini."
                )
            else:
                lines = [
                    "📅 *EVENT EKONOMI HIGH-IMPACT HARI INI*",
                    f"📆 {today.strftime('%A, %d %B %Y')}\n",
                ]
                for e in sorted(high_today, key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc)):
                    lines.append(f"{e.get('impact_label', '🔥 HIGH')} {e.get('country_emoji', '')} *{e.get('event', '')}*")
                    lines.append(f"   🕐 {e.get('time', '')}")
                lines.append("")
                lines.append("⚡ Pengingat akan dikirim menjelang jam rilis.")
                message = "\n".join(lines)

            # Tombol '📊 Analisis Dampak' untuk event hari ini (ketuk → analisis)
            kb = self._build_calendar_aftermath_buttons(high_today)
            if kb:
                message += "\n\n📊 *Ketuk tombol event untuk analisis dampak.*"

            kwargs_send = {"parse_mode": "Markdown"}
            if kb:
                kwargs_send["reply_markup"] = kb
            for chat_id in list(subscribers):
                try:
                    await safe_send_message(
                        application.bot,
                        chat_id=chat_id,
                        text=message,
                        **kwargs_send,
                    )
                except Exception as e:
                    logger.error(f"Gagal kirim digest event ke {chat_id}: {e}")
                    if "Forbidden" in str(e):
                        subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
        except Exception as e:
            logger.error(f"Event digest error: {e}")
    async def check_event_reminders(self, application: Application):
        """
        Kirim reminder event HIGH-IMPACT yang akan rilis dalam X jam ke depan.
        Dipanggil job scheduler secara berkala. Dedup via event_alert_notified.
        """
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            events = await self.macro.get_economic_calendar()
            now_utc = datetime.now(timezone.utc)
            lead = timedelta(hours=ECONOMIC_ALERT_LEAD_HOURS)

            notified = set(application.bot_data.get("event_alert_notified", set()))
            before_notified = set(notified)

            # Prune key yang sudah lewat agar set tidak membengkak tanpa batas
            notified = {k for k in notified if k.split("|")[-1] >= (now_utc - timedelta(days=7)).isoformat()}

            new_keys = []
            for e in events:
                if e.get("impact") != "high":
                    continue
                dt = e.get("_dt_utc")
                if not dt or dt.tzinfo is None:  # skip jika tanpa waktu yang valid
                    continue
                if now_utc < dt <= now_utc + lead:
                    key = f"{e.get('event')}|{dt.isoformat()}"
                    if key in notified:
                        continue
                    notified.add(key)
                    new_keys.append(e)

            # Persist dedup dulu agar tidak terkirim dobel walau ada error saat kirim
            # (best-effort ke Supabase — hanya jika ada perubahan).
            application.bot_data["event_alert_notified"] = notified
            if notified != before_notified:
                try:
                    await db.save_event_alert_notified_async(notified)
                except Exception as e:
                    logger.debug(f"Persist event_alert_notified gagal: {e}")

            for e in new_keys:
                # Tombol '📊 Analisis Dampak' untuk event ini
                kb = self._build_calendar_aftermath_buttons([e], max_buttons=1)
                hint = "\n\n📊 *Ketuk tombol di bawah untuk analisis dampak.*" if kb else ""
                message = (
                    f"⏰ *REMINDER EVENT EKONOMI*\n\n"
                    f"{e.get('impact_label', '🔥 HIGH')} {e.get('country_emoji', '')} *{e.get('event', '')}*\n"
                    f"🕐 {e.get('time', '')}\n\n"
                    f"⚠️ Rilis dalam ±{ECONOMIC_ALERT_LEAD_HOURS} jam — bersiap untuk volatilitas!"
                    f"{hint}"
                )
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot,
                            chat_id=chat_id,
                            text=message,
                            parse_mode="Markdown",
                            reply_markup=kb,
                        )
                    except Exception as ex:
                        logger.error(f"Gagal kirim reminder event ke {chat_id}: {ex}")
                        if "Forbidden" in str(ex):
                            # User block bot / keluar — hapus dari daftar subscriber
                            subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
        except Exception as e:
            logger.error(f"Event reminder error: {e}")
    @staticmethod
    def _aftermath_key(event: Dict) -> str:
        """Kunci dedup stabil per event (nama + waktu rilis UTC)."""
        dt = event.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc)
        return f"{event.get('event')}|{dt.isoformat()}"
    @staticmethod
    def _search_aftermath_events(events: List[Dict], query: Optional[str]) -> List[Dict]:
        """
        Cari event di kalender yang cocok dengan kata kunci (case-insensitive).
        Dipakai perintah /aftermath <event> — memilih kandidat terbaik.

        Peringkat (skor lebih tinggi = lebih dulu):
        1. +10 bila event sudah rilis (ada angka Actual) — analisis lebih bermakna
        2. +2 bila kata kunci cocok utuh di salah satu kata nama event
        3. +1 bila hanya substring biasa
        4. Tie-break: event paling baru lebih dulu

        Returns:
            List[Dict] — terurut peringkat (index 0 = kandidat terbaik).
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        ranked = []
        for e in events or []:
            name = (e.get("event") or "").lower()
            if not name or q not in name:
                continue
            words = name.split()
            if (
                name == q
                or name.startswith(q)
                or any(w == q or w.startswith(q) for w in words)
            ):
                score = 2
            else:
                score = 1
            if e.get("actual") not in (None, ""):
                score += 10
            dt = e.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc)
            ranked.append((score, dt.timestamp(), e))
        # Skor turun → yang paling baru lebih dulu pada skor sama
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [e for _, _, e in ranked]
    @staticmethod
    def _collect_aftermath_events(events: List[Dict], now_utc, lookback_hours: float) -> List[Dict]:
        """
        Pilih event HIGH-IMPACT yang sudah rilis dalam jendela lookback (jam).
        Murni & mudah di-test (tanpa I/O).

        Returns:
            List[Dict] — terurut dari paling baru.
        """
        cutoff = now_utc - timedelta(hours=lookback_hours)
        out = []
        for e in events or []:
            if e.get("impact") != "high":
                continue
            dt = e.get("_dt_utc")
            if not dt or dt.tzinfo is None:  # event tanpa waktu valid tidak bisa dinilai
                continue
            if cutoff <= dt <= now_utc:
                out.append(e)
        out.sort(
            key=lambda x: x.get("_dt_utc") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return out
    @staticmethod
    def _fmt_ev_value(v) -> str:
        """
        Format nilai event (angka/string) atau '—' bila kosong.
        Float dibulatkan ramah: 250.0 → "250" (NFP), 3.0 → "3.0" (CPI),
        2.9 → "2.9" — agar angka makro tetap terbaca presisi.
        """
        if v is None or v == "":
            return "—"
        if isinstance(v, float):
            if v == int(v):
                return str(int(v)) if abs(v) >= 10 else f"{v:.1f}"
            return f"{v:g}"
        return str(v)
    @staticmethod
    def _format_event_numbers(event: Dict) -> str:
        """Actual/Forecast/Previous dalam satu baris ringkas dengan satuan."""
        unit = event.get("unit", "")
        return (
            f"📊 *Actual:* {SchedulerJobsMixin._fmt_ev_value(event.get('actual'))}{unit}  |  "
            f"*Forecast:* {SchedulerJobsMixin._fmt_ev_value(event.get('estimate'))}{unit}  |  "
            f"*Previous:* {SchedulerJobsMixin._fmt_ev_value(event.get('prev'))}{unit}"
        )
    @staticmethod
    def _static_event_interpretation(event: Dict) -> str:
        """
        Interpretasi arah DXY berbasis aturan — fallback saat AI tidak tersedia
        (dan dasar yang selalu ditampilkan). Membandingkan Actual vs Forecast
        untuk event utama AS; event non-AS dinilai via pasangan mata uang.
        """
        actual = event.get("actual")
        forecast = event.get("estimate")
        name = (event.get("event") or "").lower()
        country = (event.get("country") or "").upper()

        def _diff(a, f):
            try:
                return float(a) - float(f)
            except (TypeError, ValueError):
                return None

        # ---- Event non-AS: dampak lewat pasangan mata uang ----
        if country and country != "US":
            if actual in (None, "") or forecast in (None, ""):
                return (
                    f"Event di luar AS ({country}) — dampak ke DXY lewat pasangan mata uang. "
                    "Nilai aktual belum tersedia untuk perbandingan."
                )
            diff = _diff(actual, forecast)
            if diff is None:
                return f"Event di luar AS ({country}) — pantau pasangan mata uang terkait."
            if diff == 0:
                return f"Data {country} sesuai ekspektasi — dampak ke DXY cenderung terbatas."
            if diff > 0:
                return (
                    f"Data {country} DI ATAS ekspektasi → mata uang {country} berpotensi "
                    "menguat → DXY berpotensi TURUN."
                )
            return (
                f"Data {country} DI BAWAH ekspektasi → mata uang {country} berpotensi "
                "melemah → DXY berpotensi NAIK."
            )

        # ---- Event AS ----
        if actual in (None, "") or forecast in (None, ""):
            return "Nilai aktual belum tersedia — bandingkan dengan ekspektasi pasar bila sudah rilis."
        diff = _diff(actual, forecast)
        if diff is None:
            return "Angka aktual tidak bisa dibandingkan dengan ekspektasi."

        # FOMC / keputusan suku bunga: arah ditentukan oleh PERUBAHAN rate
        # (Actual vs Previous), bukan vs forecast — diproses lebih dulu.
        if "fed" in name or "fomc" in name or "rate decision" in name:
            prev = event.get("prev")
            try:
                pv = float(prev) if prev not in (None, "") else None
                if pv is not None:
                    av = float(actual)
                    if av > pv:
                        return (
                            f"Actual {SchedulerJobsMixin._fmt_ev_value(actual)} vs Previous "
                            f"{SchedulerJobsMixin._fmt_ev_value(prev)} — suku bunga NAIK (hawkish) → DXY cenderung NAIK."
                        )
                    if av < pv:
                        return (
                            f"Actual {SchedulerJobsMixin._fmt_ev_value(actual)} vs Previous "
                            f"{SchedulerJobsMixin._fmt_ev_value(prev)} — suku bunga TURUN (dovish) → DXY cenderung TURUN."
                        )
                    return (
                        f"Actual {SchedulerJobsMixin._fmt_ev_value(actual)} vs Previous "
                        f"{SchedulerJobsMixin._fmt_ev_value(prev)} — suku bunga dipertahankan — arah DXY tergantung nada statement & guidance."
                    )
                return "Keputusan Fed — arah DXY tergantung guidance/statement."
            except (TypeError, ValueError):
                return "Keputusan Fed — arah DXY tergantung guidance/statement."

        # Actual PERSIS sesuai ekspektasi — data sudah "harga-in" pasar, arah
        # dampak biasanya terbatas (penting: JANGAN dianggap lebih rendah/tinggi).
        if diff == 0:
            return f"Actual sesuai ekspektasi ({forecast}). Dampak ke DXY cenderung terbatas — fokus pada data sekunder (revisi, detail komponen)."

        if diff > 0:
            surprise = "DI ATAS ekspektasi"
        else:
            surprise = "DI BAWAH ekspektasi"

        if "cpi" in name or "inflasi" in name or "ppi" in name or "harga produsen" in name:
            hint = (
                "Data inflasi lebih tinggi dari ekspektasi → Fed cenderung hawkish → DXY cenderung NAIK."
                if diff > 0 else
                "Data inflasi lebih rendah dari ekspektasi → Fed cenderung dovish → DXY cenderung TURUN."
            )
        elif "non-farm" in name or "payroll" in name or "gdp" in name or "retail" in name:
            hint = (
                "Data ekonomi lebih kuat dari ekspektasi → dolar menguat → DXY cenderung NAIK."
                if diff > 0 else
                "Data ekonomi lebih lemah dari ekspektasi → dolar melemah → DXY cenderung TURUN."
            )
        elif "unemployment" in name or "pengangguran" in name or "claims" in name:
            # Inverse: pengangguran/klaim lebih RENDAH = pasar tenaga kerja kuat
            hint = (
                "Angka pengangguran/klaim lebih rendah dari ekspektasi → pasar tenaga kerja kuat → DXY cenderung NAIK."
                if diff < 0 else
                "Angka pengangguran/klaim lebih tinggi dari ekspektasi → pasar tenaga kerja melemah → DXY cenderung TURUN."
            )
        else:
            hint = (
                "Data di atas ekspektasi cenderung menguatkan USD → DXY berpotensi NAIK."
                if diff > 0 else
                "Data di bawah ekspektasi cenderung melemahkan USD → DXY berpotensi TURUN."
            )

        return f"Actual {surprise} vs Forecast ({forecast}). {hint}"
    async def _build_aftermath_message(self, event: Dict, market_line: str, manual: bool = False) -> str:
        """Bangun pesan analisis aftermath: angka + interpretasi statis + analisis AI.

        Args:
            manual: True saat dipanggil perintah /aftermath (judul berbeda).
        """
        event_name = event.get("event", "Event Ekonomi")
        title = "🎯 *ANALISIS DAMPAK EVENT*" if manual else "🔥 *AFTERMATH EVENT EKONOMI*"
        header = (
            f"{title}\n"
            f"{event.get('country_emoji', '')} *{event_name}*\n"
            f"🕐 {event.get('time', '')}\n\n"
            f"{self._format_event_numbers(event)}\n"
            f"💱 *Kondisi Pasar:* {market_line}\n"
        )

        static = self._static_event_interpretation(event)

        ai_section = ""
        try:
            prompt = format_prompt(
                "event_aftermath",
                EVENT_NAME=event_name,
                COUNTRY=event.get("country", "US"),
                TIME=event.get("time", ""),
                IMPACT_LABEL=event.get("impact_label", "🔥 HIGH"),
                ACTUAL=self._fmt_ev_value(event.get("actual")),
                FORECAST=self._fmt_ev_value(event.get("estimate")),
                PREV=self._fmt_ev_value(event.get("prev")),
                UNIT=event.get("unit", ""),
                DXY_DATA=market_line,
            )
            ai_text = await asyncio.to_thread(
                self.ai.generate, prompt, max_tokens=700, use_cache=True
            )
            if ai_text and "error" not in ai_text.lower():
                ai_text = strip_markdown_asterisks(_strip_provider_prefix(ai_text))
                ai_section = f"\n📰 *Analisis:*\n{ai_text}\n"
        except Exception as e:
            logger.warning(f"Aftermath AI analysis failed: {e}")

        if not ai_section:
            ai_section = f"\n📰 *Interpretasi:*\n{static}\n"

        # Section prediksi bot (XAU/USD) — tampil bila event ini punya prediksi
        # tercatat (fitur /prediksi): arah prediksi + hasil benar/salah + pergerakan.
        pred_section = ""
        try:
            if getattr(self, "news_preds", None) is not None:
                await asyncio.to_thread(self.news_preds.ensure_loaded)
                record = self.news_preds.get_prediction(self._aftermath_key(event))
                if record:
                    pred_section = self._format_prediction_section(record)
        except Exception as e:
            logger.debug(f"Section prediksi aftermath gagal: {e}")

        return header + ai_section + pred_section + f"\n{DISCLAIMER}"
    async def _build_market_line(self) -> str:
        """
        Konteks pasar satu baris: DXY + Gold + EUR/USD untuk analisis aftermath.
        Data di-cache data layer (TTL pendek), jadi biaya request kecil.
        Tidak pernah raise — selalu mengembalikan string minimal.
        """
        market_line = "DXY: tidak tersedia"
        try:
            dxy = await asyncio.to_thread(
                self.market.get_yahoo_data, "DX-Y.NYB", period="2d", interval="1h", ohlcv_limit=1
            )
            if "error" not in dxy and dxy.get("current_price") is not None:
                p = dxy["current_price"]
                c = dxy.get("change_pct")
                arrow = "🟢" if c and c > 0 else "🔴" if c and c < 0 else "⚪"
                c_str = f"{c:+.2f}%" if c is not None else ""
                market_line = f"DXY: {format_price(p, 'DX-Y.NYB')} {arrow} {c_str}"
        except Exception as e:
            logger.warning(f"DXY fetch gagal: {e}")
        for sym, label in (("GC=F", "Gold"), ("EURUSD=X", "EUR/USD")):
            try:
                data = await asyncio.to_thread(
                    self.market.get_yahoo_data, sym, period="2d", interval="1h", ohlcv_limit=1
                )
                if "error" not in data and data.get("current_price") is not None:
                    market_line += f"  |  {label}: {format_price(data['current_price'], sym)}"
            except Exception:
                pass
        return market_line
    async def check_event_aftermath(self, application: Application):
        """
        Job berkala: kirim analisis dampak event high-impact yang BARU SAJA rilis.
        Dipanggil scheduler (main.py) setiap ECONOMIC_ALERT_CHECK_INTERVAL_MINUTES.
        Dedup via bot_data + Supabase (event_reports) agar tidak dobel, termasuk
        setelah restart/deploy.
        """
        if not EVENT_AFTERMATH_ENABLED:
            return
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            events = await self.macro.get_economic_calendar()
            now_utc = datetime.now(timezone.utc)

            candidates = self._collect_aftermath_events(events, now_utc, EVENT_AFTERMATH_LOOKBACK_HOURS)
            # Hanya laporkan event yang ANGKA ACTUAL-nya sudah tersedia — notifikasi
            # tanpa actual hanya placeholder & nilainya rendah. Event tanpa actual
            # akan dicek lagi di run berikutnya (dedup hanya menandai yang terkirim).
            candidates = [
                e for e in candidates if e.get("actual") not in (None, "")
            ]
            if not candidates:
                return

            # Dedup: gabung set memori + persisten (Supabase), lalu prune 7 hari
            reported = set(application.bot_data.get("event_aftermath_reported", set()))
            try:
                reported |= await db.get_reported_events_async()
            except Exception as e:
                logger.debug(f"event_reports load gagal: {e}")
            cutoff_ts = (now_utc - timedelta(days=self.EVENT_AFTERMATH_DEDUP_TTL_DAYS)).isoformat()
            reported = {k for k in reported if k.split("|")[-1] >= cutoff_ts}

            new_events = []
            for e in candidates:
                key = self._aftermath_key(e)
                if key in reported:
                    continue
                reported.add(key)
                new_events.append(e)
                # Batasi analisis per run: tiap event memanggil AI (budget sampai
                # AI_MAX_TOTAL_WAIT_SECONDS). Sisanya menunggu run berikutnya.
                if len(new_events) >= 3:
                    break
            application.bot_data["event_aftermath_reported"] = reported

            if not new_events:
                return

            # Konteks pasar SEKALI per run (DXY + Gold + EUR/USD)
            market_line = await self._build_market_line()

            for e in new_events:
                try:
                    message = await self._build_aftermath_message(e, market_line)
                except Exception as ex:
                    logger.warning(f"Aftermath message gagal untuk {e.get('event')}: {ex}")
                    message = (
                        f"🔥 *AFTERMATH EVENT EKONOMI*\n{e.get('country_emoji', '')} "
                        f"*{e.get('event', '')}*\n🕐 {e.get('time', '')}\n\n"
                        f"{self._format_event_numbers(e)}\n\n"
                        f"💱 Kondisi Pasar: {market_line}\n\n"
                        f"📰 {self._static_event_interpretation(e)}\n\n{DISCLAIMER}"
                    )
                # Tombol '📊 Analisis Dampak' — ketuk untuk lihat detail/ulangi analisis
                kb = self._build_calendar_aftermath_buttons([e], max_buttons=1)
                kwargs_send = {"parse_mode": "Markdown"}
                if kb:
                    kwargs_send["reply_markup"] = kb
                for chat_id in list(subscribers):
                    try:
                        await safe_send_message(
                            application.bot, chat_id=chat_id, text=message, **kwargs_send
                        )
                    except Exception as ex:
                        logger.error(f"Gagal kirim aftermath ke {chat_id}: {ex}")
                        if "Forbidden" in str(ex):
                            subscribers.discard(chat_id)
                # Persist dedup (best-effort — kegagalan tidak menggagalkan kirim)
                try:
                    await db.save_reported_event_async(self._aftermath_key(e))
                except Exception as ex:
                    logger.debug(f"save_reported_event gagal: {ex}")

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
        except Exception as e:
            logger.error(f"Event aftermath error: {e}")
    @staticmethod
    def _parse_ai_direction(text: Optional[str]) -> Optional[str]:
        """Ambil kata pertama 'naik'/'turun' dari output AI (awali satu kata)."""
        if not text:
            return None
        first = text.strip().split()[0].strip(".!?:;\"'()[]-–—*_#").lower()
        return first if first in ("naik", "turun") else None
    @staticmethod
    def _parse_ai_verdict(text: Optional[str]) -> Optional[str]:
        """Ambil kata pertama 'benar'/'salah'/'flat' dari output AI."""
        if not text:
            return None
        first = text.strip().split()[0].strip(".!?:;\"'()[]-–—*_#").lower()
        return first if first in ("benar", "salah", "flat") else None
    @staticmethod
    def _rule_based_gold_direction(event: Dict) -> Tuple[str, str]:
        """
        Fallback arah emas berbasis aturan (saat AI tidak tersedia/gagal).
        Mengembalikan (direction, alasan). Arah DXY diestimasi dari Forecast vs
        Previous ala _static_event_interpretation; emas umumnya berkorelasi
        terbalik dengan DXY dalam reaksi jangka pendek.
        """
        name = (event.get("event") or "").lower()
        forecast = event.get("estimate")
        prev = event.get("prev")
        try:
            fv = float(forecast) if forecast not in (None, "") else None
            pv = float(prev) if prev not in (None, "") else None
        except (TypeError, ValueError):
            fv = pv = None

        # Keputusan suku bunga: arah tidak bisa diestimasi dari angka
        if "fed" in name or "fomc" in name or "rate decision" in name:
            return (
                "naik",
                "Keputusan suku bunga Fed menentukan arah lewat nada statement & "
                "guidance — di tengah ketidakpastian emas cenderung didukung "
                "permintaan safe-haven.",
            )
        if fv is None or pv is None:
            return (
                "naik",
                "Ekspektasi pasar belum tersedia untuk perbandingan — perkiraan "
                "default: emas didukung status safe-haven.",
            )

        diff = fv - pv
        if "cpi" in name or "inflasi" in name or "ppi" in name:
            if diff > 0:
                return (
                    "turun",
                    f"Forecast inflasi {fv} di atas previous {pv} → ekspektasi inflasi "
                    "lebih tinggi → yield & USD berpotensi naik → emas cenderung turun.",
                )
            return (
                "naik",
                f"Forecast inflasi {fv} di bawah previous {pv} → tekanan inflasi mereda "
                "→ ekspektasi dovish → emas cenderung naik.",
            )
        if (
            "non-farm" in name
            or "payroll" in name
            or "gdp" in name
            or "retail" in name
            or "durable" in name
            or "manufaktur" in name
        ):
            if diff > 0:
                return (
                    "turun",
                    f"Forecast {fv} di atas previous {pv} → ekonomi diprediksi lebih "
                    "kuat → USD menguat → emas cenderung turun.",
                )
            return (
                "naik",
                f"Forecast {fv} di bawah previous {pv} → ekonomi diprediksi melambat → "
                "USD melemah → emas cenderung naik.",
            )
        if "unemployment" in name or "pengangguran" in name or "claims" in name:
            if diff < 0:
                return (
                    "turun",
                    f"Forecast pengangguran/klaim {fv} di bawah previous {pv} → pasar "
                    "tenaga kerja kuat → USD menguat → emas cenderung turun.",
                )
            return (
                "naik",
                f"Forecast pengangguran/klaim {fv} di atas previous {pv} → pasar tenaga "
                "kerja melemah → USD melemah → emas cenderung naik.",
            )
        if diff > 0:
            return (
                "turun",
                f"Forecast {fv} di atas previous {pv} → ekspektasi USD lebih kuat → emas "
                "cenderung turun.",
            )
        return (
            "naik",
            f"Forecast {fv} di bawah previous {pv} → ekspektasi USD lebih lemah → emas "
            "cenderung naik.",
        )
    @staticmethod
    def _compute_rule_result(
        predicted: str,
        pred_price: Optional[float],
        now_price: Optional[float],
        min_move_pct: float = 0.05,
    ) -> Optional[Dict]:
        """
        Hasil evaluasi berbasis aturan (dasar sebelum AI menilai).
        Returns {"result", "actual_direction", "move_pct"} atau None bila harga
        tidak tersedia (prediksi tetap pending — coba lagi run berikutnya).
        """
        if pred_price is None or now_price is None or pred_price <= 0:
            return None
        move_pct = (now_price - pred_price) / pred_price * 100.0
        if abs(move_pct) < min_move_pct:
            return {"result": "flat", "actual_direction": "flat", "move_pct": move_pct}
        if move_pct > 0:
            return {
                "result": "benar" if predicted == "naik" else "salah",
                "actual_direction": "naik",
                "move_pct": move_pct,
            }
        return {
            "result": "benar" if predicted == "turun" else "salah",
            "actual_direction": "turun",
            "move_pct": move_pct,
        }
    async def _fetch_gold_price(self) -> Optional[float]:
        """Harga emas (XAU/USD) saat ini — tidak pernah raise."""
        try:
            data = await asyncio.to_thread(
                self.market.get_yahoo_data,
                self.GOLD_PREDICTION_SYMBOL,
                period="2d",
                interval="1h",
                ohlcv_limit=1,
            )
            if "error" not in data and data.get("current_price") is not None:
                return float(data["current_price"])
        except Exception as e:
            logger.warning(f"Gold price fetch gagal: {e}")
        return None
    async def _create_news_prediction(
        self, event_key: str, event: Dict, market_line: str, gold_price: Optional[float]
    ) -> Optional[dict]:
        """Buat prediksi (AI dulu, fallback aturan) & simpan ke store."""
        direction, reasoning = self._rule_based_gold_direction(event)
        try:
            prompt = format_prompt(
                "news_prediction",
                EVENT_NAME=event.get("event", "Event Ekonomi"),
                COUNTRY=event.get("country", "US"),
                TIME=event.get("time", ""),
                FORECAST=self._fmt_ev_value(event.get("estimate")),
                PREV=self._fmt_ev_value(event.get("prev")),
                UNIT=event.get("unit", ""),
                MARKET_LINE=market_line,
                GOLD_PRICE=format_price(gold_price, "GC=F") if gold_price else "tidak tersedia",
            )
            ai_text = await asyncio.to_thread(
                self.ai.generate, prompt, max_tokens=300, use_cache=True
            )
            parsed = self._parse_ai_direction(ai_text)
            if parsed:
                direction = parsed
                lines = (ai_text or "").strip().splitlines()
                extra = "\n".join(lines[1:]).strip()
                if extra:
                    reasoning = extra
        except Exception as e:
            logger.warning(f"AI prediksi gagal untuk {event.get('event')}: {e}")

        return self.news_preds.add_prediction(
            event_key=event_key,
            event_name=event.get("event", "Event Ekonomi"),
            event_time=event.get("time", ""),
            event_dt_utc=event.get("_dt_utc"),
            country=event.get("country", ""),
            country_emoji=event.get("country_emoji", ""),
            direction=direction,
            price_at_prediction=gold_price,
            reasoning=reasoning,
            market_line=market_line,
            actual=event.get("actual"),
            forecast=event.get("estimate"),
            prev=event.get("prev"),
            unit=event.get("unit", ""),
        )
    @staticmethod
    def _format_prediction_section(record: dict) -> str:
        """Section 🎯 Prediksi Bot untuk pesan aftermath (dari record store)."""
        arrow = "📈 naik" if record.get("direction") == "naik" else "📉 turun"
        if record.get("status") == "settled" and record.get("result"):
            res = record.get("result")
            icon = {"benar": "✅", "salah": "❌", "flat": "➖"}.get(res, "➖")
            move = record.get("move_pct")
            move_str = f"{move:+.2f}%" if move is not None else "—"
            return f"\n🎯 *Prediksi Bot:* {arrow} → {icon} {res} (pergerakan {move_str})\n"
        return f"\n🎯 *Prediksi Bot:* {arrow} — ⏳ hasil belum dievaluasi\n"
    def _format_prediction_message(self, record: dict) -> str:
        arrow = "📈 naik" if record.get("direction") == "naik" else "📉 turun"
        price = record.get("price_at_prediction")
        price_str = format_price(price, "GC=F") if price else "—"
        lines = [
            "🎯 *PREDIKSI NEWS — XAU/USD*\n",
            f"{record.get('country_emoji', '')} *{record.get('event_name', 'Event Ekonomi')}*\n",
            f"🕐 {record.get('event_time', '')}\n",
            f"Prediksi emas: *{arrow}*\n",
            f"💰 Harga saat ini: {price_str}\n",
        ]
        if record.get("reasoning"):
            lines.append(f"💡 *Alasan:* {record.get('reasoning')}\n")
        lines.append(
            f"⏳ Hasil dievaluasi ±{NEWS_PREDICTION_SETTLE_MINUTES} menit setelah rilis.\n"
        )
        lines.append(DISCLAIMER)
        return "\n".join(lines)
    def _format_verdict_message(self, record: dict) -> str:
        result = record.get("result")
        if result == "benar":
            head = "✅ *PREDIKSI BENAR*"
        elif result == "salah":
            head = "❌ *PREDIKSI SALAH*"
        else:
            head = "➖ *PREDIKSI FLAT*"
        arrow = "📈 naik" if record.get("direction") == "naik" else "📉 turun"
        price_pred = record.get("price_at_prediction")
        price_now = record.get("price_after")
        move = record.get("move_pct")
        move_str = f"{move:+.2f}%" if move is not None else "—"
        p_pred = format_price(price_pred, "GC=F") if price_pred else "—"
        p_now = format_price(price_now, "GC=F") if price_now else "—"
        lines = [
            f"{head}\n",
            f"{record.get('country_emoji', '')} *{record.get('event_name', 'Event Ekonomi')}*\n",
            f"🕐 {record.get('event_time', '')}\n",
            f"Prediksi: *{arrow}*\n",
            f"💰 Harga: {p_pred} → sekarang {p_now} ({move_str})\n",
        ]
        if record.get("result_reasoning"):
            lines.append(f"📊 *Evaluasi:* {record.get('result_reasoning')}\n")
        lines.append(DISCLAIMER)
        return "\n".join(lines)
    async def check_news_predictions(self, application: Application):
        """
        Job berkala: buat & kirim prediksi arah emas untuk event high-impact yang
        akan rilis dalam NEWS_PREDICTION_LEAD_MINUTES menit. Dedup via store.
        Dikirim ke subscriber /alert.
        """
        if not NEWS_PREDICTION_ENABLED:
            return
        subscribers = self._get_alert_subscribers(application)
        if not subscribers:
            return
        before_subscribers = set(subscribers)

        try:
            await asyncio.to_thread(self.news_preds.ensure_loaded)
            events = await self.macro.get_economic_calendar()
        except Exception as e:
            logger.error(f"News prediction calendar error: {e}")
            return

        now_utc = datetime.now(timezone.utc)
        lead = timedelta(minutes=NEWS_PREDICTION_LEAD_MINUTES)
        candidates = []
        for e in events:
            if e.get("impact") != "high":
                continue
            dt = e.get("_dt_utc")
            if not dt or dt.tzinfo is None:
                continue
            if now_utc < dt <= now_utc + lead:
                key = self._aftermath_key(e)
                if self.news_preds.get_prediction(key):
                    continue
                candidates.append((key, e))
            if len(candidates) >= NEWS_PREDICTION_MAX_PER_RUN:
                break

        if not candidates:
            return

        # Konteks pasar SEKALI per run (DXY + Gold + EUR/USD) + harga emas
        market_line = await self._build_market_line()
        gold_price = await self._fetch_gold_price()

        for key, e in candidates:
            if gold_price is None:
                # Tanpa harga acuan, prediksi tidak bisa dievaluasi nanti —
                # lewati & coba lagi di run berikutnya (event masih dalam jendela).
                logger.warning(f"Lewati prediksi {e.get('event')}: harga emas tidak tersedia.")
                continue
            try:
                record = await self._create_news_prediction(key, e, market_line, gold_price)
            except Exception as ex:
                logger.warning(f"Buat prediksi gagal untuk {e.get('event')}: {ex}")
                continue
            if not record:
                continue
            # Persist dulu (best-effort) agar restart tidak membuat prediksi dobel
            try:
                await db.save_news_prediction_async(record)
            except Exception as ex:
                logger.debug(f"save_news_prediction gagal: {ex}")
            message = self._format_prediction_message(record)
            for chat_id in list(subscribers):
                try:
                    await safe_send_message(
                        application.bot, chat_id=chat_id, text=message, parse_mode="Markdown"
                    )
                except Exception as ex:
                    logger.error(f"Gagal kirim prediksi ke {chat_id}: {ex}")
                    if "Forbidden" in str(ex):
                        subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
    async def settle_news_predictions(self, application: Application):
        """
        Job berkala: evaluasi prediksi yang event-nya sudah lewat
        NEWS_PREDICTION_SETTLE_MINUTES menit. AI menilai benar/salah/flat dengan
        konteks: pergerakan harga + Actual vs Forecast + berita. Kirim hasil ke
        subscriber /alert & simpan ke store + Supabase.
        """
        if not NEWS_PREDICTION_ENABLED:
            return
        # Settlement adalah operasi data (win rate /prediksi) — tetap berjalan
        # walau tidak ada subscriber; hanya pengiriman pesan yang di-gate subscriber.
        subscribers = self._get_alert_subscribers(application)
        before_subscribers = set(subscribers)

        try:
            await asyncio.to_thread(self.news_preds.ensure_loaded)
        except Exception as e:
            logger.warning(f"News prediction load gagal: {e}")
            return

        pending = self.news_preds.get_pending(settle_minutes=NEWS_PREDICTION_SETTLE_MINUTES)
        if not pending:
            return

        gold_price = await self._fetch_gold_price()
        market_line = await self._build_market_line()

        settled_in_run = 0
        for record in pending:
            if settled_in_run >= NEWS_PREDICTION_MAX_PER_RUN:
                break  # budget AI per run — sisanya di run berikutnya
            try:
                updated = await self._evaluate_news_prediction(
                    record, gold_price, market_line
                )
            except Exception as ex:
                logger.warning(
                    f"Evaluasi prediksi gagal {record.get('event_name')}: {ex}"
                )
                continue
            if not updated or updated.get("status") != "settled":
                continue  # harga belum tersedia — coba lagi run berikutnya
            settled_in_run += 1
            # Persist hasil dulu (best-effort) agar restart tidak menilai ulang
            try:
                await db.save_news_prediction_async(updated)
            except Exception as ex:
                logger.debug(f"save_news_prediction (settle) gagal: {ex}")
            if not subscribers:
                continue
            message = self._format_verdict_message(updated)
            for chat_id in list(subscribers):
                try:
                    await safe_send_message(
                        application.bot, chat_id=chat_id, text=message, parse_mode="Markdown"
                    )
                except Exception as ex:
                    logger.error(f"Gagal kirim verdict ke {chat_id}: {ex}")
                    if "Forbidden" in str(ex):
                        subscribers.discard(chat_id)

            application.bot_data["event_alert_subscribers"] = subscribers
            if subscribers != before_subscribers:
                await self._persist_alert_subscribers(application)
    async def _evaluate_news_prediction(
        self, record: dict, gold_price: Optional[float], market_line: str
    ) -> Optional[dict]:
        """
        Evaluasi satu prediksi: aturan dulu (harga), lalu AI menilai dengan
        konteks lengkap. Mengembalikan record terselesaikan, atau None bila
        harga tidak tersedia (tetap pending).
        """
        rule = self._compute_rule_result(
            record.get("direction") or "naik",
            record.get("price_at_prediction"),
            gold_price,
            NEWS_PREDICTION_MIN_MOVE_PCT,
        )
        if rule is None:
            return None

        result = rule["result"]
        actual_direction = rule["actual_direction"]
        move_pct = rule["move_pct"]
        reasoning = (
            f"Harga emas bergerak {move_pct:+.2f}% dari "
            f"{format_price(record.get('price_at_prediction'), 'GC=F')} ke "
            f"{format_price(gold_price, 'GC=F')}. Prediksi: {record.get('direction')}."
        )

        try:
            unit = record.get("unit", "")
            numbers = (
                f"Actual: {self._fmt_ev_value(record.get('actual'))}{unit} | "
                f"Forecast: {self._fmt_ev_value(record.get('forecast'))}{unit} | "
                f"Previous: {self._fmt_ev_value(record.get('prev'))}{unit}"
            )
            news_summary = ""
            try:
                news_summary = await self.news.get_news_summary("GC=F")
            except Exception as ex:
                logger.debug(f"News summary gagal: {ex}")
            prompt = format_prompt(
                "news_prediction_verdict",
                DIRECTION=record.get("direction", "naik"),
                DIRECTION_LABEL=record.get("direction", "naik"),
                REASONING=record.get("reasoning", ""),
                PRICE_AT_PREDICTION=format_price(record.get("price_at_prediction"), "GC=F"),
                MARKET_LINE_AT_PREDICTION=record.get("market_line", "tidak tersedia"),
                PRICE_NOW=format_price(gold_price, "GC=F"),
                MOVE_PCT=f"{move_pct:+.2f}%",
                MOVE_ABS=f"{abs(move_pct):.2f}%",
                ACTUAL_VS_FORECAST=numbers,
                NEWS=(news_summary or "")[:600],
                MIN_MOVE_PCT=f"{NEWS_PREDICTION_MIN_MOVE_PCT}%",
            )
            ai_text = await asyncio.to_thread(
                self.ai.generate, prompt, max_tokens=300, use_cache=True
            )
            parsed = self._parse_ai_verdict(ai_text)
            if parsed:
                result = parsed
                lines = (ai_text or "").strip().splitlines()
                extra = "\n".join(lines[1:]).strip()
                if extra:
                    reasoning = f"{reasoning}\n{extra}"
        except Exception as e:
            logger.warning(f"AI verdict gagal: {e}")

        return self.news_preds.settle(
            event_key=record["event_key"],
            result=result,
            actual_direction=actual_direction,
            price_after=gold_price,
            move_pct=move_pct,
            reasoning=reasoning,
        )
    async def _build_prediksi_message(self, limit: int = 10) -> str:
        """
        Bangun pesan win rate & riwayat prediksi news (XAU/USD).

        Dipakai /prediksi command DAN tombol menu '🎯 Prediksi News' agar
        keduanya konsisten (satu sumber logika).
        """
        try:
            await asyncio.to_thread(self.news_preds.ensure_loaded)
        except Exception as e:
            logger.warning(f"News predictions load gagal: {e}")

        stats = self.news_preds.get_stats()
        recent = self.news_preds.get_recent(limit)

        if stats["total"] == 0:
            return (
                "🎯 *PREDIKSI NEWS — XAU/USD*\n\n"
                "Belum ada prediksi tercatat. Prediksi otomatis dibuat 5 menit "
                "sebelum event ekonomi high-impact rilis dan dikirim ke subscriber "
                "`/alert`.\n\nAktifkan notifikasi: `/alert`\nBantuan: `/prediksi help`"
            )

        wr = stats["win_rate"]
        wr_str = f"{wr:.1f}%" if wr is not None else "— (belum ada hasil)"
        lines = [
            "🎯 *WIN RATE PREDIKSI NEWS — XAU/USD*\n",
            f"📊 Total prediksi: *{stats['total']}*",
            f"✅ Benar: *{stats['benar']}*",
            f"❌ Salah: *{stats['salah']}*",
            f"➖ Flat: *{stats['flat']}*",
            f"🏆 Win rate: *{wr_str}*\n",
        ]
        if recent:
            lines.append(f"*{len(recent)} Prediksi Terakhir:*")
            icons = {"benar": "✅", "salah": "❌", "flat": "➖", "pending": "⏳"}
            for i, r in enumerate(recent, 1):
                arrow = "📈 naik" if r.get("direction") == "naik" else "📉 turun"
                res = r.get("result") or "pending"
                name = (r.get("event_name") or "")[:38]
                lines.append(
                    f"{i}. {r.get('country_emoji', '')} *{name}* — {arrow} {icons.get(res, '⏳')} {res}"
                )
            lines.append("\n`/prediksi history` — riwayat lebih panjang")

        return "\n".join(lines)
    async def prediksi_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler /prediksi — win rate & riwayat prediksi news (XAU/USD)."""
        text = update.message.text or ""
        arg = text.replace("/prediksi", "").strip().lower()

        if arg in ("help", "bantuan"):
            await safe_reply_text(update.message, self.PREDICTION_USAGE, parse_mode="Markdown")
            return

        limit = 25 if arg in ("history", "riwayat") else 10
        message = await self._build_prediksi_message(limit)
        await safe_reply_text(update.message, message, parse_mode="Markdown")
    async def aftermath_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler /aftermath <event> — analisis dampak event ekonomi secara manual.

        Mencari event di kalender (14 hari terakhir → besok), memilih kandidat
        terbaik (yang sudah rilis diutamakan, paling baru), lalu memakai mesin
        aftermath yang sama dengan notifikasi otomatis (angka + interpretasi + AI).
        """
        text = update.message.text or ""
        arg = text.replace("/aftermath", "").strip().lower()

        if not arg or arg in ("help", "bantuan"):
            await safe_reply_text(update.message, self.AFTERMATH_USAGE, parse_mode="Markdown")
            return

        # Rate limit HANYA untuk analisis (bukan pesan usage yang ringan).
        if not await self._check_command_rate_limit(update, context):
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        try:
            tz_wib = ZoneInfo(MORNING_BRIEF_TIMEZONE)
            today = datetime.now(tz_wib).date()
            from_date = (today - timedelta(days=self.AFTERMATH_SEARCH_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            to_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            events = await self.macro.get_economic_calendar(from_date=from_date, to_date=to_date)
        except Exception as e:
            logger.error(f"Aftermath calendar fetch error: {e}")
            await safe_reply_text(
                update.message,
                "❌ Gagal memuat kalender ekonomi. Silakan coba lagi nanti.",
            )
            return

        matches = self._search_aftermath_events(events, arg)
        if not matches:
            await safe_reply_text(
                update.message,
                f"❌ Tidak ada event yang cocok dengan *\"{arg}\"* dalam 14 hari terakhir.\n\n"
                f"Contoh: `/aftermath nfp`, `/aftermath cpi`, `/aftermath fomc`, "
                f"`/aftermath gdp`, `/aftermath unemployment`.",
                parse_mode="Markdown",
            )
            return

        event = matches[0]
        note = ""
        if len(matches) > 1:
            note = (
                f"ℹ️ {len(matches)} kandidat cocok — ditampilkan yang paling baru: "
                f"*{event.get('event')}*.\n\n"
            )

        message = await self._build_aftermath_for_event(event)

        await safe_reply_text(
            update.message,
            f"{note}{message}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    @staticmethod
    def _event_short_id(event: Dict) -> str:
        """ID pendek stabil per event — payload callback tombol kalender (≤64 byte)."""
        key = SchedulerJobsMixin._aftermath_key(event)
        return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    @staticmethod
    def _short_event_label(name: str) -> str:
        """Label tombol pendek untuk nama event yang panjang (mis. 'NFP', 'CPI')."""
        n = name or ""
        for kw, short in (
            ("Non-Farm Payrolls", "NFP"),
            ("Fed Funds Rate", "FOMC"),
            ("Unemployment Rate", "UNEMP"),
            ("Initial Jobless Claims", "CLAIMS"),
            ("Inflation Rate", "CPI"),
            ("CPI", "CPI"),
            ("PPI", "PPI"),
            ("GDP", "GDP"),
            ("Retail Sales", "RETAIL"),
            ("Consumer Confidence", "CONF"),
            ("Trade Balance", "TRADE"),
            ("ISM", "ISM"),
            ("PMI", "PMI"),
        ):
            if kw in n:
                return short
        # Fallback: potong kata-kata pertama (maks ±14 karakter)
        words = n.split()
        if not words:
            return "EVENT"
        label = ""
        for w in words:
            if len(label) + len(w) + 1 > 14:
                break
            label = f"{label} {w}" if label else w
        return label.upper() or "EVENT"
    def _build_calendar_aftermath_buttons(self, events: List[Dict], max_buttons: int = 15, numbered: bool = False) -> Optional[InlineKeyboardMarkup]:
        """
        Keyboard '📊 Analisis Dampak' untuk SEMUA event high-impact yang tampil
        (urutan sama dengan daftar kalender, 3 tombol per baris agar ringkas).
        None bila tidak ada event. Hanya high-impact yang diberi tombol
        (konsisten dengan matching di callback).

        Args:
            numbered: Jika True, label tombol diberi nomor urut yang sama dengan
                daftar kalender (dipakai /calendar agar mudah dipetakan).
        """
        picked = [e for e in (events or []) if e.get("impact") == "high"][:max_buttons]
        if not picked:
            return None
        rows: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        used_labels = set()
        for i, e in enumerate(picked, start=1):
            label = self._short_event_label(e.get("event", ""))
            if label in used_labels:
                label = f"{label} {len(used_labels) + 1}"  # dedupe (mis. CPI MoM vs CPI YoY)
            used_labels.add(label)
            text = f"📊 {i}·{label}" if numbered else f"📊 {label}"
            row.append(
                InlineKeyboardButton(
                    text,
                    callback_data=f"aft:{self._event_short_id(e)}",
                )
            )
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)
    async def _build_aftermath_for_event(self, event: Dict) -> str:
        """Bangun pesan analisis dampak lengkap untuk satu event (angka + pasar + AI).
        Tidak pernah raise — fallback ke interpretasi statis bila AI/gagal."""
        try:
            market_line = await self._build_market_line()
            return await self._build_aftermath_message(event, market_line, manual=True)
        except Exception as e:
            logger.warning(f"Aftermath gagal untuk {event.get('event')}: {e}")
            return (
                f"🎯 *ANALISIS DAMPAK EVENT*\n{event.get('country_emoji', '')} "
                f"*{event.get('event', '')}*\n🕐 {event.get('time', '')}\n\n"
                f"{self._format_event_numbers(event)}\n\n"
                f"📰 {self._static_event_interpretation(event)}\n\n{DISCLAIMER}"
            )
    def _add_refresh_button(self, kb: Optional[InlineKeyboardMarkup], callback: str = "calendar_refresh") -> InlineKeyboardMarkup:
        """Tambahkan baris tombol '🔁 Refresh' di bawah keyboard (kalender/overview).
        Selalu ada — agar halaman bisa dimuat ulang tanpa mengetik ulang perintah."""
        rows = list(kb.inline_keyboard) if kb else []
        rows.append([InlineKeyboardButton("🔁 Refresh", callback_data=callback)])
        return InlineKeyboardMarkup(rows)
    async def _build_calendar_reply(self, refresh: bool = False) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        """Bangun isi pesan /calendar + tombol analisis dampak & refresh
        (dipakai /calendar, tombol menu kalender, dan '🔁 Refresh')."""
        events = await self.macro.get_economic_calendar_month(refresh=refresh)
        # numbered=True: event berindeks (1., 2., ...) agar mudah dipetakan ke
        # tombol '📊 Analisis Dampak' (tombol memakai nomor yang sama).
        calendar_text = self.macro.format_calendar_text(
            events, max_events=15, only_high=True, numbered=True
        )
        message = f"{calendar_text}\n{DISCLAIMER}"
        displayed = [e for e in events if e.get("impact") == "high"][:15]
        aft_kb = self._build_calendar_aftermath_buttons(displayed, numbered=True)
        if aft_kb:
            message = f"{calendar_text}\n\n📊 *Ketuk tombol event untuk analisis dampak.*\n{DISCLAIMER}"
        return message, self._add_refresh_button(aft_kb)
    async def _handle_calendar_aftermath_button(self, query, data: str):
        """Tombol '📊 Analisis Dampak' pada pesan /calendar → kirim analisis event.
        Mencocokkan ulang via ID pendek (kalender di-cache, jadi stabil)."""
        target = data.split(":", 1)[1] if ":" in data else ""
        if not target:
            await safe_edit_message_text(query, "⚠️ Tombol tidak valid. Kirim /calendar lagi.")
            return
        try:
            # Jendela lebar agar mencakup semua sumber tombol: kalender bulan ini,
            # digest (hari ini), dan reminder (maks +7 hari / lintas bulan).
            tz_wib = ZoneInfo(MORNING_BRIEF_TIMEZONE)
            today = datetime.now(tz_wib).date()
            month_start = today.replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            from_date = min(month_start, today - timedelta(days=14)).strftime("%Y-%m-%d")
            to_date = max(month_end, today + timedelta(days=7)).strftime("%Y-%m-%d")
            events = await self.macro.get_economic_calendar(from_date=from_date, to_date=to_date)
        except Exception as e:
            logger.error(f"Aftermath button calendar fetch error: {e}")
            await safe_edit_message_text(
                query, "❌ Gagal memuat kalender ekonomi. Silakan coba lagi nanti."
            )
            return
        event = None
        for e in events or []:
            if e.get("impact") != "high":
                continue
            if self._event_short_id(e) == target:
                event = e
                break
        if event is None:
            await safe_edit_message_text(
                query,
                "⚠️ Event tidak ditemukan — kalender mungkin sudah berganti bulan. "
                "Kirim /calendar untuk daftar terbaru.",
            )
            return
        message = await self._build_aftermath_for_event(event)
        await safe_reply_text(
            query.message,
            message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    @staticmethod
    def _is_cot_prewarm_window(now_local: datetime) -> bool:
        """Jendela pre-warm COT: hari + jam terkonfigurasi (zona lokal).

        CFTC rilis Jumat 15:30 ET = Jumat malam/sabtu dini hari WIB. Default
        COT_PREWARM_DAYS=1-6 (Senin-Sabtu): Jumat/Sabtu menangkap rilis
        mingguan, hari kerja lain (termasuk Senin pagi) menjaga cache tetap
        hangat untuk /cot pertama di hari itu. Minggu dilewati (data masih
        segar dari Sabtu). Jam pintu masuk COT_PREWARM_HOUR.
        """
        iso_weekday = now_local.weekday() + 1  # Python weekday(): Mon=0..Sun=6 → ISO Mon=1..Sun=7
        if iso_weekday not in COT_PREWARM_DAYS:
            return False
        return now_local.hour >= COT_PREWARM_HOUR

    async def prewarm_cot_cache(self, application: Application, max_instruments: int = 0):
        """Job mingguan: isi cache COT SEMUA instrumen sebelum user bertanya.

        CFTC merilis laporan COT 1x/minggu (Jumat malam WIB). Job ini mendownload
        arsip tahun berjalan (sekali, di-cache memori 12 jam), mengekstrak data
        per instrumen, dan menyimpannya ke Supabase (TTL 7 hari) beserta
        interpretasi AI bila belum ada — sehingga /cot langsung instan tanpa
        menunggu download di tengah request user.

        Defensif penuh: kegagalan SATU instrumen tidak menghentikan sisanya,
        dan kegagalan total hanya di-log (tidak ada notifikasi spam — admin
        tetap bisa melihat /cot per instrumen).

        Args:
            application: Application PTB (bot_data untuk statistik run).
            max_instruments: batas instrumen per run (0 = semua).
        """
        instruments = list(COT_INSTRUMENTS)
        if max_instruments and max_instruments > 0:
            instruments = instruments[:max_instruments]
        if not instruments:
            return

        # Download arsip SEKALI (legacy + TFF) — di-cache memori 12 jam di data/cot.py.
        try:
            legacy_rows = await asyncio.to_thread(fetch_year_rows)
        except Exception as e:
            logger.warning(f"COT pre-warm: arsip legacy gagal: {e}")
            legacy_rows = []
        try:
            tff_rows = await asyncio.to_thread(fetch_tff_rows)
        except Exception as e:
            logger.warning(f"COT pre-warm: arsip TFF gagal: {e}")
            tff_rows = []
        if not legacy_rows and not tff_rows:
            # Gagal TOTAL: semua arsip CFTC tidak bisa diunduh → user /cot akan
            # tetap coba download on-demand (kemungkinan besar ikut gagal).
            # Kabari admin — kalau dibiarkan, fitur COT diam-diam mati dan baru
            # ketahuan saat user komplain. Best-effort (tanpa admin = no-op).
            # RATE-LIMIT: maks 1 notif per hari kalender — job sekarang berjalan
            # tiap pagi (Senin-Sabtu), tanpa dedup admin bisa dapat 6 notif
            # berturut-turut saat CFTC down berhari-hari.
            logger.error("COT pre-warm: semua arsip CFTC gagal diunduh — dilewati")
            today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if application.bot_data.get("cot_prewarm_notified") != today_key:
                try:
                    await notify_admins(
                        application.bot,
                        "⚠️ *COT pre-warm gagal total*\n\n"
                        "Semua arsip CFTC (legacy & TFF) tidak bisa diunduh. "
                        "`/cot` akan tetap mencoba download on-demand — kalau "
                        "situs CFTC sedang bermasalah, user bisa melihat error.\n\n"
                        "Cek koneksi/URL CFTC lalu pantau pre-warm berikutnya "
                        "(atau `/cotrefresh` untuk coba manual).",
                    )
                    application.bot_data["cot_prewarm_notified"] = today_key
                except Exception as e:
                    logger.warning(f"Notif admin COT pre-warm gagal: {e}")
            return

        ok_count = skip_count = fail_count = 0
        for config in instruments:
            cache_key = self._cot_cache_key(config)
            rows = tff_rows if config.get("report") == "tff" else legacy_rows
            if not rows:
                fail_count += 1
                continue
            try:
                data = extract_market(rows, config)
                if not data:
                    skip_count += 1  # instrumen tidak ada di laporan terbaru
                    continue

                # Pertahankan interpretasi AI lama (bila ada) — 1 panggilan AI
                # per instrumen per minggu, tidak perlu ulangi untuk data sama.
                try:
                    cached = await db.get_cot_cache_async(cache_key)
                    if cached and cached.get("data"):
                        old_ai = cached["data"].get("ai_interpretation")
                        if old_ai:
                            data["ai_interpretation"] = old_ai
                        # Data identik dengan cache → tidak perlu tulis ulang
                        if cached["data"].get("data") == cot_data_to_json(data):
                            skip_count += 1
                            continue
                except Exception as e:
                    logger.debug(f"COT pre-warm: baca cache {cache_key} gagal: {e}")

                # Interpretasi AI hanya untuk data BARU / instrumen tanpa AI
                if not data.get("ai_interpretation") and getattr(self, "ai", None) is not None:
                    try:
                        ai_text = await self._cot_ai_interpretation(data)
                        if ai_text:
                            data["ai_interpretation"] = ai_text
                    except Exception as e:
                        logger.warning(f"COT pre-warm: AI {cache_key} gagal: {e}")

                ok = await db.set_cot_cache_async(cache_key, cot_data_to_json(data))
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.warning(f"COT pre-warm: instrumen {config.get('display')} gagal: {e}")
                fail_count += 1

        logger.info(
            f"COT pre-warm selesai: {ok_count} cache diisi, {skip_count} sudah segar, "
            f"{fail_count} gagal"
        )
        application.bot_data["cot_prewarm_stats"] = {
            "ok": ok_count,
            "skipped": skip_count,
            "failed": fail_count,
            "at": datetime.now(timezone.utc).isoformat(),
        }

    async def send_scheduled_morning_brief(self, application: Application):
        """
        Kirim morning brief ke semua chat yang terdaftar.
        Dipanggil oleh scheduler setiap pagi.
        """
        logger.info("Sending scheduled morning brief...")

        # CATATAN PERSONALISASI: brief terjadwal sengaja GLOBAL (tanpa watchlist)
        # — brief per-user untuk SEMUA subscriber = N panggilan AI per pagi,
        # tidak aman untuk free tier. Personalisasi hanya di /morning (on-demand,
        # 1 user = 1 panggilan AI).
        brief = await self._generate_morning_brief()

        # Gabungkan chat_ids dari ENV dan Database
        env_chat_ids = []
        if MORNING_BRIEF_CHAT_IDS:
            env_chat_ids = [int(x.strip()) for x in MORNING_BRIEF_CHAT_IDS.split(",") if x.strip()]

        db_chat_ids = await db.get_all_subscribers_async()

        # Buat unique list
        chat_ids = list(set(env_chat_ids + db_chat_ids))

        if not chat_ids:
            logger.info("No subscribers found for morning brief.")
            return

        for chat_id in chat_ids:
            try:
                await safe_send_message(
                    application.bot,
                    chat_id=chat_id,
                    text=brief,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                logger.info(f"Morning brief sent to chat {chat_id}")
            except Exception as e:
                logger.error(f"Failed to send morning brief to {chat_id}: {e}")
