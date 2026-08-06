"""
Package `prompts` — Single Source of Truth untuk semua template prompt bot.

Template analisis disimpan sebagai file .txt di folder ini:

    market_analysis.txt / technical_analysis.txt / macro_explanation.txt
    morning_brief.txt            → prompt user-facing (path legacy)
    director_system.txt ...      → prompt agent multi-agent (15 file)

Edit file .txt → perilaku bot berubah tanpa mengubah kode. Lihat loader.py
untuk detail pemuatan & fallback, dan jalankan `python -m prompts.loader
--list` untuk daftar semua template + preview.

Package ini lazy-load (PEP 562) agar `python -m prompts.loader` tidak
menimbulkan RuntimeWarning runpy (modul loader di-import dua kali).
"""

from typing import Any

__all__ = ["load_prompt", "format_prompt", "reload_prompts", "prompt_names"]

# Atribut yang diekspos dari prompts.loader (dimuat on-demand).
_LOADER_ATTRS = ("load_prompt", "format_prompt", "reload_prompts", "prompt_names")


def __getattr__(name: str) -> Any:
    if name in _LOADER_ATTRS or name == "loader":
        from . import loader

        if name == "loader":
            return loader
        return getattr(loader, name)
    raise AttributeError(f"module 'prompts' has no attribute '{name}'")
