"""Shared job-submission logic used by both the JSON API and the
server-rendered UI, so the two front ends can't drift out of sync.
"""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.apply import apply_pending_change
from app.arr_client import ArrClient
from app.jobs import Job, JobManager
from app.scanner import run_scan
from app.settings_store import ArrConfig, get_arr_config, get_media_paths, get_rule_config


def build_arr_client(arr_config: ArrConfig) -> ArrClient:
    return ArrClient(
        radarr_url=arr_config.radarr_url,
        radarr_api_key=arr_config.radarr_api_key,
        sonarr_url=arr_config.sonarr_url,
        sonarr_api_key=arr_config.sonarr_api_key,
    )


def submit_scan_job(session_factory: async_sessionmaker, job_manager: JobManager) -> str:
    async def run(job: Job) -> None:
        async with session_factory() as session:
            rule_config = await get_rule_config(session)
            media_paths = await get_media_paths(session)
            arr_config = await get_arr_config(session)

        if not media_paths:
            job.message = "no media paths configured"
            job.result = {"files_seen": 0}
            return

        arr_client = build_arr_client(arr_config)
        async with session_factory() as session:
            summary = await run_scan(session, media_paths, rule_config, arr_client)

        job.result = asdict(summary)
        job.message = (
            f"scanned {summary.files_scanned} file(s), "
            f"{summary.files_with_pending_changes} need changes, "
            f"{len(summary.errors)} error(s)"
        )

    return job_manager.submit("scan", run)


def submit_apply_job(session_factory: async_sessionmaker, job_manager: JobManager, change_ids: list[int]) -> str:
    async def run(job: Job) -> None:
        results = []
        # Applied strictly one file at a time within this job — required by
        # the remux executor given how little free space the array has.
        for change_id in change_ids:
            async with session_factory() as session:
                rule_config = await get_rule_config(session)
                result = await apply_pending_change(session, change_id, rule_config)
                results.append(
                    {
                        "pending_change_id": result.pending_change_id,
                        "success": result.success,
                        "message": result.message,
                        "bytes_reclaimed": result.bytes_reclaimed,
                    }
                )
        job.result = {"results": results}
        succeeded = sum(1 for r in results if r["success"])
        job.message = f"applied {succeeded}/{len(results)}"

    return job_manager.submit("apply", run)
