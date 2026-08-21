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
    assert "30.0%" in page
    assert "width: 30.0%" in page
    # Live progress is JS-polled (fetch('/api/status')) instead of a full
    # page teardown/rebuild every 2s — see app/templates/base.html.
    assert "<meta http-equiv=\"refresh\"" not in page
    assert "fetch(\"/api/status\"" in page


def test_an_idle_page_still_polls_so_a_scheduled_job_shows_up(client: TestClient):
    """Schedules fire at 03:00 with nobody watching. A page that only polled
    when it happened to be rendered mid-job showed a stale "Idle" for as long
    as it was left open, and never displayed the run at all.
    """
    page = client.get("/").text
    assert "fetch(\"/api/status\"" in page
    assert "<meta http-equiv=\"refresh\"" not in page


def test_the_progress_bar_is_indeterminate_before_a_total_is_known(client: TestClient):
    # A scan walking the directory tree has no total yet. Reporting percent
    # as null lets the bar sweep instead of sitting at 0% looking stalled.
    job_manager = client.app.state.job_manager
    job = Job(id="counting", kind="scan", state=JobState.running)
    job_manager._jobs[job.id] = job

    assert client.get("/api/status").json()["job"]["percent"] is None
    assert "progress-indeterminate" in client.get("/").text  # the CSS is present to switch to


def test_topbar_shows_the_phase_of_a_multi_stage_job(client: TestClient):
    # A scheduled run scans and then applies, resetting the counter in
    # between; without a phase the bar just silently restarts from zero.
    job_manager = client.app.state.job_manager
    job = Job(id="applying", kind="scan", state=JobState.running, progress_current=2, progress_total=8)
    job.phase = "Removing tracks"
    job_manager._jobs[job.id] = job

    assert "Removing tracks" in client.get("/").text
    assert client.get("/api/status").json()["job"]["phase"] == "Removing tracks"


def test_status_surfaces_the_last_failed_job(client: TestClient):
    # current() only reports active jobs, so a failure used to vanish and
    # leave the UI showing a cheerful "Idle".
    import datetime as _dt

    job_manager = client.app.state.job_manager
    job = Job(id="boom", kind="scan", state=JobState.error, message="ffprobe not found")
    job.finished_at = _dt.datetime.now(_dt.timezone.utc)
    job_manager._jobs[job.id] = job

    err = client.get("/api/status").json()["last_error"]
    assert err["kind"] == "scan"
    assert err["message"] == "ffprobe not found"
    assert "job-error" in client.get("/").text  # the banner it renders into


def test_status_reports_a_job_queued_behind_the_running_one(client: TestClient):
    import datetime as _dt

    job_manager = client.app.state.job_manager
    now = _dt.datetime.now(_dt.timezone.utc)
    running = Job(id="clean", kind="scan", state=JobState.running, created_at=now)
    queued = Job(id="norm", kind="normalize_scan", state=JobState.queued, created_at=now + _dt.timedelta(seconds=1))
    job_manager._jobs[running.id] = running
    job_manager._jobs[queued.id] = queued

    status = client.get("/api/status").json()
    assert status["job"]["id"] == "clean"  # the one actually running, not the newer queued one
    assert status["job"]["queued_behind"] == 1


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
            "run_clean": "on",
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
    client.post("/schedule", data={"hour": "4", "minute": "0", "run_clean": "on"})  # no days_of_week at all
    page = client.get("/schedule").text
    assert "every day" in page


def test_schedule_with_end_time_shows_window_and_apply_queued_badge(client: TestClient):
    resp = client.post(
        "/schedule",
        data={"hour": "4", "minute": "0", "end_hour": "6", "end_minute": "0", "run_clean": "on", "apply_queued": "on"},
    )
    assert "04:00-06:00" in resp.text
    assert "APPLY QUEUE" in resp.text


async def _get_schedule(session_factory):
    async with session_factory() as session:
        return (await get_schedules(session))[0]


def test_schedule_zero_length_window_is_normalized_to_no_window(client: TestClient):
    # end time identical to the start time is meaningless — treated the
    # same as leaving End blank entirely.
    client.post("/schedule", data={"hour": "4", "minute": "30", "end_hour": "4", "end_minute": "30", "run_clean": "on"})
    schedule = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert schedule.end_hour is None
    assert schedule.end_minute is None

    page = client.get("/schedule").text
    assert "04:30-" not in page  # no window rendered
    assert "04:30" in page


