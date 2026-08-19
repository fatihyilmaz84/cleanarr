"""Smoke tests for the server-rendered UI: pages render without template
errors, and the classic POST-redirect-GET actions actually change state.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job, JobState
from app.main import create_app
from app.settings_store import get_schedules
from tests.test_api import FULL_STREAMS, REDUCED_STREAMS, _fake_subprocess_run, _wait_for_job  # noqa: F401


def _wait_for_idle(client: TestClient, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()
        if not jobs or jobs[0]["state"] in ("done", "error"):
            return
        time.sleep(0.02)
    raise TimeoutError("no job settled in time")


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
    with TestClient(app, follow_redirects=True) as c:
        yield c


def test_empty_state_pages_render(client: TestClient):
    for path in ["/", "/review", "/queue", "/rules", "/settings", "/history", "/schedule"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "Cleanarr" in resp.text


def test_save_rules_via_form(client: TestClient):
    # audio: two dropdown selections (display names, as the <select multiple>
    # sends them) plus one hand-typed "extra" code not in the dropdown list.
    resp = client.post(
        "/rules",
        data={
            "audio_keep_languages": ["English", "Turkish"],
            "audio_keep_languages_extra": "und",
            "subtitle_keep_languages": ["English"],
            "always_keep_forced_subtitles": "on",
        },
    )
    assert resp.status_code == 200  # followed the redirect
    assert "Rules saved" in resp.text

    settings = client.get("/api/settings").json()
    audio_languages = set(settings["rules"]["audio_keep_languages"])
    assert {"eng", "tur", "und"} <= audio_languages
    assert set(settings["rules"]["subtitle_keep_languages"]) == {"eng", "en"}  # dropdown expands to ISO 639-1 alias too
    assert settings["rules"]["keep_untagged_language"] is False  # checkbox omitted -> False

    # The Rules page re-selects the dropdown options and re-populates the
    # extra-codes field from what was just saved.
    rules_page = client.get("/rules").text
    assert '<option value="English" selected>' in rules_page
    assert '<option value="Turkish" selected>' in rules_page
    assert 'value="und"' in rules_page


def test_rules_page_shows_default_commentary_and_hi_patterns_on_first_load(client: TestClient):
    rules_page = client.get("/rules").text
    assert 'name="commentary_title_patterns" value="commentary"' in rules_page
    assert 'name="hearing_impaired_title_patterns" value="sdh, hearing.impaired"' in rules_page


def test_save_commentary_and_hearing_impaired_settings_via_form(client: TestClient):
    resp = client.post(
        "/rules",
        data={
            "commentary_title_patterns": "commentary, cast chat",
            "hearing_impaired_title_patterns": "sdh",
            "drop_commentary_tracks": "on",
            "drop_hearing_impaired_tracks": "on",
        },
    )
    assert "Rules saved" in resp.text

    rules = client.get("/api/settings").json()["rules"]
    assert rules["commentary_title_patterns"] == ["commentary", "cast chat"]
    assert rules["hearing_impaired_title_patterns"] == ["sdh"]
    assert rules["drop_commentary_tracks"] is True
    assert rules["drop_hearing_impaired_tracks"] is True

    # re-populated on reload, same as the other pattern fields
    rules_page = client.get("/rules").text
    assert 'name="commentary_title_patterns" value="commentary, cast chat"' in rules_page
    assert 'name="hearing_impaired_title_patterns" value="sdh"' in rules_page


def test_save_media_paths_via_form(client: TestClient, media_dir: Path):
    resp = client.post("/settings/media-paths", data={"paths": f"{media_dir},movie\n"})
    assert resp.status_code == 200
    assert "saved" in resp.text.lower()

    settings = client.get("/api/settings").json()
    assert settings["media_paths"][0]["library_type"] == "movie"


def test_arr_api_key_preserved_when_blank(client: TestClient):
    client.post("/settings/arr", data={"radarr_url": "http://radarr:7878", "radarr_api_key": "secret123"})
    settings1 = client.get("/api/settings").json()
    assert settings1["arr"]["radarr_api_key"] == "***"  # redacted in API response

    # Re-save with URL changed but key left blank -> key should be preserved, not wiped.
    client.post("/settings/arr", data={"radarr_url": "http://radarr:7878/", "radarr_api_key": ""})
    settings2 = client.get("/api/settings").json()
    assert settings2["arr"]["radarr_api_key"] == "***"


def test_topbar_shows_progress_bar_for_running_job(client: TestClient):
    job_manager = client.app.state.job_manager
    job = Job(id="fake-job", kind="apply", state=JobState.running, progress_current=3, progress_total=10)
    job_manager._jobs[job.id] = job

    page = client.get("/").text
    assert "3/10" in page
    assert "width: 30.0%" in page


def test_full_ui_scan_review_approve_flow(client: TestClient, media_dir: Path):
    client.post("/rules", data={"audio_keep_languages": "eng", "subtitle_keep_languages": "eng"})
    client.post("/settings/media-paths", data={"paths": f"{media_dir},movie"})

    resp = client.post("/scan")
    assert resp.status_code == 200
    _wait_for_idle(client)

    review_page = client.get("/review")
    assert "DROP audio jpn" in review_page.text
    assert "Drop all" not in review_page.text  # only one droppable track — no bulk controls needed

    pending = client.get("/api/review", params={"status": "pending"}).json()
    change_id = pending[0]["id"]
    # Real submissions send every checked box (checked by default in the
    # template) — with none, nothing would be confirmed for drop.
    drop_indices = [str(p["index"]) for p in pending[0]["proposed"] if not p["keep"]]

    resp = client.post(
        f"/review/{change_id}/approve", data={"drop_index": drop_indices, "approve_submitted": "1"}
    )
    assert resp.status_code == 200

    queue_page = client.get("/queue")
    assert "DROP audio jpn" in queue_page.text  # effective plan shown, nothing applied yet
    assert client.get("/api/history").json() == []

    resp = client.post("/queue/run")
    assert resp.status_code == 200
    _wait_for_idle(client)

    history_page = client.get("/history")
    assert "Movie" in history_page.text or str(media_dir) in history_page.text

    overview_page = client.get("/")
    assert "1 file(s) tracked" in overview_page.text or "tracked" in overview_page.text


def test_partial_approve_keeps_unchecked_drop(tmp_path, monkeypatch):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "Movie.mkv").write_bytes(b"x" * 1000)

    full_streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {}, "disposition": {"default": 1}},
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "ac3",
            "channels": 6,
            "tags": {"language": "eng"},
            "disposition": {"default": 1},
        },
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "jpn"}, "disposition": {}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "tur"}, "disposition": {}},
    ]
    # Only the jpn audio track is actually removed — the tur subtitle stays
    # checked off (overridden) at approval time even though the rules flag it too.
    reduced_streams = [full_streams[0], full_streams[1], full_streams[3]]

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[0] == "ffprobe":
            target = Path(cmd[-1])
            streams = reduced_streams if target.name.startswith(".cleanarr.tmp.") else full_streams
            payload = {"format": {"duration": "3600.000000"}, "streams": streams}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"remuxed-bytes")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    app = create_app(tmp_path / "test.db")
    with TestClient(app, follow_redirects=True) as c:
        c.post("/rules", data={"audio_keep_languages": "English", "subtitle_keep_languages": "English"})
        c.post("/settings/media-paths", data={"paths": f"{media_dir},movie"})
        c.post("/scan")
        _wait_for_idle(c)

        review_page = c.get("/review").text
        assert "Drop all" in review_page and "Keep all" in review_page  # two droppable tracks -> bulk controls shown

        pending = c.get("/api/review", params={"status": "pending"}).json()
        change = pending[0]
        dropped = [p for p in change["proposed"] if not p["keep"]]
        assert {d["language"] for d in dropped} == {"jpn", "tur"}
        audio_index = next(d["index"] for d in dropped if d["type"] == "audio")

        # Only confirm the audio drop — leave the tur subtitle's checkbox off.
        c.post(
            f"/review/{change['id']}/approve", data={"drop_index": str(audio_index), "approve_submitted": "1"}
        )

        queue_page = c.get("/queue").text
        assert "DROP audio jpn" in queue_page and "DROP subtitle tur" not in queue_page  # override reflected

        c.post("/queue/run")
        _wait_for_idle(c)

        history = c.get("/api/history").json()
        assert len(history[0]["streams_removed"]) == 1
        assert history[0]["streams_removed"][0]["language"] == "jpn"

        # The surviving subtitle track was originally stream index 3, but
        # the audio-jpn track at index 2 was dropped ahead of it in the
        # remux — its real position in the remuxed file (and thus the
        # StreamRecord that must be written) is a sequential renumbering
        # among the *kept* tracks (0=video, 1=audio-eng, 2=subtitle-tur),
        # not its stale pre-remux index.
        from sqlmodel import select

        from app.models import StreamRecord

        async def _get_stream_indices():
            async with c.app.state.session_factory() as session:
                records = (await session.exec(select(StreamRecord))).all()
                return sorted((r.stream_index, r.codec_type, r.language) for r in records)

        indices = asyncio.run(_get_stream_indices())
        assert indices == [(0, "video", None), (1, "audio", "eng"), (2, "subtitle", "tur")]


def test_schedule_add_toggle_delete_via_form(client: TestClient):
    resp = client.post(
        "/schedule",
        data={
            "label": "Nightly cleanup",
            "hour": "4",
            "minute": "30",
            "days_of_week": ["0", "2", "4"],
            "auto_apply": "on",
        },
    )
    assert resp.status_code == 200
    assert "Schedule added" in resp.text
    assert "Nightly cleanup" in resp.text
    assert "04:30 on Mon, Wed, Fri" in resp.text
    assert "AUTO-APPLY" in resp.text

    async def _get_id():
        async with client.app.state.session_factory() as session:
            return (await get_schedules(session))[0].id

    schedule_id = asyncio.run(_get_id())

    disabled = client.post(f"/schedule/{schedule_id}/toggle").text
    assert "Enable" in disabled  # button now offers to re-enable -> currently disabled

    client.post(f"/schedule/{schedule_id}/toggle")  # re-enable
    deleted = client.post(f"/schedule/{schedule_id}/delete").text
    assert "No schedules yet" in deleted


def test_schedule_defaults_to_every_day_when_no_days_checked(client: TestClient):
    client.post("/schedule", data={"hour": "4", "minute": "0"})  # no days_of_week at all
    page = client.get("/schedule").text
    assert "every day" in page


def test_schedule_with_end_time_shows_window_and_apply_queued_badge(client: TestClient):
    resp = client.post(
        "/schedule",
        data={"hour": "4", "minute": "0", "end_hour": "6", "end_minute": "0", "apply_queued": "on"},
    )
    assert "04:00-06:00" in resp.text
    assert "APPLY QUEUE" in resp.text


async def _get_schedule(session_factory):
    async with session_factory() as session:
        return (await get_schedules(session))[0]


def test_schedule_zero_length_window_is_normalized_to_no_window(client: TestClient):
    # end time identical to the start time is meaningless — treated the
    # same as leaving End blank entirely.
    client.post("/schedule", data={"hour": "4", "minute": "30", "end_hour": "4", "end_minute": "30"})
    schedule = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert schedule.end_hour is None
    assert schedule.end_minute is None

    page = client.get("/schedule").text
    assert "04:30-" not in page  # no window rendered
    assert "04:30" in page


def test_schedule_incomplete_end_time_is_treated_as_no_window(client: TestClient):
    # only end_hour given, end_minute left blank — an incomplete window is
    # as good as none, not an error.
    client.post("/schedule", data={"hour": "4", "minute": "0", "end_hour": "6"})
    schedule = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert schedule.end_hour is None
    assert schedule.end_minute is None


def test_schedule_window_spanning_midnight_saved_and_displayed_correctly(client: TestClient):
    resp = client.post("/schedule", data={"hour": "23", "minute": "0", "end_hour": "2", "end_minute": "0"})
    assert "23:00-02:00" in resp.text

    schedule = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert schedule.hour == 23
    assert schedule.end_hour == 2


def test_queue_run_and_remove(client: TestClient, media_dir: Path):
    client.post("/rules", data={"audio_keep_languages": "eng", "subtitle_keep_languages": "eng"})
    client.post("/settings/media-paths", data={"paths": f"{media_dir},movie"})
    client.post("/scan")
    _wait_for_idle(client)

    assert "Nothing queued" in client.get("/queue").text
    empty_run = client.post("/queue/run")
    assert "Queue is empty" in empty_run.text
    assert client.get("/api/history").json() == []

    pending = client.get("/api/review", params={"status": "pending"}).json()
    change_id = pending[0]["id"]
    drop_indices = [str(p["index"]) for p in pending[0]["proposed"] if not p["keep"]]
    client.post(f"/review/{change_id}/approve", data={"drop_index": drop_indices, "approve_submitted": "1"})

    # queued, not yet applied
    assert client.get("/api/review", params={"status": "pending"}).json() == []
    queue_page = client.get("/queue").text
    assert "Run Queue (1)" in queue_page
    assert client.get("/api/overview").json()["queued_count"] == 1
    assert client.get("/api/history").json() == []

    # remove it -> back to pending, nothing applied
    client.post(f"/queue/{change_id}/remove")
    assert "Nothing queued" in client.get("/queue").text
    assert len(client.get("/api/review", params={"status": "pending"}).json()) == 1

    # re-queue and actually run it this time
    client.post(f"/review/{change_id}/approve", data={"drop_index": drop_indices, "approve_submitted": "1"})
    resp = client.post("/queue/run")
    assert "Running 1 queued change" in resp.text
    _wait_for_idle(client)

    assert len(client.get("/api/history").json()) == 1
    assert "Nothing queued" in client.get("/queue").text
