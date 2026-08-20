FROM python:3.12-slim

# ffmpeg provides `ffmpeg`/`ffprobe` for the rule-based remover (app/remux.py).
# mkvtoolnix provides `mkvpropedit` for the metadata normalizer
# (app/mkv_metadata.py) — it rewrites an MKV's track metadata in place
# without touching the media payload, so normalizing is near-instant
# instead of a full remux. CLI-only package, no GUI/Qt dependencies pulled in.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

# Unraid's Docker page reads these off the container to decide what to show
# for it (confirmed against a working container on the target host — the
# bracketed placeholders are Unraid's own, substituted with the host's IP
# and the port actually published). Baked into the image rather than passed
# as --label flags so any `docker run` of it gets them, however it's started:
#   managed  puts the container under Docker Manager, which is what gives it
#            the context menu at all
#   webui    adds the "WebUI" entry to that menu, opening the app
#   icon     replaces the anonymous default square with the app's own logo
# The icon is fetched from the public repo rather than served by the app, so
# it still renders while the container is stopped — which is exactly when
# the menu is most likely to be used.
LABEL net.unraid.docker.managed="dockerman" \
      net.unraid.docker.webui="http://[IP]:[PORT:8420]/" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/fatihyilmaz84/cleanarr/main/app/static/icon-512.png" \
      org.opencontainers.image.title="Cleanarr" \
      org.opencontainers.image.description="Strips unwanted audio/subtitle tracks from a media library via lossless remux, with a review queue." \
      org.opencontainers.image.source="https://github.com/fatihyilmaz84/cleanarr" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

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