def test_schedule_incomplete_end_time_is_treated_as_no_window(client: TestClient):
    # only end_hour given, end_minute left blank — an incomplete window is
    # as good as none, not an error.
    client.post("/schedule", data={"hour": "4", "minute": "0", "end_hour": "6", "run_clean": "on"})
    schedule = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert schedule.end_hour is None
    assert schedule.end_minute is None


def test_schedule_window_spanning_midnight_saved_and_displayed_correctly(client: TestClient):
    resp = client.post("/schedule", data={"hour": "23", "minute": "0", "end_hour": "2", "end_minute": "0", "run_clean": "on"})
    assert "23:00-02:00" in resp.text

    schedule = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert schedule.hour == 23
    assert schedule.end_hour == 2


def _add_schedule(client: TestClient, **overrides) -> str:
    data = {"hour": "4", "minute": "0", "run_clean": "on"}
    data.update(overrides)
    client.post("/schedule", data=data)
    return asyncio.run(_get_schedule(client.app.state.session_factory)).id


def test_schedule_edit_form_is_prefilled_with_the_saved_schedule(client: TestClient):
    schedule_id = _add_schedule(
        client,
        label="Nightly cleanup",
        hour="23",
        minute="15",
        end_hour="2",
        end_minute="30",
        days_of_week=["0", "2"],
        auto_apply="on",
        run_normalize="on",
    )

    page = client.get(f"/schedule?edit={schedule_id}").text
    assert "Editing: Nightly cleanup" in page
    assert "Save Changes" in page
    assert f'action="/schedule?edit={schedule_id}"' in page
    assert 'name="hour" min="0" max="23" value="23"' in page
    assert 'name="minute" min="0" max="59" value="15"' in page
    assert 'name="end_hour" min="0" max="23" value="2"' in page
    assert 'name="end_minute" min="0" max="59" value="30"' in page
    # Mon/Wed checked, the rest not; the opt-ins reflect what was saved.
    assert page.count('name="days_of_week"') == 7
    assert page.count('name="days_of_week" value="0" checked') == 1
    assert page.count('name="days_of_week" value="1" checked') == 0
    assert 'name="auto_apply" checked' in page
    assert 'name="run_normalize" checked' in page
    assert 'name="apply_queued" checked' not in page


def test_schedule_edit_updates_in_place_keeping_id_and_enabled_state(client: TestClient):
    schedule_id = _add_schedule(client, label="Nightly", auto_apply="on")
    client.post(f"/schedule/{schedule_id}/toggle")  # disable it before editing

    resp = client.post(
        f"/schedule?edit={schedule_id}",
        data={"label": "Weekly deep clean", "hour": "5", "minute": "45", "days_of_week": ["6"], "run_clean": "on"},
    )
    assert "Schedule updated" in resp.text

    async def _all():
        async with client.app.state.session_factory() as session:
            return await get_schedules(session)

    schedules = asyncio.run(_all())
    assert len(schedules) == 1  # edited, not appended as a second one
    saved = schedules[0]
    assert saved.id == schedule_id  # the id the toggle/delete buttons address
    assert saved.enabled is False  # editing is not a way to silently re-enable
    assert (saved.label, saved.hour, saved.minute, saved.days_of_week) == ("Weekly deep clean", 5, 45, [6])
    assert saved.auto_apply is False  # an unchecked box clears it, as on add


def test_schedule_edit_can_clear_the_end_window(client: TestClient):
    schedule_id = _add_schedule(client, end_hour="6", end_minute="0")
    client.post(f"/schedule?edit={schedule_id}", data={"hour": "4", "minute": "0", "run_clean": "on"})

    saved = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert (saved.end_hour, saved.end_minute) == (None, None)


def test_schedule_edit_of_a_deleted_schedule_saves_nothing(client: TestClient):
    # Deleted in another tab between opening the edit form and submitting it —
    # the edits must not come back as a brand-new schedule.
    schedule_id = _add_schedule(client)
    client.post(f"/schedule/{schedule_id}/delete")

    resp = client.post(f"/schedule?edit={schedule_id}", data={"hour": "9", "minute": "0", "run_clean": "on"})
    assert "no longer exists" in resp.text
    assert "No schedules yet" in resp.text


def test_schedule_edit_rejected_by_validation_returns_to_the_edit_form(client: TestClient):
    schedule_id = _add_schedule(client, label="Nightly")
    # neither Clean nor Normalize checked — rejected, and the user lands back
    # on the edit form rather than on a blank add form.
    resp = client.post(f"/schedule?edit={schedule_id}", data={"hour": "4", "minute": "0"})
    assert "Pick at least one of Clean or Normalize" in resp.text
    assert "Editing: Nightly" in resp.text

    saved = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert saved.run_clean is True  # untouched


