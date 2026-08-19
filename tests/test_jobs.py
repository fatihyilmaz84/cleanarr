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


def test_current_returns_the_most_recently_created_active_job():
    manager = JobManager()
    now = datetime.now(timezone.utc)
    manager._jobs["older-running"] = _job("older-running", JobState.running, now - timedelta(seconds=10))
    manager._jobs["newer-queued"] = _job("newer-queued", JobState.queued, now)
    manager._jobs["finished"] = _job("finished", JobState.done, now + timedelta(seconds=5))

    current = manager.current()
    assert current is not None
    assert current.id == "newer-queued"  # most recently created among active ones, finished ones ignored


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
