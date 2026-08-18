"""Lightweight in-process scheduler: polls once every 30 seconds and fires
any configured Schedule whose time-of-day/day-of-week matches the current
moment (in the configured display timezone — see app/settings_store.py's
DisplaySettings and TODO.md #4/#3), by submitting a scan job.

A schedule with an end time (e.g. 04:00-06:00) also carries a computed
deadline through to that job — see window_deadline() and its use in
app/actions.py/app/scanner.py — so a run with a large queue behind it
doesn't run indefinitely past the window: it stops *between* files once
the deadline passes, never mid-file, leaving whatever's left for the next
scheduled run or a manual "Run Queue".

No external cron/APScheduler dependency, consistent with the deliberately
minimal job runner in app/jobs.py — this is a single-container app with one
schedule list, not a distributed system.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.actions import submit_scan_job
from app.jobs import JobManager
from app.settings_store import Schedule, get_display_settings, get_schedules

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


def schedule_matches(schedule: Schedule, now: datetime) -> bool:
    """Whether `schedule` should fire at this exact minute of `now`. `now`
    must already be in the schedule's intended (display) timezone — this
    function does no timezone handling itself.
    """
    return (
        schedule.enabled
        and now.hour == schedule.hour
        and now.minute == schedule.minute
        and now.weekday() in schedule.days_of_week
    )


def window_deadline(schedule: Schedule, trigger: datetime) -> datetime | None:
    """The instant a run started by `schedule` at `trigger` must stop
    *starting* new work — None if the schedule has no end time configured
    (run to completion, the default). Computed as trigger + duration rather
    than a fresh wall-clock read, so it's correct across a DST transition
    that happens to fall inside the window.

    An end time at or before the start time is treated as the window
    spanning past midnight (e.g. 23:00-02:00 is a 3-hour window, not a
    negative one) — see the Schedule model docstring.
    """
    if schedule.end_hour is None or schedule.end_minute is None:
        return None
    start_minutes = schedule.hour * 60 + schedule.minute
    end_minutes = schedule.end_hour * 60 + schedule.end_minute
    duration = end_minutes - start_minutes
    if duration <= 0:
        duration += 24 * 60
    return trigger + timedelta(minutes=duration)


class Scheduler:
    def __init__(self, session_factory: async_sessionmaker, job_manager: JobManager) -> None:
        self._session_factory = session_factory
        self._job_manager = job_manager
        self._task: asyncio.Task | None = None
        # schedule.id -> "YYYY-MM-DD HH:MM" it last fired at. The poll
        # interval is well under a minute so a schedule is checked several
        # times within its matching minute; this stops it firing more than
        # once for that minute.
        self._last_fired: dict[str, str] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def tick(self, now: datetime | None = None) -> None:
        """One check of every configured schedule against `now` (or the
        real current time, converted to the configured display timezone, if
        not given — tests pass an explicit `now` to avoid depending on the
        wall clock).
        """
        async with self._session_factory() as session:
            schedules = await get_schedules(session)
            if not schedules:
                return
            tz_name = (await get_display_settings(session)).timezone

        if now is None:
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("UTC")
            now = datetime.now(tz)

        minute_key = now.strftime("%Y-%m-%d %H:%M")

        for schedule in schedules:
            if not schedule_matches(schedule, now):
                continue
            if self._last_fired.get(schedule.id) == minute_key:
                continue
            self._last_fired[schedule.id] = minute_key
            deadline = window_deadline(schedule, now)
            logger.info(
                "schedule %s ('%s') firing, auto_apply=%s, apply_queued=%s, deadline=%s",
                schedule.id,
                schedule.label,
                schedule.auto_apply,
                schedule.apply_queued,
                deadline,
            )
            submit_scan_job(
                self._session_factory,
                self._job_manager,
                auto_apply=schedule.auto_apply,
                apply_queued=schedule.apply_queued,
                deadline=deadline,
            )