def test_schedule_edit_link_for_an_unknown_id_falls_back_to_the_add_form(client: TestClient):
    _add_schedule(client)
    page = client.get("/schedule?edit=nope").text
    assert "Add a schedule" in page
    assert "Add Schedule" in page


def test_redirect_message_does_not_clobber_an_existing_query_param(client: TestClient):
    # _redirect appends msg= to a path that may already carry a query string
    # (the ?edit=/?preset= edit forms) — with a "?" instead of an "&" the
    # message would be swallowed into the preceding param's value.
    schedule_id = _add_schedule(client, label="Nightly")
    resp = client.post(f"/schedule?edit={schedule_id}", data={"hour": "4", "minute": "0"})
    assert str(resp.url).endswith(f"/schedule?edit={schedule_id}&msg=Pick%20at%20least%20one%20of%20Clean%20or%20Normalize%20%E2%80%94%20nothing%20saved.")


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


# --- Sonarr/Radarr connection check ------------------------------------


def _arr_handler(radarr_ok=True, sonarr_ok=True):
    import httpx

    def handler(request):
        ok = radarr_ok if "7878" in str(request.url) else sonarr_ok
        if ok:
            return httpx.Response(200, json={"version": "5.14.0", "appName": "Radarr"})
        return httpx.Response(401, json={})

    return handler


