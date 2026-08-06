"""
Package `prompts` — Single Source of Truth untuk semua template prompt bot.

Template analisis disimpan sebagai file .txt di folder ini:

    market_analysis.txt      → analisis pasar / teknikal (path legacy)
    technical_analysis.txt   → analisis korelasi antar instrumen
    macro_explanation.txt    → penjelasan data makroekonomi
    morning_brief.txt        → morning brief harian

Edit file .txt → perilaku bot berubah tanpa mengubah kode. Lihat loader.py
untuk detail pemuatan & fallback.
"""

from .loader import (  # noqa: F401  (import relatif lebih aman)
    format_prompt,
    load_prompt,
    prompt_names,
    reload_prompts,
)

__all__ = ["load_prompt", "format_prompt", "reload_prompts", "prompt_names"]
