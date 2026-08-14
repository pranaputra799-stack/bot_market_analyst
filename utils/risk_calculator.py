"""Position size calculator — edukasi, tanpa AI & tanpa network.

Rumus standar risk management:
    risk_amount  = balance * risk_pct / 100
    lots         = risk_amount / (sl_pips * pip_value_per_standard_lot)

Pip value per STANDARD lot (100.000 unit) untuk pair ber-quote USD
(EUR/USD, GBP/USD, XAU/USD, ...) = USD 10 per pip.
Untuk quote non-USD (mis. USD/JPY, EUR/JPY) butuh harga quote saat ini:
    pip_value_usd = (pip_size * contract_size) / price_quote

Kontrak yang didukung:
- Forex: 100.000 unit per standard lot; pip = 0.0001 (0.01 utk quote JPY).
- XAU/USD (Gold):   100 oz per lot, pip = 0.1   → USD 10/pip per lot.
- XAG/USD (Silver): 5.000 oz per lot, pip = 0.01 → USD 50/pip per lot.

DISCLAIMER: hitungan edukasi — bukan saran trading/investasi.
"""
from typing import Dict, Optional

FOREX_LOT_UNITS = 100_000
XAU_LOT_OZ = 100
XAG_LOT_OZ = 5_000

XAU_PIP = 0.1
XAG_PIP = 0.01
FOREX_PIP = 0.0001
JPY_PIP = 0.01

# Pip value (USD) per STANDARD lot untuk instrumen ber-quote USD
USD_QUOTED_PIP_VALUE = 10.0
XAU_PIP_VALUE = 10.0   # 100 oz × 0.1
XAG_PIP_VALUE = 50.0   # 5000 oz × 0.01


def _parse_symbol(symbol: str):
    """Normalisasi 'XAU/USD', 'eurusd', 'EUR/USD' → (base, quote)."""
    sym = (symbol or "").strip().upper().replace(" ", "")
    if "/" in sym:
        base, quote = sym.split("/", 1)
    elif len(sym) == 6:
        base, quote = sym[:3], sym[3:]
    else:
        base, quote = sym, "USD"
    return base, quote


def calculate_position_size(
    balance: float,
    risk_pct: float,
    sl_pips: float,
    symbol: str = "XAU/USD",
    price_quote: Optional[float] = None,
) -> Dict:
    """Hitung ukuran posisi (lot) berdasarkan modal, risiko %, dan SL pips.

    Args:
        balance: Modal akun (USD).
        risk_pct: Risiko per trade (persen dari modal).
        sl_pips: Jarak stop-loss dalam pips.
        symbol: Instrumen (mis. 'XAU/USD', 'EUR/USD', 'USD/JPY').
        price_quote: Harga quote saat ini — WAJIB untuk pair ber-quote
            non-USD (mis. USD/JPY → harga USD/JPY). Opsional untuk
            quote USD / logam mulia.

    Returns:
        Dict hasil; berisi kunci "error" bila input tidak valid.
    """
    try:
        balance = float(balance)
        risk_pct = float(risk_pct)
        sl_pips = float(sl_pips)
    except (TypeError, ValueError):
        return {"error": "Modal, risiko%, dan SL harus berupa angka."}
    if balance <= 0 or risk_pct <= 0:
        return {"error": "Modal dan risiko% harus lebih besar dari 0."}
    if sl_pips <= 0:
        return {"error": "SL pips harus lebih besar dari 0."}
    if risk_pct > 100:
        return {"error": "Risiko% tidak boleh melebihi 100."}

    base, quote = _parse_symbol(symbol)

    if base == "XAU":
        pip = XAU_PIP
        pip_value_per_lot = XAU_PIP_VALUE
    elif base == "XAG":
        pip = XAG_PIP
        pip_value_per_lot = XAG_PIP_VALUE
    elif quote == "USD":
        pip = FOREX_PIP
        pip_value_per_lot = USD_QUOTED_PIP_VALUE
    else:
        # Quote non-USD (JPY, CHF, ...) — butuh harga quote saat ini
        pip = JPY_PIP if quote == "JPY" else FOREX_PIP
        try:
            price_quote = float(price_quote)
        except (TypeError, ValueError):
            return {
                "error": (
                    f"{symbol} ber-quote {quote} — butuh harga {quote} saat ini "
                    "(contoh: `/risk 1000 2 20 EUR/USD 155`)."
                )
            }
        if price_quote <= 0:
            return {"error": "Harga quote tidak valid."}
        pip_value_per_lot = (pip * FOREX_LOT_UNITS) / price_quote

    risk_amount = balance * risk_pct / 100.0
    lots = risk_amount / (sl_pips * pip_value_per_lot)

    return {
        "symbol": f"{base}/{quote}",
        "balance": balance,
        "risk_pct": risk_pct,
        "risk_amount": risk_amount,
        "sl_pips": sl_pips,
        "pip": pip,
        "pip_value_per_lot": pip_value_per_lot,
        "lots": lots,
        "lots_standard": lots,
        "lots_mini": lots * 10,
        "lots_micro": lots * 100,
    }


def format_risk_result(result: Dict) -> str:
    """Format hasil kalkulator menjadi teks siap kirim (Markdown)."""
    if result.get("error"):
        return f"❌ {result['error']}"
    return (
        "📐 *POSITION SIZE CALCULATOR*\n\n"
        f"*{result['symbol']}*\n\n"
        f"💰 Modal: ${result['balance']:,.0f}\n"
        f"⚠️ Risiko: {result['risk_pct']:.1f}% = ${result['risk_amount']:,.2f}\n"
        f"🛑 SL: {result['sl_pips']:g} pips (pip = {result['pip']:g})\n\n"
        f"📊 *Ukuran posisi:*\n"
        f"• {result['lots']:.2f} lot standar\n"
        f"• {result['lots_mini']:.1f} lot mini\n"
        f"• {result['lots_micro']:.0f} lot mikro\n\n"
        "⚠️ Hitungan edukasi — bukan saran trading."
    )
