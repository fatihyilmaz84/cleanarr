"""End-to-end test of the API through a full scan -> review -> approve ->
apply cycle, with ffprobe/ffmpeg subprocess calls faked (no real ffmpeg
needed to validate the wiring). Real ffmpeg validation happens via the CLI
harness / on deployment, per the plan.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

FULL_STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {}, "disposition": {"default": 1}},
    {
        "index": 1,
        "codec_type": "audio",
        "codec_name": "ac3",
        "channels": 6,
        "tags": {"language": "eng"},
        "disposition": {"default": 1},
    },
    {
        "index": 2,
        "codec_type": "audio",
        "codec_name": "aac",
        "channels": 2,
        "tags": {"language": "jpn", "title": "Japanese"},
        "disposition": {},
    },
    {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}, "disposition": {}},
]

# What the file looks like *after* the jpn audio track is stripped.
REDUCED_STREAMS = [FULL_STREAMS[0], FULL_STREAMS[1], FULL_STREAMS[3]]


def _fake_subprocess_run(cmd, capture_output=True, text=True, timeout=None):
    if cmd[0] == "ffprobe":
        target = Path(cmd[-1])
        streams = REDUCED_STREAMS if target.name.startswith(".cleanarr.tmp.") else FULL_STREAMS
        payload = {"format": {"duration": "3600.000000"}, "streams": streams}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
    if cmd[0] == "ffmpeg":
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"remuxed-bytes")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    raise AssertionError(f"unexpected command: {cmd}")


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise TimeoutError(f"job {job_id} did not finish in time")


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    (d / "Movie (2020).mkv").write_bytes(b"x" * 1000)
    return d


@pytest.fixture
def client(tmp_path, media_dir, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as c:
        c.put("/api/settings/rules", json={"audio_keep_languages": ["eng"], "subtitle_keep_languages": ["eng"]})
        c.put("/api/settings/media-paths", json=[{"path": str(media_dir), "library_type": "movie"}])
        yield c


def test_health(client: TestClient):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_settings_roundtrip(client: TestClient):
    settings = client.get("/api/settings").json()
    assert settings["rules"]["audio_keep_languages"] == ["eng"]
    assert settings["media_paths"][0]["library_type"] == "movie"
    assert settings["arr"]["radarr_api_key"] is None


def test_full_scan_review_approve_apply_cycle(client: TestClient, media_dir: Path):
    scan_job_id = client.post("/api/scan").json()["job_id"]
    scan_job = _wait_for_job(client, scan_job_id)
    assert scan_job["state"] == "done", scan_job
    assert scan_job["result"]["files_scanned"] == 1
    assert scan_job["result"]["files_with_pending_changes"] == 1
    assert scan_job["result"]["errors"] == []
    assert scan_job["progress_total"] == 1
    assert scan_job["progress_current"] == 1

    pending = client.get("/api/review", params={"status": "pending"}).json()
    assert len(pending) == 1
    change = pending[0]
    assert change["library_type"] == "movie"
    dropped = [p for p in change["proposed"] if not p["keep"]]
    assert len(dropped) == 1
    assert dropped[0]["language"] == "jpn"

    overview_before = client.get("/api/overview").json()
    assert overview_before["pending_review_count"] == 1
    assert overview_before["total_applied_count"] == 0

    approve_job_id = client.post(f"/api/review/{change['id']}/approve").json()["job_id"]
    approve_job = _wait_for_job(client, approve_job_id)
    assert approve_job["state"] == "done", approve_job
    assert approve_job["result"]["results"][0]["success"] is True
    assert approve_job["progress_total"] == 1
    assert approve_job["progress_current"] == 1

    assert client.get("/api/review", params={"status": "pending"}).json() == []

    history = client.get("/api/history").json()
    assert len(history) == 1
    assert history[0]["bytes_before"] == 1000
    assert history[0]["bytes_after"] == len(b"remuxed-bytes")
    assert history[0]["bytes_reclaimed"] == 1000 - len(b"remuxed-bytes")
    assert history[0]["streams_removed"][0]["language"] == "jpn"

    overview_after = client.get("/api/overview").json()
    assert overview_after["pending_review_count"] == 0
    assert overview_after["total_applied_count"] == 1
    assert overview_after["total_bytes_reclaimed"] == 1000 - len(b"remuxed-bytes")

    # the actual file on disk was atomically replaced
    movie_path = media_dir / "Movie (2020).mkv"
    assert movie_path.read_bytes() == b"remuxed-bytes"


def test_skip_change(client: TestClient):
    scan_job_id = client.post("/api/scan").json()["job_id"]
    _wait_for_job(client, scan_job_id)
    change_id = client.get("/api/review", params={"status": "pending"}).json()[0]["id"]

    resp = client.post(f"/api/review/{change_id}/skip")
    assert resp.json() == {"status": "skipped"}
    assert client.get("/api/review", params={"status": "pending"}).json() == []
    assert len(client.get("/api/review", params={"status": "skipped"}).json()) == 1


def test_approve_unknown_change_returns_404(client: TestClient):
    resp = client.post("/api/review/999999/approve")
    assert resp.status_code == 404


def test_scan_with_no_media_paths_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)
    app = create_app(tmp_path / "empty.db")
    with TestClient(app) as c:
        job_id = c.post("/api/scan").json()["job_id"]
        job = _wait_for_job(c, job_id)
        assert job["state"] == "done"
        assert job["message"] == "no media paths configured"
