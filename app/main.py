"""FastAPI app: settings, scan/apply jobs, review queue, history.

`create_app(db_path)` is a factory so tests can point each test at its own
throwaway SQLite file; `app` below is the module-level instance used by
uvicorn (`app.main:app`), bound to the default/env-configured DB path.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel.ext.asyncio.session import AsyncSession

from app.actions import submit_apply_job, submit_scan_job
from app.db import init_db, make_engine, make_session_factory
from app.deps import get_session
from app.jobs import JobManager, job_status
from app.models import ChangeStatus, PendingChange
from app.queries import list_history_items, list_review_items, normalize_stats, overview_stats
from app.rules import RuleConfig
from app.scheduler import Scheduler
from app.settings_store import (
    ArrConfig,
    MediaPath,
    get_arr_config,
    get_media_paths,
    get_rule_config,
    set_arr_config,
    set_media_paths,
    set_rule_config,
)

router = APIRouter()


STATIC_DIR = Path(__file__).resolve().parent / "static"


@router.get("/api/health")
async def health():
    return {"status": "ok"}


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Served at the root path as well as from /static, because browsers ask
    for /favicon.ico on their own — for tabs opened before the page's own
    <link rel="icon"> is parsed, and for anything that ignores it.
    """
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@router.get("/api/settings")
async def get_settings(session: AsyncSession = Depends(get_session)):
    rules = await get_rule_config(session)
    media_paths = await get_media_paths(session)
    arr = await get_arr_config(session)
    return {
        "rules": rules.model_dump(),
        "media_paths": [p.model_dump() for p in media_paths],
        "arr": arr.redacted().model_dump(),
    }


@router.put("/api/settings/rules")
async def update_rules(rules: RuleConfig, session: AsyncSession = Depends(get_session)):
    await set_rule_config(session, rules)
    return rules


@router.put("/api/settings/media-paths")
async def update_media_paths(paths: list[MediaPath], session: AsyncSession = Depends(get_session)):
    await set_media_paths(session, paths)
    return paths


@router.put("/api/settings/arr")
async def update_arr(config: ArrConfig, session: AsyncSession = Depends(get_session)):
    await set_arr_config(session, config)
    return config.redacted()


@router.post("/api/scan")
async def trigger_scan(request: Request):
    job_id = submit_scan_job(request.app.state.session_factory, request.app.state.job_manager)
    return {"job_id": job_id}


@router.get("/api/jobs")
async def list_jobs(request: Request):
    job_manager: JobManager = request.app.state.job_manager
    return [
        {
            "id": j.id,
            "kind": j.kind,
            "state": j.state.value,
            "message": j.message,
            "result": j.result,
            "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "progress_current": j.progress_current,
            "progress_total": j.progress_total,
            "progress_fraction": j.progress_fraction,
        }
        for j in job_manager.list_recent()
    ]


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    job_manager: JobManager = request.app.state.job_manager
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {
        "id": job.id,
        "kind": job.kind,
        "state": job.state.value,
        "message": job.message,
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "progress_fraction": job.progress_fraction,
    }


@router.get("/api/review")
async def list_review(status: str = "pending", session: AsyncSession = Depends(get_session)):
    try:
        status_enum = ChangeStatus(status)
    except ValueError as e:
        raise HTTPException(400, f"invalid status '{status}'") from e

    return await list_review_items(session, status_enum)


@router.post("/api/review/{change_id}/approve")
async def approve_change(change_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    change = await session.get(PendingChange, change_id)
    if change is None:
        raise HTTPException(404, "pending change not found")
    change.status = ChangeStatus.approved
    session.add(change)
    await session.commit()

    job_id = submit_apply_job(request.app.state.session_factory, request.app.state.job_manager, [change_id])
    return {"job_id": job_id}


@router.post("/api/review/approve-bulk")
async def approve_bulk(change_ids: list[int], request: Request, session: AsyncSession = Depends(get_session)):
    for change_id in change_ids:
        change = await session.get(PendingChange, change_id)
        if change is not None:
            change.status = ChangeStatus.approved
            session.add(change)
    await session.commit()

    job_id = submit_apply_job(request.app.state.session_factory, request.app.state.job_manager, change_ids)
    return {"job_id": job_id}


@router.post("/api/review/{change_id}/skip")
async def skip_change(change_id: int, session: AsyncSession = Depends(get_session)):
    change = await session.get(PendingChange, change_id)
    if change is None:
        raise HTTPException(404, "pending change not found")
    change.status = ChangeStatus.skipped
    session.add(change)
    await session.commit()
    return {"status": "skipped"}


@router.get("/api/history")
async def list_history(limit: int = 50, session: AsyncSession = Depends(get_session)):
    return await list_history_items(session, limit)


@router.get("/api/overview")
async def overview(session: AsyncSession = Depends(get_session)):
    return await overview_stats(session)


@router.get("/api/status")
async def status(request: Request, session: AsyncSession = Depends(get_session)):
    """Lightweight polling target for the topbar's live job indicator and
    nav badges (see app/templates/base.html) — deliberately smaller than
    /api/jobs (no per-job result blobs, no job history) so polling it every
    couple seconds while a job runs is cheap.
    """
    job_manager: JobManager = request.app.state.job_manager
    job = job_manager.current()
    failed = job_manager.last_failed()
    stats = await overview_stats(session)
    norm_stats = await normalize_stats(session)
    return {
        "job": None if job is None else job_status(job, job_manager.queued_count()),
        # Reported alongside (not instead of) the active job: a failure that
        # happened before the current job started still needs surfacing, and
        # the client decides whether it has already been dismissed.
        "last_error": None
        if failed is None
        else {
            "id": failed.id,
            "kind": failed.kind,
            "message": failed.message,
            "finished_at": failed.finished_at.isoformat() if failed.finished_at else None,
        },
        "pending_review_count": stats["pending_review_count"],
        "queued_count": stats["queued_count"],
        "normalize_pending_count": norm_stats["pending_count"],
        "normalize_queued_count": norm_stats["queued_count"],
    }


def _make_lifespan(db_path: Path | None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(db_path)
        await init_db(engine)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.job_manager = JobManager()
        app.state.job_manager.start()
        app.state.scheduler = Scheduler(app.state.session_factory, app.state.job_manager)
        app.state.scheduler.start()
        yield
        await app.state.scheduler.stop()
        await app.state.job_manager.stop()
        await engine.dispose()

    return lifespan


def create_app(db_path: Path | None = None) -> FastAPI:
    from app.normalize_web import normalize_router  # local import: avoids a circular import with app.actions
    from app.web import web_router  # local import: avoids a circular import with app.actions

    app = FastAPI(title="Cleanarr", lifespan=_make_lifespan(db_path))
    app.add_middleware(GZipMiddleware, minimum_size=500)
    # Resolved from this module's own location rather than the working
    # directory, so it doesn't depend on where uvicorn was started from.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    app.include_router(web_router)
    app.include_router(normalize_router)
    return app


app = create_app()
