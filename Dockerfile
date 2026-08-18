FROM python:3.12-slim

# ffmpeg provides both `ffmpeg` and `ffprobe` — the only external tools this
# app depends on. Everything else (rule engine, DB, job queue) is pure Python.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV CLEANARR_DB_PATH=/config/cleanarr.db \
    PYTHONUNBUFFERED=1

EXPOSE 8420

# Single worker, intentionally: the job queue and SQLite access assume one
# process. Don't add --workers here.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8420"]
