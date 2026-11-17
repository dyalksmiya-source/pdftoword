FROM python:3.11-slim

# System dependencies: Tesseract OCR engine + language packs
# (eng = English, fra = French, ara = Arabic — remove/add as needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fra \
    tesseract-ocr-ara \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime; 8080 matches the public domain target port.
# 2 workers x 1 thread keeps OCR memory under the 2 GB limit.
CMD gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 1 --timeout 300 --graceful-timeout 30 server:app
