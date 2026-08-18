"""Shared job-submission logic used by both the JSON API and the
server-rendered UI, so the two front ends can't drift out of sync.
"""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

from app.apply import apply_pending_change
from app.arr_client import ArrClient
from app.jobs import Job, JobManager
from app.models import ChangeStatus, PendingChange
from app.scanner import ScanSummary, run_scan
from app.settings_store import ArrConfig, get_arr_config, get_media_paths, get_rule_config


def build_arr_client(arr_config: ArrConfig) -> ArrClient:
    return ArrClient(
        radarr_url=arr_config.radarr_url,
        radarr_api_key=arr_config.radarr_api_key,
        sonarr_url=arr_config.sonarr_url,
        sonarr_api_key=arr_config.sonarr_api_key,
    )


async def _apply_changes(session_factory: async_sessionmaker, job: Job, change_ids: list[int]) -> list[dict]:
    """Shared by submit_apply_job and a scheduled scan's auto-apply tail —
    applies each change_id in turn, one file at a time (required by the
    remux executor given how little free space the array has), updating
    `job`'s progress as it goes.
    """

    def progress_cb(fraction: float) -> None:
        job.progress_fraction = fraction

    results = []
    for change_id in change_ids:
        job.progress_fraction = 0.0
        job.message = f"applying {job.progress_current + 1}/{job.progress_total}…"
        async with session_factory() as session:
            rule_config = await get_rule_config(session)
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
    return results


def submit_scan_job(session_factory: async_sessionmaker, job_manager: JobManager, auto_apply: bool = False) -> str:
    """`auto_apply` is only ever set by the scheduler (see app/scheduler.py)
    — a manual "Scan Now" never applies anything unattended. When set, every
    pending change the scan produces is applied as-is (no per-track
    overrides — there's no one there to check a box).
    """

    async def run(job: Job) -> None:
        async with session_factory() as session:
            rule_config = await get_rule_config(session)
            media_paths = await get_media_paths(session)
            arr_config = await get_arr_config(session)

        if not media_paths:
            job.message = "no media paths configured"
            job.result = {"files_seen": 0}
            return

        def progress_cb(summary: ScanSummary) -> None:
            job.progress_current = summary.files_seen
            job.progress_total = summary.files_total
            job.message = f"scanning… {summary.files_seen}/{summary.files_total}"

        arr_client = build_arr_client(arr_config)
        async with session_factory() as session:
            summary = await run_scan(session, media_paths, rule_config, arr_client, progress_cb=progress_cb)

        job.result = asdict(summary)
        job.message = (
            f"scanned {summary.files_scanned} file(s), "
            f"{summary.files_with_pending_changes} need changes, "
            f"{len(summary.errors)} error(s)"
        )

        if auto_apply and summary.files_with_pending_changes:
            async with session_factory() as session:
                pending_ids = [
                    c.id
                    for c in (
                        await session.exec(select(PendingChange).where(PendingChange.status == ChangeStatus.pending))
                    ).all()
                ]
            job.progress_current = 0
            job.progress_total = len(pending_ids)
            apply_results = await _apply_changes(session_factory, job, pending_ids)
            applied = sum(1 for r in apply_results if r["success"])
            job.result["auto_applied"] = {"attempted": len(pending_ids), "succeeded": applied}
            job.message += f"; auto-applied {applied}/{len(pending_ids)}"

    return job_manager.submit("scan", run)


def submit_apply_job(session_factory: async_sessionmaker, job_manager: JobManager, change_ids: list[int]) -> str:
    async def run(job: Job) -> None:
        job.progress_total = len(change_ids)
        results = await _apply_changes(session_factory, job, change_ids)
        job.result = {"results": results}
        succeeded = sum(1 for r in results if r["success"])
        job.message = f"applied {succeeded}/{len(results)}"

    return job_manager.submit("apply", run)