def _patch_arr(monkeypatch, handler):
    """Point the app's ArrClient factory at a mock transport."""
    import httpx

    from app import web as web_mod
    from app.arr_client import ArrClient

    def build(arr_config):
        return ArrClient(
            radarr_url=arr_config.radarr_url,
            radarr_api_key=arr_config.radarr_api_key,
            sonarr_url=arr_config.sonarr_url,
            sonarr_api_key=arr_config.sonarr_api_key,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(web_mod, "build_arr_client", build)


def test_settings_shows_not_checked_before_any_connection_test(client: TestClient):
    # "never tested" must not look like "broken" — no red cross on a fresh
    # install that simply hasn't been configured yet.
    page = client.get("/settings").text
    assert "Not checked yet" in page
    assert "Connected" not in page


def test_saving_the_connection_tests_it_and_shows_a_checkmark(client: TestClient, monkeypatch):
    _patch_arr(monkeypatch, _arr_handler())
    resp = client.post(
        "/settings/arr",
        data={
            "radarr_url": "http://radarr:7878",
            "radarr_api_key": "k1",
            "sonarr_url": "http://sonarr:8989",
            "sonarr_api_key": "k2",
        },
    )
    assert "Radarr: OK" in resp.text
    assert "Sonarr: OK" in resp.text

    # And the checkmark persists on a later plain page load, without the
    # page itself having to re-test.
    page = client.get("/settings").text
    assert page.count("Connected") == 2
    assert "v5.14.0" in page


def test_a_failing_connection_shows_why_rather_than_a_bare_cross(client: TestClient, monkeypatch):
    _patch_arr(monkeypatch, _arr_handler(radarr_ok=True, sonarr_ok=False))
    client.post(
        "/settings/arr",
        data={
            "radarr_url": "http://radarr:7878",
            "radarr_api_key": "k1",
            "sonarr_url": "http://sonarr:8989",
            "sonarr_api_key": "wrong",
        },
    )
    page = client.get("/settings").text
    assert "Connected" in page  # Radarr still fine
    assert "Rejected the API key" in page


def test_test_button_rechecks_a_single_service(client: TestClient, monkeypatch):
    _patch_arr(monkeypatch, _arr_handler(sonarr_ok=False))
    client.post(
        "/settings/arr",
        data={"radarr_url": "http://radarr:7878", "radarr_api_key": "k1",
              "sonarr_url": "http://sonarr:8989", "sonarr_api_key": "wrong"},
    )
    # Sonarr comes good; re-testing just that one must not touch Radarr's result.
    _patch_arr(monkeypatch, _arr_handler())
    resp = client.post("/settings/arr/test", data={"service": "sonarr"})
    assert "Sonarr: OK" in resp.text
    assert "Radarr" not in resp.text.split("Sonarr: OK")[0][-80:]  # only sonarr was reported
    assert client.get("/settings").text.count("Connected") == 2


def test_hidden_attribute_actually_hides_flex_containers(client: TestClient):
    """Tailwind's .flex utility sits after preflight's [hidden]{display:none}
    at equal specificity, so the topbar's flex containers stayed visible even
    with the attribute set — an idle page still rendered the running job's
    pulsing dot, and the error banner showed up empty.
    """
    page = client.get("/").text
    assert "[hidden] { display: none !important; }" in page


def test_page_reloads_when_work_completed_unseen(client: TestClient, media_dir: Path):
    """A scheduled job can start and finish entirely while a tab sits in the
    background on the slow poll. wasActive never flips in that case, so the
    reload-on-finish never fires and the page keeps showing the counts it was
    rendered with. The script compares against those counts to notice.
    """
    page = client.get("/").text
    assert "renderedCounts" in page
    assert "countsMoved" in page
    # Rendered with the real current values, so a later poll can diff them.
    status = client.get("/api/status").json()
    assert "total_files" in status  # a scan can add files without moving any other count


def test_the_page_does_not_reload_over_input_someone_is_typing(client: TestClient):
    """Every page shares the polling script, including ones that are nothing
    but a form. A scheduled scan finishing moves the counts, and reloading on
    that would discard regexes someone is halfway through typing.
    """
    page = client.get("/rules").text
    assert "hasUnsavedInput" in page
    assert "(wasActive || countsMoved(data)) && !hasUnsavedInput()" in page


def test_a_garbled_form_index_is_rejected_not_a_500(client: TestClient, media_dir: Path):
    """These fields carry stream indices out of the review forms. A value
    that isn't a number means the submission didn't arrive from the rendered
    form intact — the neighbouring hidden-field guard already treats that as
    malformed, but a non-numeric index crashed with a 500 error page instead.
    """
    client.post("/rules", data={"audio_keep_languages": "eng", "subtitle_keep_languages": "eng"})
    client.post("/settings/media-paths", data={"paths": f"{media_dir},movie"})
    client.post("/scan")
    _wait_for_idle(client)

    change_id = client.get("/api/review", params={"status": "pending"}).json()[0]["id"]
    resp = client.post(f"/review/{change_id}/approve", data={"approve_submitted": "1", "drop_index": "abc"})
    assert resp.status_code == 200
    assert "malformed submission" in resp.text

    # Still pending — nothing was silently approved.
    assert len(client.get("/api/review", params={"status": "pending"}).json()) == 1


def test_a_garbled_day_of_week_is_rejected_not_a_500(client: TestClient):
    resp = client.post("/schedule", data={"hour": "4", "minute": "0", "run_clean": "on", "days_of_week": "xyz"})
    assert resp.status_code == 200
    assert "Invalid days" in resp.text
    assert "No schedules yet" in resp.text


def test_an_out_of_range_day_is_dropped_rather_than_stored(client: TestClient):
    client.post("/schedule", data={"hour": "4", "minute": "0", "run_clean": "on", "days_of_week": ["1", "99"]})
    schedule = asyncio.run(_get_schedule(client.app.state.session_factory))
    assert schedule.days_of_week == [1]


def test_pagination_survives_nonsense_page_numbers(client: TestClient):
    for value in ("-1", "0", "abc", "99999", ""):
        assert client.get("/review", params={"page": value}).status_code == 200


def test_a_hostile_track_title_is_escaped_not_executed(client: TestClient):
    # Track titles come out of media files, which are untrusted input.
    from app.queries import _describe_normalization

    summary = _describe_normalization(
        {"old_title": "<script>alert(1)</script>", "new_title": "English", "old_default": False, "new_default": False}
    )
    assert "<script>" in summary  # the value itself is preserved verbatim…
    page = client.get("/review").text
    assert "<script>alert(1)</script>" not in page  # …and escaped at render time


def test_a_kept_track_says_which_one_it_is_and_why(client: TestClient, media_dir: Path):
    """Kept tracks rendered as bare "KEEP subtitle hin" — no title, no reason
    — while the dropped row beneath them carried both. With two subtitles in
    one language, one kept and one dropped, there was nothing on screen that
    said which was which or why: the answer ("forced subtitle, always kept")
    was in the data the whole time and simply wasn't shown.
    """
    client.post("/rules", data={"audio_keep_languages": "eng", "subtitle_keep_languages": "eng"})
    client.post("/settings/media-paths", data={"paths": f"{media_dir},movie"})
    client.post("/scan")
    _wait_for_idle(client)

    page = client.get("/review").text
    assert "KEEP audio eng" in page
    # The reason travels as a tooltip, the same way the DROP rows do it.
    assert "in keep-list" in page
    # A kept track that has a title shows it, so two tracks of the same
    # language can be told apart — which is the whole point.
    assert 'KEEP subtitle eng' in page
