"""A deliberately minimal in-process background job runner. One asyncio
worker task processes jobs strictly one at a time — that's not just
simplicity, it's required: the remux executor must never run concurrently
given how little free space the array has (see app/remux.py).

No Redis/Celery — this app is meant to be a single Docker container, and job
state doesn't need to survive a restart (a scan is idempotent and safe to
re-run; an interrupted apply just leaves that one file's PendingChange row
in `approved` state, ready to be retried on the next apply pass).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


@dataclass
class Job:
    id: str
    kind: str
    state: JobState = JobState.queued
    message: str = ""
    result: dict | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    # Set by the job's own run function as it works — 0/0 means "no progress
    # info available yet" (e.g. before the file count is known), not "done".
    progress_current: int = 0
    progress_total: int = 0
    # Fractional progress (0.0-1.0) *within* the current unit counted by
    # progress_current — e.g. how far a single large remux has gotten, so a
    # single-item apply job's bar isn't frozen at 0% for its whole duration.
    progress_fraction: float = 0.0
    # Which stage of a multi-stage job is running. A scheduled run scans and
    # then applies, resetting the counter in between — without this the bar
    # just silently restarts from zero and looks like it lost its place.
    phase: str = ""
    # When the worker actually picked this job up. Distinct from created_at,
    # which is when it was *queued*: a job that sat behind another one for
    # ten minutes hasn't been working for ten minutes, and estimating a rate
    # from created_at would say it had.
    started_at: datetime | None = None

    @property
    def progress_done(self) -> float:
        """Units completed, including the fraction of the one in flight."""
        return self.progress_current + self.progress_fraction

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return max((end - self.started_at).total_seconds(), 0.0)

    @property
    def eta_seconds(self) -> float | None:
        """Rough seconds remaining, from the average rate so far — None until
        there's enough to divide by.

        Deliberately an average over the whole run rather than a recent-rate
        estimate: a scan's per-file cost swings wildly (an unchanged file is
        a stat, a changed one is an ffprobe), and a windowed rate makes the
        number jump around far more than it informs.
        """
        elapsed = self.elapsed_seconds
        done = self.progress_done
        if not elapsed or self.progress_total <= 0 or done <= 0:
            return None
        remaining = self.progress_total - done
        if remaining <= 0:
            return 0.0
        return remaining * (elapsed / done)


def job_status(job: Job, queued_count: int) -> dict:
    """The shape the topbar's progress bar consumes. Derived values (percent,
    ETA) are computed here rather than in the browser so there is one
    definition of them, and so they can be tested.
    """
    done = job.progress_done
    percent = round(min(done / job.progress_total, 1.0) * 100, 1) if job.progress_total > 0 else None
    return {
        "id": job.id,
        "kind": job.kind,
        "state": job.state.value,
        "phase": job.phase,
        "message": job.message,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "progress_fraction": job.progress_fraction,
        # None means "no total known yet" — the bar shows an indeterminate
        # animation rather than sitting frozen at 0%, which is what a scan
        # looks like while it is still walking the directory tree.
        "percent": percent,
        "elapsed_seconds": job.elapsed_seconds,
        "eta_seconds": job.eta_seconds,
        # Jobs waiting behind this one, so a schedule that queues two doesn't
        # look like it finished when the first one ends.
        "queued_behind": max(queued_count - (1 if job.state.value == "queued" else 0), 0),
    }

RunFn = Callable[[Job], Awaitable[None]]

# Finished jobs beyond this count are pruned (oldest first) after each job
# completes — otherwise, on a long-lived container with nightly scheduled
# scans, _jobs grows without bound for the process's entire uptime, and
# list_recent()'s full sort over every job ever run gets slower every day.
# Queued/running jobs are never pruned, only ones already done/error.
MAX_FINISHED_JOBS = 200


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[tuple[str, RunFn]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def submit(self, kind: str, run: RunFn) -> str:
        job = Job(id=str(uuid.uuid4()), kind=kind)
        self._jobs[job.id] = job
        self._queue.put_nowait((job.id, run))
        return job.id

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    def current(self) -> Job | None:
        """The job the UI should show progress for: the one actually running,
        or failing that the one that will run next.

        The running job wins over a newer queued one. A schedule submits its
        clean job and then its normalize job together, so picking the most
        recently created active job showed the *queued* normalize job — with
        an empty progress bar — for the entire duration of the scan that was
        really running. Among queued jobs the oldest wins, matching the order
        the single worker will actually take them in.
        """
        running = [j for j in self._jobs.values() if j.state == JobState.running]
        if running:
            return max(running, key=lambda j: j.created_at)
        queued = [j for j in self._jobs.values() if j.state == JobState.queued]
        return min(queued, key=lambda j: j.created_at, default=None)

    def queued_count(self) -> int:
        """How many jobs are waiting behind the current one — worth showing,
        since a schedule queues two and the second looks like nothing is
        happening until the first finishes.
        """
        return sum(1 for j in self._jobs.values() if j.state == JobState.queued)

    def last_failed(self) -> Job | None:
        """The most recent job that ended in error, so a failure can be
        surfaced instead of vanishing: current() only ever reports active
        jobs, so a scan that died used to leave the UI showing a cheerful
        "Idle" and no indication anything had gone wrong.
        """
        finished = [j for j in self._jobs.values() if j.finished_at is not None]
        if not finished:
            return None
        # Only the *last* thing that ran, and only if it failed. A failure
        # that something later succeeded past is history, not news — without
        # this, one bad scan nags on every page load until the process
        # restarts, since dismissing it only lives as long as the DOM.
        latest = max(finished, key=lambda j: j.finished_at)
        return latest if latest.state == JobState.error else None

    def _prune_finished(self) -> None:
        finished = [j for j in self._jobs.values() if j.state in (JobState.done, JobState.error)]
        if len(finished) <= MAX_FINISHED_JOBS:
            return
        finished.sort(key=lambda j: j.created_at, reverse=True)
        for stale in finished[MAX_FINISHED_JOBS:]:
            del self._jobs[stale.id]

    async def _worker_loop(self) -> None:
        while True:
            job_id, run = await self._queue.get()
            job = self._jobs[job_id]
            job.state = JobState.running
            job.started_at = datetime.now(timezone.utc)
            try:
                await run(job)
                job.state = JobState.done
            except Exception as e:
                logger.exception("job %s (%s) failed", job_id, job.kind)
                job.state = JobState.error
                job.message = str(e)
            finally:
                job.finished_at = datetime.now(timezone.utc)
                self._prune_finished()
                self._queue.task_done()
