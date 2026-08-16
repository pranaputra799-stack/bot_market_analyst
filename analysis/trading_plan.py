"""
Trading Plan Generator — rencana trading mingguan personal (fitur /plan).

Catatan desain: modul ini TIDAK menjalankan pipeline 7-agent AnalysisDirector
— prompt & sintesisnya terkunci untuk analisis pasar umum (tanpa titik
suntik profil user / format output khusus). Sebagai gantinya modul ini
memakai ULANG blok infrastruktur yang sama:
- AIFallbackEngine (multi-provider: OpenRouter free → Groq → Gemini → ...)
- MarketDataAggregator / MacroDataFetcher / NewsFetcher (sumber data pasar)
- compute_indicators + format_indicators_for_prompt (teknikal per pair)
- calculate_position_size (logika /risk untuk ukuran posisi)
- prompts/loader (single source of truth — prompts/trading_plan.txt)

Biaya AI: 1 panggilan per /plan per minggu per user (hasil di-cache 7 hari
oleh lapisan bot). Input profil user (modal, risk %, gaya, pair favorit, jam)
dipakai sebagai konteks prompt + perhitungan ukuran posisi.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from analysis.indicators import compute_indicators, format_indicators_for_prompt
from prompts.loader import format_prompt
from utils.risk_calculator import calculate_position_size

logger = logging.getLogger(__name__)

# Label display (dari watchlist/profile) → simbol Yahoo Finance.
# Kunci dinormalisasi: huruf besar + hanya [A-Z0-9/] (spasi & tanda baca dibuang).
_SPECIAL_LABELS = {
    "XAU/USDGOLD": "GC=F",
    "XAG/USDSILVER": "SI=F",
    "BTC/USDBITCOIN": "BTC-USD",
    "ETH/USDETHEREUM": "ETH-USD",
    "USDOLLARINDEXDXY": "DX-Y.NYB",
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOWJONES": "^DJI",
}

VALID_STYLES = ("scalping", "day_trade", "swing")


def resolve_yahoo_symbol(label: str) -> Optional[str]:
    """Label display ('EUR/USD', 'XAU/USD (Gold)') → simbol Yahoo Finance."""
    if not label:
        return None
    key = re.sub(r"[^A-Z0-9/]", "", label.strip().upper())
    if key in _SPECIAL_LABELS:
        return _SPECIAL_LABELS[key]
    m = re.match(r"^([A-Z]{3})/([A-Z]{3})$", key)
    if m:
        return f"{m.group(1)}{m.group(2)}=X"
    return None


def _fmt_number(value) -> str:
    """Format angka profil (float dari JSONB) menjadi teks rapi."""
    if value is None or value == "":
        return "—"
    try:
        f = float(value)
        if f == int(f):
            return f"{int(f):,}"
        return f"{f:g}"
    except (TypeError, ValueError):
        return str(value)


def _as_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def format_profile_line(profile: dict) -> str:
    """Ringkasan profil user satu baris (untuk header pesan /plan)."""
    balance = _as_float(profile.get("balance"))
    risk = _as_float(profile.get("risk_per_trade"))
    style = profile.get("trading_style") or ""
    hours = profile.get("trading_hours") or ""
    parts = []
    if balance is not None:
        parts.append(f"modal ${balance:,.0f}")
    if risk is not None:
        parts.append(f"risiko {risk:g}%/trade")
    if style:
        parts.append(f"gaya {style}")
    if hours:
        parts.append(f"jam {hours}")
    return " • ".join(parts) if parts else "profil belum lengkap"


async def _fetch_pairs_technical(market, pairs: List[str]) -> str:
    """Data teknikal per pair favorit (OHLCV → indikator) untuk konteks prompt.

    Gagal satu pair tidak menggagalkan seluruhnya (fallback teks). Dipakai
    sebagai data acuan AI — angka yang bisa diverifikasi bot (RSI/EMA/harga).
    """
    sections = []
    for label in pairs:
        try:
            symbol = resolve_yahoo_symbol(label)
            if not symbol:
                continue
            ohlcv = await asyncio.to_thread(
                market.get_ohlcv_history, symbol, period="3mo", interval="1d", limit=60
            )
            if not ohlcv:
                continue
            ind = compute_indicators(ohlcv)
            txt = format_indicators_for_prompt(ind)
            sections.append(f"--- {label} ({symbol}) ---\n{txt}")
        except Exception as e:
            logger.debug(f"Trading plan: teknikal {label} gagal: {e}")
    return "\n\n".join(sections) if sections else ""


def build_trading_plan_prompt(
    profile: dict,
    market_summary: str,
    macro_summary: str,
    calendar_text: str,
    news_summary: str,
    pairs_technical: str,
    date_str: str,
) -> str:
    """Prompt rencana trading — isi placeholder dari prompts/trading_plan.txt."""
    pairs = [p for p in (profile.get("favorite_pairs") or "").split(",") if p.strip()]
    return format_prompt(
        "trading_plan",
        DATE=date_str,
        PROFILE=format_profile_line(profile) or "belum diisi",
        BALANCE=_fmt_number(profile.get("balance")),
        RISK_PCT=_fmt_number(profile.get("risk_per_trade")),
        TRADING_STYLE=profile.get("trading_style") or "belum diisi",
        FAVORITE_PAIRS=", ".join(pairs) if pairs else "belum diisi",
        TRADING_HOURS=profile.get("trading_hours") or "belum diisi",
        EXPERIENCE=profile.get("experience") or "belum diisi",
        market_data=market_summary,
        macro_data=macro_summary,
        calendar_data=calendar_text,
        news_data=news_summary,
        pairs_technical=pairs_technical or "Data teknikal tidak tersedia.",
    )


def parse_plan_json(ai_text: str) -> dict:
    """Parse respons AI → dict rencana. Tahan markdown fence / teks tambahan."""
    if not ai_text:
        return {}
    try:
        # Cari blok JSON ({...}) pertama — sama seperti agent lain.
        from data.cache import parse_json_payload

        payload = parse_json_payload(ai_text)
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            return {"pairs": payload}
        return {}
    except Exception as e:
        logger.warning(f"Trading plan: parse JSON gagal: {e}")
        return {}


def _pip_size(symbol: str) -> float:
    """Ukuran pip per instrumen (sama dengan utils/risk_calculator)."""
    sym = (symbol or "").upper().replace(" ", "")
    if sym.startswith("XAU"):
        return 0.1
    if sym.startswith("XAG"):
        return 0.01
    if sym.endswith("JPY") or "/JPY" in sym:
        return 0.01
    return 0.0001


def compute_position_sizes(profile: dict, pairs: List[dict]) -> List[dict]:
    """
    Hitung ukuran posisi per pair dari entry/SL yang diberikan AI.

    Memakai calculate_position_size (logika /risk yang sama). Pair tanpa
    entry/SL valid dilewati (tanpa lot).
    """
    balance = _as_float(profile.get("balance"))
    risk_pct = _as_float(profile.get("risk_per_trade"))
    out = []
    for p in pairs:
        entry = _as_float(p.get("entry"))
        sl = _as_float(p.get("stop_loss"))
        symbol = (p.get("symbol") or "").strip().upper()
        if balance is None or risk_pct is None or entry is None or sl is None or not symbol:
            out.append({**p, "position": None})
            continue
        pip = _pip_size(symbol)
        sl_pips = abs(entry - sl) / pip
        if sl_pips <= 0:
            out.append({**p, "position": None})
            continue
        # Pair ber-quote non-USD butuh harga quote — pakai harga entry sebagai
        # aproksimasi (cukup untuk edukasi).
        result = calculate_position_size(balance, risk_pct, sl_pips, symbol, price_quote=entry)
        out.append({
            **p,
            "position": result if "error" not in result else None,
            "sl_pips": sl_pips,
        })
    return out


def _rr(p: dict) -> Optional[float]:
    entry = _as_float(p.get("entry"))
    sl = _as_float(p.get("stop_loss"))
    tp = _as_float(p.get("take_profit"))
    if entry is None or sl is None or tp is None or abs(entry - sl) < 1e-12:
        return None
    return abs(tp - entry) / abs(entry - sl)


def format_trading_plan(profile: dict, plan: dict, date_str: str) -> str:
    """Format rencana trading + ukuran posisi → teks Telegram (Markdown)."""
    style = (profile.get("trading_style") or "").upper() or "TRADING"
    lines = [
        f"📋 *RENCANA TRADING MINGGU INI — {style}*",
        f"🗓 {date_str}",
        f"👤 Profil: {format_profile_line(profile) or '—'}",
        "",
    ]
    outlook = (plan.get("market_outlook") or "").strip()
    if outlook:
        lines.append("🎯 *OUTLOOK PASAR:*")
        lines.append(outlook)
        lines.append("")

    pairs = plan.get("pairs") or []
    if not pairs:
        lines.append("❌ AI tidak menghasilkan rencana pair yang valid. Coba lagi nanti.")
        lines.append("")
        lines.append("⚠️ Edukasi — bukan saran trading.")
        return "\n".join(lines)

    for i, p in enumerate(pairs, 1):
        symbol = (p.get("symbol") or "?").upper()
        direction = (p.get("direction") or "long").lower()
        arrow = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        entry = _as_float(p.get("entry"))
        sl = _as_float(p.get("stop_loss"))
        tp = _as_float(p.get("take_profit"))
        rr = _rr(p)

        def _p(v: Optional[float]) -> str:
            return f"{v:,.4f}" if v is not None else "—"

        lines.append(f"📌 *{i}. {symbol} — {arrow}*")
        if p.get("bias_summary"):
            lines.append(f"💡 {p['bias_summary']}")
        lines.append(f"Entry: {_p(entry)} | SL: {_p(sl)} | TP: {_p(tp)}")
        if rr is not None:
            lines.append(f"⚖️ Risk/Reward: 1:{rr:.1f}")
        pos = p.get("position")
        if isinstance(pos, dict) and pos.get("lots") is not None:
            risk_amount = pos.get("risk_amount")
            risk_txt = f" (risiko ${risk_amount:,.2f})" if risk_amount is not None else ""
            lines.append(
                f"📐 *Ukuran posisi:* {pos['lots']:.2f} lot standar{risk_txt} "
                f"(= {pos['lots_mini']:.1f} mini / {pos['lots_micro']:.0f} mikro)"
            )
        elif p.get("sl_pips") is not None:
            lines.append("📐 Ukuran posisi: tidak bisa dihitung (cek entry/SL).")
        fund = (p.get("fundamental_reason") or "").strip()
        tech = (p.get("technical_reason") or "").strip()
        if fund:
            lines.append(f"🏛 Fundamental: {fund}")
        if tech:
            lines.append(f"📈 Teknikal: {tech}")
        lines.append("")

    risk_notes = (plan.get("risk_notes") or "").strip()
    if risk_notes:
        lines.append("⚠️ *RISIKO & CATATAN:*")
        lines.append(risk_notes)
        lines.append("")
    lines.append("⚠️ Edukasi — bukan saran trading. Level dari AI + data pasar, verifikasi sebelum eksekusi.")
    return "\n".join(lines)


async def generate_trading_plan(ai, market, macro, news, profile: dict) -> dict:
    """
    Orkestrasi lengkap: kumpulkan data pasar → prompt → AI → parse → ukuran
    posisi → format. Tidak pernah raise — selalu mengembalikan dict dengan
    kunci 'error' bila gagal.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
        pairs = [p for p in (profile.get("favorite_pairs") or "").split(",") if p.strip()]

        results = await asyncio.gather(
            asyncio.to_thread(market.get_market_summary),
            asyncio.to_thread(macro.get_macro_summary),
            macro.get_economic_calendar(),
            news.get_news_summary("FOREX"),
            return_exceptions=True,
        )
        market_summary, macro_summary, calendar_events, news_summary = results
        if isinstance(market_summary, Exception):
            market_summary = "📊 Data pasar tidak tersedia saat ini."
        if isinstance(macro_summary, Exception):
            macro_summary = "🏛️ Data makro tidak tersedia saat ini."
        if isinstance(calendar_events, Exception):
            calendar_events = []
        if isinstance(news_summary, Exception):
            news_summary = "📰 Berita tidak tersedia saat ini."
        try:
            calendar_text = macro.format_calendar_text(calendar_events, max_events=5)
        except Exception as e:
            logger.warning(f"Trading plan: format kalender gagal: {e}")
            calendar_text = "📅 Tidak ada event terjadwal yang tersedia."

        pairs_technical = await _fetch_pairs_technical(market, pairs)

        prompt = build_trading_plan_prompt(
            profile, market_summary, macro_summary, calendar_text,
            news_summary, pairs_technical, today,
        )
        ai_text = await ai.generate_async(prompt, use_cache=False, max_tokens=2048)
        if not ai_text:
            return {"error": "AI tidak mengembalikan respons."}

        plan = parse_plan_json(ai_text)
        if not plan.get("pairs") and not plan.get("market_outlook"):
            return {
                "error": "Respons AI tidak valid (bukan JSON rencana). Coba lagi nanti.",
                "raw": ai_text[:500],
            }

        sized = compute_position_sizes(profile, plan.get("pairs") or [])
        plan["pairs"] = sized
        return {"plan": plan, "text": format_trading_plan(profile, plan, today)}
    except Exception as e:
        logger.error(f"Trading plan gagal: {e}", exc_info=True)
        return {"error": f"Gagal membuat rencana trading: {e}"}


