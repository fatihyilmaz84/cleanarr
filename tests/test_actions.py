"""Covers app/actions.py's scheduled-run specifics: a deadline stops the
apply loop *between* files (never mid-file), and a scheduled scan can
combine its own freshly-found pending changes with whatever's already
sitting in the Queue — two independent, explicit opt-ins (auto_apply vs.
apply_queued) rather than one flag conflating a safe behavior with a risky
one. See TODO.md #3 follow-up.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.actions as actions_module
from app.db import init_db, make_engine, make_session_factory
from app.jobs import Job
from app.main import create_app
from app.models import ChangeStatus, LibraryType, MediaFile, PendingChange
from app.rules import RuleConfig
from app.settings_store import set_rule_config
from tests.test_api import _fake_subprocess_run

PROPOSED = [
    {"index": 0, "type": "video", "codec": "h264", "language": None, "title": None, "keep": True, "reason": "video track, always kept"},
    {"index": 1, "type": "audio", "codec": "ac3", "language": "eng", "title": None, "keep": True, "reason": "in keep-list"},
    {"index": 2, "type": "audio", "codec": "aac", "language": "jpn", "title": None, "keep": False, "reason": "not in keep-list"},
]


@pytest.mark.asyncio
async def test_apply_changes_stops_between_files_never_mid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    file1 = media_dir / "Movie1.mkv"
    file2 = media_dir / "Movie2.mkv"
    file1.write_bytes(b"x" * 1000)
    file2.write_bytes(b"x" * 1000)

    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)

    # apply_pending_change re-decides from scratch against the *current*
    # rule config at apply time (never trusts the cached `proposed` list) —
    # without this, the default inert RuleConfig() would keep everything
    # and there'd be nothing to actually apply.
    async with session_factory() as session:
        await set_rule_config(session, RuleConfig(audio_keep_languages=["eng"]))

    change_ids = []
    async with session_factory() as session:
        for f in (file1, file2):
            mf = MediaFile(path=str(f), library_type=LibraryType.movie, size_bytes=1000, mtime=1.0)
            session.add(mf)
            await session.commit()
            await session.refresh(mf)
            change = PendingChange(file_id=mf.id, status=ChangeStatus.pending, proposed=PROPOSED)
            session.add(change)
            await session.commit()
            await session.refresh(change)
            change_ids.append(change.id)

    # Fake clock: "not yet past deadline" for the check before file 1, "past
    # deadline" for the check before file 2 — deterministic regardless of
    # how fast the fakes actually run, unlike relying on real elapsed time.
    real_datetime = actions_module.datetime
    deadline = real_datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    call_times = iter(
        [
            real_datetime(2026, 1, 1, 5, 59, tzinfo=timezone.utc),
            real_datetime(2026, 1, 1, 6, 1, tzinfo=timezone.utc),
        ]
    )

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return next(call_times)

    monkeypatch.setattr(actions_module, "datetime", FakeDatetime)

    job = Job(id="test-job", kind="apply")
    job.progress_total = len(change_ids)

    results, stopped_early = await actions_module._apply_changes(session_factory, job, change_ids, deadline=deadline)

    assert stopped_early is True
    assert len(results) == 1
    assert results[0]["success"] is True
    assert file1.read_bytes() == b"remuxed-bytes"  # file 1 completed fully — never aborted mid-file
    assert file2.read_bytes() == b"x" * 1000  # file 2 never even started

    await engine.dispose()


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


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise TimeoutError(f"job {job_id} did not finish in time")


async def _move_to_approved(session_factory, change_id: int) -> None:
    async with session_factory() as session:
        change = await session.get(PendingChange, change_id)
        change.status = ChangeStatus.approved
        session.add(change)
        await session.commit()


def test_apply_queued_applies_previously_approved_changes_without_auto_apply(client: TestClient):
    from app.actions import submit_scan_job

    # Scan finds the pending change, then move it to "approved" (queued)
    # directly — simulating a prior manual review — rather than via
    # /api/review/{id}/approve, which applies immediately (a deliberately
    # different, synchronous contract from the scheduler's queue-then-run
    # path; see TODO.md #2).
    _wait_for_job(client, submit_scan_job(client.app.state.session_factory, client.app.state.job_manager))
    change_id = client.get("/api/review", params={"status": "pending"}).json()[0]["id"]
    asyncio.run(_move_to_approved(client.app.state.session_factory, change_id))
    assert client.get("/api/review", params={"status": "approved"}).json()[0]["id"] == change_id

    # auto_apply=False (default) — only apply_queued should matter here.
    job_id = submit_scan_job(client.app.state.session_factory, client.app.state.job_manager, apply_queued=True)
    job = _wait_for_job(client, job_id)

    assert job["result"]["applied"] == {"attempted": 1, "succeeded": 1, "stopped_early": False}
    history = client.get("/api/history").json()
    assert len(history) == 1
    assert history[0]["streams_removed"][0]["language"] == "jpn"


def test_apply_queued_without_it_leaves_queue_untouched(client: TestClient):
    from app.actions import submit_scan_job

    _wait_for_job(client, submit_scan_job(client.app.state.session_factory, client.app.state.job_manager))
    change_id = client.get("/api/review", params={"status": "pending"}).json()[0]["id"]
    asyncio.run(_move_to_approved(client.app.state.session_factory, change_id))

    # Default schedule behavior (neither opt-in set) — must not touch the queue.
    job_id = submit_scan_job(client.app.state.session_factory, client.app.state.job_manager)
    job = _wait_for_job(client, job_id)

    assert "applied" not in (job["result"] or {})
    assert client.get("/api/review", params={"status": "approved"}).json()[0]["id"] == change_id
    assert client.get("/api/history").json() == []


def test_auto_apply_and_apply_queued_combine_without_duplicates(client: TestClient):
    from app.actions import submit_scan_job

    # First run: scan finds + auto-applies the file's own pending change.
    job_id = submit_scan_job(client.app.state.session_factory, client.app.state.job_manager, auto_apply=True)
    job = _wait_for_job(client, job_id)
    assert job["result"]["applied"]["succeeded"] == 1
    assert len(client.get("/api/history").json()) == 1

    # Nothing left pending or queued — a second combined run must not
    # re-apply anything (no duplicate application of an already-applied change).
    job_id = submit_scan_job(
        client.app.state.session_factory, client.app.state.job_manager, auto_apply=True, apply_queued=True
    )
    job = _wait_for_job(client, job_id)
    assert "applied" not in (job["result"] or {})
    assert len(client.get("/api/history").json()) == 1


def test_deadline_already_elapsed_skips_apply_phase_entirely(client: TestClient, monkeypatch):
    from app.actions import submit_scan_job

    # The scan phase's own deadline check runs against the real clock in
    # app.scanner (a separate module binding from app.actions.datetime, see
    # below) — a deadline comfortably in the future lets that one file's
    # scan finish normally. Only app.actions's own checks (the pre-apply
    # check and _apply_changes's per-file check) are faked to already be
    # past it, isolating "apply phase skipped" from "scan itself ran out of
    # time" (covered separately by test_scanner.py's deadline tests).
    real_datetime = actions_module.datetime
    deadline = real_datetime.now(timezone.utc) + timedelta(seconds=30)
    already_past_deadline = deadline + timedelta(seconds=1)

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return already_past_deadline

    monkeypatch.setattr(actions_module, "datetime", FakeDatetime)

    job_id = submit_scan_job(
        client.app.state.session_factory, client.app.state.job_manager, auto_apply=True, deadline=deadline
    )
    job = _wait_for_job(client, job_id)

    assert "applied" not in (job["result"] or {})
    assert "time window already elapsed" in job["message"]
    assert client.get("/api/review", params={"status": "pending"}).json() != []  # untouched, still pending
    assert client.get("/api/history").json() == []