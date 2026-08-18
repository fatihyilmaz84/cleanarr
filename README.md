# Cleanarr

Scans a media library, inspects each file's audio/subtitle tracks via
`ffprobe`, and strips the ones you don't want via a lossless `ffmpeg -c copy`
remux — no re-encoding, no quality loss, container-level track removal only.

Every change goes through a review queue you approve — nothing is ever
applied automatically. Rules ship empty by default: the app does nothing
until you configure which languages to keep.

## Run it

```bash
docker compose up -d --build
```

Then open `http://<host>:8420`. First run:

1. **Settings** — add your media path(s) (must match the host path your
   Sonarr/Radarr containers already use — see `docker-compose.yml` for why),
   and optionally your Sonarr/Radarr URL + API key for title/poster lookup.
2. **Rules** — set which audio/subtitle languages to keep. Leave a list
   empty to not filter that track type at all.
3. Click **Scan Now**, then review proposed changes under **Review Queue**
   and approve the ones you want applied.

## Local development

```bash
python -m venv .venv
.venv/Scripts/activate  # or source .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt
pytest
```

Running the app locally without Docker requires `ffmpeg`/`ffprobe` on PATH
for scans/applies to actually do anything — without them, the app still
runs but every probe attempt fails gracefully with a clear error.

`scripts/cli.py` exercises the core engine (analyzer → rules → remux)
directly against real files, no server/DB involved — useful for validating
behavior against a couple of real files before trusting it against a whole
library:

```bash
python scripts/cli.py --rules scripts/example-rules.json /path/to/movie.mkv
python scripts/cli.py --rules scripts/example-rules.json --apply /path/to/movie.mkv
```
