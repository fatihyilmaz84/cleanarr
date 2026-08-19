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
        """The most recently created queued/running job, if any — the single
        thing the UI needs to know to show a live progress indicator. Cheaper
        than list_recent() for callers (like every page load) that only need
        this, not the last 20 jobs' full detail.
        """
        candidates = [j for j in self._jobs.values() if j.state in (JobState.queued, JobState.running)]
        return max(candidates, key=lambda j: j.created_at, default=None)

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
