# ============================================================
# Dockerfile - MarketAI Analyst Bot
# Berlaku untuk JustRunMy (git push / zip upload / docker push),
# Railway, Render, dan platform Docker lain.
# ============================================================
# Base image: Python 3.11 slim (~120MB)
# ============================================================

FROM python:3.11-slim

# Set working directory sesuai dengan lokasi main.py di dalam container
WORKDIR /app/bot-telegram

# Environment variables
# Batasi glibc malloc arena — hemat RSS 10-30MB untuk proses Python
# multithread (menghindari OOM-restart di container memory kecil).
# Catatan: komentar TIDAK boleh berada di dalam instruksi ENV multi-line.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PYTHONPATH=/app/bot-telegram \
    MALLOC_ARENA_MAX=2

# Copy requirements first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy semua file project ke WORKDIR
COPY . .

# Buat directory untuk data persistence (mount volume)
RUN mkdir -p /data

# Expose port webhook (default PORT=8080; mapping port di panel JustRunMy
# harus mengarah ke port ini — atau set env PORT sesuai mapping).
EXPOSE 8080

# Healthcheck: cek endpoint /health (utils/health_server.py, daemon thread,
# bind HEALTH_BIND:HEALTH_PORT default 127.0.0.1:8090) — berjalan DI DALAM
# container jadi bind localhost bisa diakses langsung. Bila endpoint
# dinonaktifkan (HEALTH_ENDPOINT_ENABLED=false), fallback ke cek proses
# utama (main.py) masih hidup. Exit 0 = sehat, 1 = tidak sehat → platform
# (JustRunMy/Railway) bisa otomatis restart container yang crash.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=4 \
  CMD ["python", "utils/healthcheck.py"]

# Jalankan bot dari WORKDIR.
# Mode dipilih otomatis: BOT_RUN_MODE=auto → webhook bila WEBHOOK_URL terisi
# (atau platform cloud terdeteksi), selain itu polling (jalan tanpa port
# publik — rekomendasi JustRunMy tanpa setup HTTPS port).
CMD ["python", "main.py"]
