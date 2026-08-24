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

ENV PORT=5001
EXPOSE 5001

CMD ["gunicorn", "-b", "0.0.0.0:5001", "--timeout", "120", "server:app"]
