"""
Konfigurasi detail untuk setiap AI provider.
Digunakan oleh AIFallbackEngine untuk melakukan request ke API.
"""

PROVIDER_CONFIGS = {
    "groq": {
        "name": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        # Fallback 1 setelah OpenRouter (banyak model gratis di tier free).
        # mixtral-8x7b-32768 & llama-3.1-70b-versatile sudah discontinued (400 decommissioned).
        # llama-3.3-70b-versatile juga dijadwalkan pensiun 2026-08-16, jadi model utama
        # dipindah ke openai/gpt-oss-120b (model produksi terbaru Groq).
        "model": "openai/gpt-oss-120b",
        "fallback_models": ["openai/gpt-oss-20b", "llama-3.3-70b-versatile"],
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        },
        "payload_template": "openai",  # OpenAI-compatible format
        "rate_limit": 30,  # requests per minute
    },
    "gemini": {
        "name": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/",
        "model": "gemini-2.0-flash",
        # gemini-2.5-flash mengembalikan 404 untuk user baru; hanya gunakan model
        # 2.0 yang terverifikasi (429 = valid, hanya rate limit).
        "fallback_models": ["gemini-2.0-flash-lite"],
        "headers": lambda key: {"Content-Type": "application/json"},
        "payload_template": "gemini",
        "rate_limit": 60,
    },
    "openrouter": {
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        # PRIMARY provider — HANYA model GRATIS (:free / $0). OpenRouter punya
        # BANYAK model gratis. auto_discover_free = True membuat engine otomatis
        # menarik daftar model free terkini dari API OpenRouter sehingga bot
        # selalu memakai model free terbaru dan biaya AI tetap $0.
        # Model utama & fallback di bawah ini semua ber-suffix :free.
        # CATATAN: beberapa model :free bisa diblokir guardrail privacy akun
        # kecuali data policy diubah di https://openrouter.ai/settings/privacy
        # (set ke "Allow all" agar semua model free bisa dipakai).
        "model": "inclusionai/ling-3.0-flash:free",
        "fallback_models": [
            "openrouter/free",                          # auto-router: pilih model free terbaik
            "google/gemma-4-31b-it:free",               # reasoning kuat, vision (262K ctx)
            "google/gemma-4-26b-a4b-it:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",   # 1M context, bagus utk analisis panjang
            "nvidia/nemotron-3-super-120b-a12b:free",
            "openai/gpt-oss-20b:free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "nvidia/nemotron-3-nano-30b-a3b:free",
        ],
        "auto_discover_free": True,  # tarik daftar free model terbaru dari API
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/market-ai-bot",
            "X-Title": "Market AI Bot"
        },
        "payload_template": "openai",
        "rate_limit": 20,
    },
    "cerebras": {
        "name": "Cerebras",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        # Model lama (llama-3.1-70b / llama-3.1-8b) sudah tidak ada di Cerebras (404).
        # ID Cerebras tidak pakai strip (llama3.1-8b), tapi model tersebut sudah deprecated.
        # Model produksi saat ini: gpt-oss-120b, preview: gemma-4-31b.
        "model": "gpt-oss-120b",
        "fallback_models": ["gemma-4-31b"],
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        },
        "payload_template": "openai",
        "rate_limit": 30,
    },
    "mistral": {
        "name": "Mistral AI",
        "url": "https://api.mistral.ai/v1/chat/completions",
        # mistral-tiny / open-mistral-7b sudah discontinued di Mistral API.
        # Gunakan alias "-latest" yang masih aktif.
        "model": "mistral-small-latest",
        "fallback_models": ["mistral-large-latest", "mistral-medium-latest"],
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        },
        "payload_template": "openai",
        "rate_limit": 10,
    }
}

# Simbol Yahoo Finance untuk pair forex & gold
YAHOO_SYMBOLS = {
    "eur/usd": "EURUSD=X",
    "gbp/usd": "GBPUSD=X",
    "usd/jpy": "USDJPY=X",
    "usd/chf": "USDCHF=X",
    "aud/usd": "AUDUSD=X",
    "nzd/usd": "NZDUSD=X",
    "usd/cad": "USDCAD=X",
    "xau/usd": "GC=F",        # Gold Futures
    "xau/usd spot": "XAUUSD=X",
    "xag/usd": "SI=F",        # Silver Futures
    "usd/idr": "USDIDR=X",
    "eur/idr": "EURIDR=X",
    "gbp/idr": "GBPIDR=X",
    "btc/usd": "BTC-USD",
    "eth/usd": "ETH-USD",
    "dxy": "DX-Y.NYB",        # US Dollar Index
    "s&p 500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow jones": "^DJI",
    "vix": "^VIX",
    "nikkei": "^N225",
    "hangseng": "^HSI",
}

# Indikator makro FRED
FRED_INDICATORS = {
    "nfp": "PAYEMS",              # Non-Farm Payrolls
    "cpi": "CPIAUCSL",           # CPI All Urban Consumers
    "core cpi": "CPILFESL",      # Core CPI
    "fed rate": "FEDFUNDS",      # Federal Funds Rate
    "unemployment": "UNRATE",    # Unemployment Rate
    "gdp": "GDP",                # Gross Domestic Product
    "initial claims": "ICSA",    # Initial Jobless Claims
    "ppi": "PPIACO",             # Producer Price Index
    "retail sales": "RSXFS",     # Retail Sales
    "industrial production": "INDPRO",
    "consumer confidence": "UMCSENT",  # UofM Consumer Sentiment
    "housing starts": "HOUST",
    "trade balance": "BOPGSTB",
    "wages": "CES0500000003",    # Average Hourly Earnings
    "m2 money supply": "M2SL",
    "10y treasury": "DGS10",
    "2y treasury": "DGS2",
}
