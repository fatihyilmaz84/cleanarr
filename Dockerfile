FROM python:3.12-slim

# ffmpeg provides `ffmpeg`/`ffprobe` for the rule-based remover (app/remux.py).
# mkvtoolnix provides `mkvpropedit` for the metadata normalizer
# (app/mkv_metadata.py) — it rewrites an MKV's track metadata in place
# without touching the media payload, so normalizing is near-instant
# instead of a full remux. CLI-only package, no GUI/Qt dependencies pulled in.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix \
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
