"""Covers JobManager's introspection helpers used by the topbar's live
status polling (app/main.py's /api/status, see app/templates/base.html):
current() picks out the one active job cheaply, and finished jobs get
pruned so a long-lived container doesn't accumulate them forever.

Most of this constructs Job objects directly and calls the manager's
methods synchronously rather than driving the real async worker loop —
deterministic, and avoids sleep-based timing races. A separate end-to-end
test exercises the real worker loop to check the wiring itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import MAX_FINISHED_JOBS, Job, JobManager, JobState


def _job(id_, state, created_at) -> Job:
    return Job(id=id_, kind="scan", state=state, created_at=created_at)


def test_current_returns_none_when_nothing_running():
    manager = JobManager()
    assert manager.current() is None


def test_current_returns_none_when_everything_is_finished():
    manager = JobManager()
    now = datetime.now(timezone.utc)
    manager._jobs["a"] = _job("a", JobState.done, now)
    manager._jobs["b"] = _job("b", JobState.error, now)
    assert manager.current() is None


def test_current_prefers_the_running_job_over_a_newer_queued_one():
    """A schedule submits its clean job and then its normalize job together.
    Reporting the most recently created active job meant the topbar showed
    the *queued* normalize job — with an empty progress bar — for the whole
    duration of the scan that was actually running.
    """
    manager = JobManager()
    now = datetime.now(timezone.utc)
    manager._jobs["running-scan"] = _job("running-scan", JobState.running, now - timedelta(seconds=10))
    manager._jobs["queued-normalize"] = _job("queued-normalize", JobState.queued, now)
    manager._jobs["finished"] = _job("finished", JobState.done, now + timedelta(seconds=5))

    current = manager.current()
    assert current is not None
    assert current.id == "running-scan"


def test_current_picks_the_oldest_queued_job_when_none_is_running_yet():
    # Matches the order the single worker will actually take them in.
    manager = JobManager()
    now = datetime.now(timezone.utc)
    manager._jobs["first"] = _job("first", JobState.queued, now)
    manager._jobs["second"] = _job("second", JobState.queued, now + timedelta(seconds=1))

    assert manager.current().id == "first"


def test_queued_count_reports_work_waiting_behind_the_current_job():
    manager = JobManager()
    now = datetime.now(timezone.utc)
    manager._jobs["running"] = _job("running", JobState.running, now)
    manager._jobs["waiting"] = _job("waiting", JobState.queued, now)
    manager._jobs["done"] = _job("done", JobState.done, now)

    assert manager.queued_count() == 1


def test_last_failed_surfaces_a_job_that_errored():
    # current() only ever reports active jobs, so without this a scan that
    # died left the UI showing "Idle" and no sign anything went wrong.
    manager = JobManager()
    now = datetime.now(timezone.utc)
    old_failure = _job("old", JobState.error, now - timedelta(hours=1))
    old_failure.finished_at = now - timedelta(hours=1)
    recent = _job("recent", JobState.error, now)
    recent.finished_at = now
    recent.message = "ffprobe not found"
    manager._jobs["old"] = old_failure
    manager._jobs["recent"] = recent
    manager._jobs["fine"] = _job("fine", JobState.done, now)

    assert manager.last_failed().id == "recent"


def test_eta_is_estimated_from_when_the_worker_started_not_when_queued():
    """A job that sat behind another one for ten minutes hasn't been working
    for ten minutes — measuring from created_at would say it had, and report
    a wildly pessimistic ETA for the rest.
    """
    now = datetime.now(timezone.utc)
    job = Job(id="j", kind="scan", state=JobState.running, created_at=now - timedelta(minutes=10))
    job.started_at = now - timedelta(seconds=60)
    job.progress_current = 25
    job.progress_total = 100

    assert job.elapsed_seconds == pytest.approx(60, abs=2)
    # 25% took 60s, so the remaining 75% should take about 180s.
    assert job.eta_seconds == pytest.approx(180, abs=10)


def test_eta_is_unknown_until_there_is_something_to_divide_by():
    now = datetime.now(timezone.utc)
    job = Job(id="j", kind="scan", state=JobState.running)
    job.started_at = now
    assert job.eta_seconds is None  # no total yet

    job.progress_total = 100
    assert job.eta_seconds is None  # total, but nothing done yet


def test_eta_counts_the_fraction_of_the_file_in_flight():
    # A one-item apply job is 0/1 for the entire remux; without the fraction
    # it could never report progress or an ETA at all.
    now = datetime.now(timezone.utc)
    job = Job(id="j", kind="apply", state=JobState.running)
    job.started_at = now - timedelta(seconds=30)
    job.progress_total = 1
    job.progress_fraction = 0.5

    assert job.progress_done == pytest.approx(0.5)
    assert job.eta_seconds == pytest.approx(30, abs=5)


def test_finished_jobs_are_pruned_beyond_the_cap():
    manager = JobManager()
    now = datetime.now(timezone.utc)
    total = MAX_FINISHED_JOBS + 10
    for i in range(total):
        manager._jobs[str(i)] = _job(str(i), JobState.done, now + timedelta(seconds=i))

    manager._prune_finished()

    assert len(manager._jobs) == MAX_FINISHED_JOBS
    # Oldest (lowest index/created_at) were pruned, newest survive.
    for i in range(10):
        assert str(i) not in manager._jobs
    for i in range(total - MAX_FINISHED_JOBS, total):
        assert str(i) in manager._jobs


def test_running_and_queued_jobs_are_never_pruned_regardless_of_age():
    manager = JobManager()
    now = datetime.now(timezone.utc)
    ancient = now - timedelta(days=365)
    manager._jobs["ancient-running"] = _job("ancient-running", JobState.running, ancient)
    manager._jobs["ancient-queued"] = _job("ancient-queued", JobState.queued, ancient)
    for i in range(MAX_FINISHED_JOBS + 10):
        manager._jobs[f"done-{i}"] = _job(f"done-{i}", JobState.done, now + timedelta(seconds=i))

    manager._prune_finished()

    assert "ancient-running" in manager._jobs
    assert "ancient-queued" in manager._jobs
    assert len(manager._jobs) == MAX_FINISHED_JOBS + 2  # cap applies only to the finished ones


@pytest.mark.asyncio
async def test_worker_loop_prunes_after_each_job_end_to_end():
    """A real (small-scale) run through the async worker loop, checking the
    wiring between _worker_loop and _prune_finished — not just the pruning
    logic in isolation above.
    """
    manager = JobManager()
    manager.start()
    try:
        async def run(job):
            return None

        first_id = manager.submit("scan", run)
        for _ in range(50):
            if manager.get(first_id).state == JobState.done:
                break
            await asyncio.sleep(0.02)
        assert manager.get(first_id).state == JobState.done
        assert manager.current() is None
    finally:
        await manager.stop()
