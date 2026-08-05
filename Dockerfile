# ============================================================
# Dockerfile - MarketAI Analyst Bot untuk Railway
# ============================================================
# Base image: Python 3.11 slim (~120MB)
# ============================================================

FROM python:3.11-slim

# Set working directory sesuai dengan lokasi main.py di dalam container
WORKDIR /app/bot-telegram

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PYTHONPATH=/app/bot-telegram

# Copy requirements first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy semua file project ke WORKDIR
COPY . .

# Buat directory untuk data persistence (mount volume Railway)
RUN mkdir -p /data

# Expose port untuk webhook
EXPOSE 8080

# Jalankan bot dari WORKDIR
CMD ["python", "main.py"]