def validate_profile_input(parts: List[str]) -> dict:
    """
    Validasi argumen /plan setup.

    Format: /plan setup <modal> <risk%> <gaya> <pair1,pair2> [jam]
    Contoh: /plan setup 1000 2 swing XAU/USD,EUR/USD 09:00-16:00
    Returns dict dengan kunci 'error' bila input tidak valid.
    """
    if len(parts) < 4:
        return {"error": "Format: `/plan setup <modal> <risk%> <gaya> <pair1,pair2> [jam]`"}
    balance = _as_float(parts[0])
    risk = _as_float(parts[1])
    if balance is None or balance <= 0:
        return {"error": "Modal harus angka > 0 (USD)."}
    if risk is None or risk <= 0 or risk > 100:
        return {"error": "Risiko% harus angka 0 < x ≤ 100."}
    style = parts[2].strip().lower().replace(" ", "_")
    style_map = {
        "scalping": "scalping",
        "scalp": "scalping",
        "day": "day_trade",
        "day_trade": "day_trade",
        "daytrade": "day_trade",
        "swing": "swing",
    }
    style = style_map.get(style, style)
    if style not in VALID_STYLES:
        return {"error": "Gaya trading: scalping, day_trade, atau swing."}

    pairs_raw = parts[3]
    pairs = [p.strip().upper() for p in pairs_raw.split(",") if p.strip()]
    if not pairs:
        return {"error": "Minimal 1 pair favorit (pisahkan koma)."}
    # Normalisasi label: 'XAUUSD' → 'XAU/USD', sisanya dipertahankan
    normalized = []
    for p in pairs:
        if len(p) == 6 and p.isalpha():
            normalized.append(f"{p[:3]}/{p[3:]}")
        elif p in ("GOLD", "EMAS"):
            normalized.append("XAU/USD (Gold)")
        elif p in ("SILVER", "PERAK"):
            normalized.append("XAG/USD (Silver)")
        elif p in ("BTC", "BITCOIN"):
            normalized.append("BTC/USD (Bitcoin)")
        elif p in ("ETH", "ETHEREUM"):
            normalized.append("ETH/USD (Ethereum)")
        elif p in ("DXY", "DOLLAR INDEX"):
            normalized.append("US Dollar Index (DXY)")
        elif p == "SP500":
            normalized.append("S&P 500")
        else:
            normalized.append(p)

    hours = parts[4] if len(parts) > 4 else ""
    return {
        "balance": balance,
        "risk_per_trade": risk,
        "trading_style": style,
        "favorite_pairs": ",".join(normalized),
        "trading_hours": hours,
    }
