"""Server-rendered UI. Plain HTML forms + full-page redirects rather than
htmx/JS — this is a small single-operator admin tool, and classic
POST-redirect-GET is the simplest thing that can't get out of sync with the
job queue's actual state. The topbar's Idle/Running indicator plus a 2s
meta-refresh while a job is in flight give it a "live" feel without any
client-side JS.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel.ext.asyncio.session import AsyncSession

from app.actions import submit_apply_job, submit_scan_job
from app.deps import get_session
from app.jobs import JobManager
from app.models import ChangeStatus, PendingChange
from app.queries import list_history_items, list_review_items, overview_stats
from app.rules import RuleConfig
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

templates = Jinja2Templates(directory="app/templates")

web_router = APIRouter()


def _current_job(job_manager: JobManager) -> dict | None:
    for job in job_manager.list_recent():
        if job.state in ("queued", "running"):
            return {"kind": job.kind, "state": job.state.value}
    return None


async def _base_context(request: Request, session: AsyncSession) -> dict:
    job_manager: JobManager = request.app.state.job_manager
    stats = await overview_stats(session)
    msg = request.query_params.get("msg")
    return {
        "request": request,
        "pending_review_count": stats["pending_review_count"],
        "current_job": _current_job(job_manager),
        "auto_refresh": _current_job(job_manager) is not None,
        "messages": [msg] if msg else [],
    }


def _redirect(path: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        path = f"{path}?msg={quote(msg)}"
    return RedirectResponse(path, status_code=303)


@web_router.get("/")
async def ui_overview(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["stats"] = await overview_stats(session)
    ctx["recent_history"] = await list_history_items(session, limit=8)
    return templates.TemplateResponse(request, "overview.html", ctx)


@web_router.post("/scan")
async def ui_trigger_scan(request: Request):
    submit_scan_job(request.app.state.session_factory, request.app.state.job_manager)
    referer = request.headers.get("referer", "/")
    return RedirectResponse(referer, status_code=303)


@web_router.get("/review")
async def ui_review(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["items"] = await list_review_items(session, ChangeStatus.pending)
    return templates.TemplateResponse(request, "review.html", ctx)


@web_router.post("/review/{change_id}/approve")
async def ui_approve(change_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    change = await session.get(PendingChange, change_id)
    if change is not None:
        change.status = ChangeStatus.approved
        session.add(change)
        await session.commit()
        submit_apply_job(request.app.state.session_factory, request.app.state.job_manager, [change_id])
    return _redirect("/review", "Applying — check back in a moment.")


@web_router.post("/review/{change_id}/skip")
async def ui_skip(change_id: int, session: AsyncSession = Depends(get_session)):
    change = await session.get(PendingChange, change_id)
    if change is not None:
        change.status = ChangeStatus.skipped
        session.add(change)
        await session.commit()
    return _redirect("/review")


@web_router.get("/history")
async def ui_history(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["items"] = await list_history_items(session, limit=200)
    return templates.TemplateResponse(request, "history.html", ctx)


@web_router.get("/rules")
async def ui_rules(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["rules"] = await get_rule_config(session)
    return templates.TemplateResponse(request, "rules.html", ctx)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@web_router.post("/rules")
async def ui_save_rules(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    rules = RuleConfig(
        audio_keep_languages=_split_csv(form.get("audio_keep_languages", "")),
        subtitle_keep_languages=_split_csv(form.get("subtitle_keep_languages", "")),
        drop_title_patterns=_split_csv(form.get("drop_title_patterns", "")),
        keep_untagged_language="keep_untagged_language" in form,
        always_keep_forced_subtitles="always_keep_forced_subtitles" in form,
        drop_commentary_tracks="drop_commentary_tracks" in form,
    )
    await set_rule_config(session, rules)
    return _redirect("/rules", "Rules saved.")


@web_router.get("/settings")
async def ui_settings(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["media_paths"] = await get_media_paths(session)
    ctx["arr"] = (await get_arr_config(session)).redacted()
    return templates.TemplateResponse(request, "settings.html", ctx)


@web_router.post("/settings/media-paths")
async def ui_save_media_paths(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    paths: list[MediaPath] = []
    for line in form.get("paths", "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        path = parts[0]
        library_type = parts[1] if len(parts) > 1 and parts[1] in ("movie", "tv") else "unknown"
        paths.append(MediaPath(path=path, library_type=library_type))
    await set_media_paths(session, paths)
    return _redirect("/settings", "Media paths saved.")


@web_router.post("/settings/arr")
async def ui_save_arr(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    existing = await get_arr_config(session)
    config = ArrConfig(
        radarr_url=form.get("radarr_url") or None,
        radarr_api_key=form.get("radarr_api_key") or existing.radarr_api_key,
        sonarr_url=form.get("sonarr_url") or None,
        sonarr_api_key=form.get("sonarr_api_key") or existing.sonarr_api_key,
    )
    await set_arr_config(session, config)
    return _redirect("/settings", "Sonarr/Radarr connection saved.")
