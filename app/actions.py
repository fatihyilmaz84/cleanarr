"""Shared job-submission logic used by both the JSON API and the
server-rendered UI, so the two front ends can't drift out of sync.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

from app.apply import apply_pending_change
from app.arr_client import ArrClient
from app.jobs import Job, JobManager
from app.models import ChangeStatus, MediaFile, NormalizationChange, PendingChange
from app.normalize_service import NormalizeScanSummary, apply_normalization_change, propose_normalizations
from app.scanner import ScanSummary, run_scan
from app.settings_store import (
    ArrConfig,
    get_arr_config,
    get_media_paths,
    resolve_normalizer_config,
    resolve_rule_config,
)


def build_arr_client(arr_config: ArrConfig) -> ArrClient:
    return ArrClient(
        radarr_url=arr_config.radarr_url,
        radarr_api_key=arr_config.radarr_api_key,
        sonarr_url=arr_config.sonarr_url,
        sonarr_api_key=arr_config.sonarr_api_key,
    )


async def _describe_file(session, file_id: int | None) -> str:
    """The name to show while a file is being worked on. Prefers the
    Sonarr/Radarr title and falls back to the filename, so the progress line
    says which film is being remuxed rather than just "3 of 12" — a single
    large remux can hold that slot for several minutes.
    """
    if file_id is None:
        return ""
    media_file = await session.get(MediaFile, file_id)
    if media_file is None:
        return ""
    return media_file.display_title or PurePosixPath(media_file.path).name


async def _apply_changes(
    session_factory: async_sessionmaker, job: Job, change_ids: list[int], deadline: datetime | None = None
) -> tuple[list[dict], bool]:
    """Shared by submit_apply_job and a scheduled scan's apply tail —
    applies each change_id in turn, one file at a time (required by the
    remux executor given how little free space the array has), updating
    `job`'s progress as it goes.

    `deadline`, if given, is checked *between* files only — a remux is
    never aborted partway through once started, since that could leave a
    half-written temp file or worse. Returns (results, stopped_early).
    """

    def progress_cb(fraction: float) -> None:
        job.progress_fraction = fraction

    results = []
    for change_id in change_ids:
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            return results, True

        job.progress_fraction = 0.0
        async with session_factory() as session:
            # Re-decide with the rules that *proposed* this specific change
            # (stamped on the row at scan time), not with whatever the
            # global Rules happen to be now — otherwise a change proposed
            # under one preset would be applied under another, and what
            # gets dropped wouldn't match what was shown or approved.
            change = await session.get(PendingChange, change_id)
            rule_config = await resolve_rule_config(session, change.rule_preset_id if change else None)
            job.message = await _describe_file(session, change.file_id if change else None)
            result = await apply_pending_change(session, change_id, rule_config, progress_cb=progress_cb)
            results.append(
                {
                    "pending_change_id": result.pending_change_id,
                    "success": result.success,
                    "message": result.message,
                    "bytes_reclaimed": result.bytes_reclaimed,
                }
            )
        job.progress_current += 1
        job.progress_fraction = 0.0
    return results, False


def submit_scan_job(
    session_factory: async_sessionmaker,
    job_manager: JobManager,
    auto_apply: bool = False,
    apply_queued: bool = False,
    deadline: datetime | None = None,
    rule_preset_id: str | None = None,
) -> str:
    """`rule_preset_id` selects a saved RulePreset to scan with (None = the
    Rules page's Default) — set by a schedule that has one attached; a
    manual "Scan Now" always uses Default.

    `auto_apply`/`apply_queued`/`deadline` are only ever set by the
    scheduler (see app/scheduler.py) — a manual "Scan Now" never applies
    anything unattended and never has a time limit. Two independent,
    explicit opt-ins, since they carry different amounts of risk:

    - `auto_apply`: every pending change *this scan itself* produces is
      applied as-is, with no human ever having looked at it (no per-track
      overrides either — there's no one there to check a box).
    - `apply_queued`: also applies everything already sitting in the Queue
      (status=approved) from a prior manual review — safe on its own terms,
      since a human already confirmed those specific changes; this just
      does the disk work on a schedule instead of requiring a click.

    `deadline`, if given, bounds *both* the scan and the apply phase — see
    run_scan and _apply_changes. Either can leave a partially-finished
    scan and/or a partially-applied batch; nothing is lost, it just carries
    over to the next scheduled run (or a manual scan / Run Queue).
    """

    async def run(job: Job) -> None:
        async with session_factory() as session:
            rule_config = await resolve_rule_config(session, rule_preset_id)
            media_paths = await get_media_paths(session)
            arr_config = await get_arr_config(session)

        if not media_paths:
            job.message = "no media paths configured"
            job.result = {"files_seen": 0}
            return

        job.phase = "Scanning library"

        def progress_cb(summary: ScanSummary) -> None:
            job.progress_current = summary.files_seen
            job.progress_total = summary.files_total
            job.message = f"{summary.files_scanned} probed, {summary.files_with_pending_changes} with changes"

        arr_client = build_arr_client(arr_config)
        async with session_factory() as session:
            summary = await run_scan(
                session,
                media_paths,
                rule_config,
                arr_client,
                progress_cb=progress_cb,
                deadline=deadline,
                rule_preset_id=rule_preset_id,
            )

        job.result = asdict(summary)
        job.message = (
            f"scanned {summary.files_scanned} file(s), "
            f"{summary.files_with_pending_changes} need changes, "
            f"{len(summary.errors)} error(s)"
        )
        if summary.stopped_early:
            job.message += " — stopped early, hit the schedule's time window"

        ids_to_apply: list[int] = []
        if auto_apply:
            # Scoped to exactly what *this* scan run produced/updated (see
            # ScanSummary.pending_change_ids) — never a fresh query for
            # every pending change, which would also sweep up stale changes
            # left over from an earlier manual scan the user hasn't
            # reviewed yet.
            ids_to_apply.extend(summary.pending_change_ids)
        async with session_factory() as session:
            if apply_queued:
                queued = (
                    await session.exec(select(PendingChange).where(PendingChange.status == ChangeStatus.approved))
                ).all()
                ids_to_apply.extend(c.id for c in queued)

        if not ids_to_apply:
            return

        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            job.message += f"; {len(ids_to_apply)} change(s) left for next time — time window already elapsed"
            return

        job.phase = "Removing tracks"
        job.progress_current = 0
        job.progress_total = len(ids_to_apply)
        apply_results, stopped_early = await _apply_changes(session_factory, job, ids_to_apply, deadline=deadline)
        applied = sum(1 for r in apply_results if r["success"])
        job.result["applied"] = {"attempted": len(ids_to_apply), "succeeded": applied, "stopped_early": stopped_early}
        suffix = " — stopped early, hit the schedule's time window" if stopped_early else ""
        job.message += f"; applied {applied}/{len(ids_to_apply)}{suffix}"

    return job_manager.submit("scan", run)


def submit_apply_job(session_factory: async_sessionmaker, job_manager: JobManager, change_ids: list[int]) -> str:
    async def run(job: Job) -> None:
        job.phase = "Removing tracks"
        job.progress_total = len(change_ids)
        results, _stopped_early = await _apply_changes(session_factory, job, change_ids)
        job.result = {"results": results}
        succeeded = sum(1 for r in results if r["success"])
        job.message = f"applied {succeeded}/{len(results)}"

    return job_manager.submit("apply", run)


async def _apply_normalizations(
    session_factory: async_sessionmaker, job: Job, change_ids: list[int], deadline: datetime | None = None
) -> tuple[list[dict], bool]:
    """Normalizer counterpart of _apply_changes. Each file's config is
    resolved from the preset stamped on its own row, so a change proposed
    under one NormalizerPreset is never applied under a different one.

    `deadline` is checked between files. mkvpropedit rewrites container
    metadata in place and returns near-instantly, so unlike the remux there
    is no long window to interrupt — but a queue of several thousand files
    still adds up, so a scheduled run honours its window here too.
    """
    results: list[dict] = []
    for change_id in change_ids:
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            return results, True

        async with session_factory() as session:
            change = await session.get(NormalizationChange, change_id)
            config = await resolve_normalizer_config(session, change.normalizer_preset_id if change else None)
            job.message = await _describe_file(session, change.file_id if change else None)
            result = await apply_normalization_change(session, change_id, config)
            results.append(
                {
                    "change_id": result.change_id,
                    "success": result.success,
                    "message": result.message,
                    "tracks_updated": result.tracks_updated,
                }
            )
        job.progress_current += 1
    return results, False


def submit_normalize_scan_job(
    session_factory: async_sessionmaker,
    job_manager: JobManager,
    normalizer_preset_id: str | None = None,
    auto_apply: bool = False,
    apply_queued: bool = False,
    deadline: datetime | None = None,
) -> str:
    """Proposes track metadata normalizations for every already-scanned
    file (see app/normalize_service.py — this reads existing MediaFile/
    StreamRecord rows, no ffprobe re-run needed). A separate job kind from
    "scan" since it's an independent system from the rule-based remover —
    see TODO.md #7.

    `normalizer_preset_id`/`auto_apply`/`apply_queued`/`deadline` mirror
    submit_scan_job's, and like there are only ever set by the scheduler:
    a manual "Scan for Normalization" proposes with Default and writes
    nothing to disk. The two apply opt-ins are deliberately separate for
    the same reason as the cleaner's — `apply_queued` runs changes a human
    already reviewed, `auto_apply` runs this pass's own fresh findings with
    nobody having looked at them.
    """

    async def run(job: Job) -> None:
        async with session_factory() as session:
            config = await resolve_normalizer_config(session, normalizer_preset_id)

        job.phase = "Checking track metadata"

        def progress_cb(summary: NormalizeScanSummary) -> None:
            job.progress_current = summary.files_considered
            job.progress_total = summary.files_total
            job.message = f"{summary.files_with_changes} file(s) need changes"

        async with session_factory() as session:
            summary = await propose_normalizations(
                session,
                config,
                progress_cb=progress_cb,
                deadline=deadline,
                normalizer_preset_id=normalizer_preset_id,
            )

        job.result = {
            "files_considered": summary.files_considered,
            "files_with_changes": summary.files_with_changes,
            "files_unsupported_container": summary.files_unsupported_container,
            "errors": summary.errors,
            "stopped_early": summary.stopped_early,
        }
        job.message = f"considered {summary.files_considered} file(s), {summary.files_with_changes} need changes"
        if summary.files_unsupported_container:
            job.message += f", {summary.files_unsupported_container} skipped (only MKV can be edited in place)"
        if summary.stopped_early:
            job.message += " — stopped early, hit the schedule's time window"

        ids_to_apply: list[int] = []
        if auto_apply:
            # Scoped to what this pass itself proposed, never a fresh query
            # for everything pending — same reasoning as submit_scan_job.
            ids_to_apply.extend(summary.change_ids)
        if apply_queued:
            async with session_factory() as session:
                queued = (
                    await session.exec(
                        select(NormalizationChange).where(NormalizationChange.status == ChangeStatus.approved)
                    )
                ).all()
                ids_to_apply.extend(c.id for c in queued)

        if not ids_to_apply:
            return

        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            job.message += f"; {len(ids_to_apply)} change(s) left for next time — time window already elapsed"
            return

        job.phase = "Writing track metadata"
        job.progress_current = 0
        job.progress_total = len(ids_to_apply)
        apply_results, stopped_early = await _apply_normalizations(
            session_factory, job, ids_to_apply, deadline=deadline
        )
        applied = sum(1 for r in apply_results if r["success"])
        job.result["applied"] = {"attempted": len(ids_to_apply), "succeeded": applied, "stopped_early": stopped_early}
        suffix = " — stopped early, hit the schedule's time window" if stopped_early else ""
        job.message += f"; applied {applied}/{len(ids_to_apply)}{suffix}"

    return job_manager.submit("normalize_scan", run)


def submit_normalize_apply_job(
    session_factory: async_sessionmaker, job_manager: JobManager, change_ids: list[int]
) -> str:
    async def run(job: Job) -> None:
        job.phase = "Writing track metadata"
        job.progress_total = len(change_ids)
        results, _stopped_early = await _apply_normalizations(session_factory, job, change_ids)
        job.result = {"results": results}
        succeeded = sum(1 for r in results if r["success"])
        job.message = f"applied {succeeded}/{len(results)}"

    return job_manager.submit("normalize_apply", run)
