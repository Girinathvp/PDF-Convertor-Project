# Production image for the PDF Bank Statement -> Excel converter.
FROM python:3.11-slim

# Tesseract OCR is a system dependency, not a Python package.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

WORKDIR /app/backend

# Render/Railway/Fly all inject PORT; default to 5000 for local docker run.
ENV PORT=5000
EXPOSE 5000

# --timeout 300: OCR-heavy pages take a while; give the worker room.
# -w 1: single worker process. Keep at 1 unless you move job state to
# Redis/a database -- the in-memory `jobs` dict is NOT shared between
# separate worker processes.
CMD gunicorn -w 1 --threads 4 --timeout 300 -b 0.0.0.0:$PORT app:app
