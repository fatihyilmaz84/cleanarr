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
from app.languages import LANGUAGE_OPTIONS, iso_codes_for_language_name
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
            return {
                "kind": job.kind,
                "state": job.state.value,
                "progress_current": job.progress_current,
                "progress_total": job.progress_total,
                "progress_fraction": job.progress_fraction,
                "message": job.message,
            }
    return None


async def _base_context(request: Request, session: AsyncSession) -> dict:
    job_manager: JobManager = request.app.state.job_manager
    stats = await overview_stats(session)
    msg = request.query_params.get("msg")
    current_job = _current_job(job_manager)
    return {
        "request": request,
        "pending_review_count": stats["pending_review_count"],
        "current_job": current_job,
        "auto_refresh": current_job is not None,
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
    all_items = await list_review_items(session, ChangeStatus.pending)

    # Filter options always reflect the full unfiltered set, so picking a
    # filter doesn't make other options disappear from the dropdowns.
    ctx["available_library_types"] = sorted({i["library_type"] for i in all_items if i["library_type"]})
    ctx["available_languages"] = sorted({i["original_language"] for i in all_items if i["original_language"]})

    library_type = request.query_params.get("library_type", "")
    language = request.query_params.get("language", "")
    drop_type = request.query_params.get("drop_type", "")
    q = request.query_params.get("q", "").strip()
    q_lower = q.lower()

    items = all_items
    if library_type:
        items = [i for i in items if i["library_type"] == library_type]
    if language:
        items = [i for i in items if i["original_language"] == language]
    if drop_type:
        items = [i for i in items if any(p["type"] == drop_type for p in i["dropped"])]
    if q_lower:
        items = [
            i
            for i in items
            if q_lower in (i["display_title"] or "").lower() or q_lower in (i["path"] or "").lower()
        ]

    ctx["items"] = items
    ctx["total_count"] = len(all_items)
    ctx["filters"] = {"library_type": library_type, "language": language, "drop_type": drop_type, "q": q}
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


def _covered_codes() -> set[str]:
    codes: set[str] = set()
    for name, _ in LANGUAGE_OPTIONS:
        codes |= iso_codes_for_language_name(name)
    return codes


def _selected_language_names(codes: list[str]) -> set[str]:
    """Which dropdown options should show as selected for a stored keep-list
    — an option is selected if ANY of its alias codes are present, since a
    keep-list saved from this same dropdown was expanded to every alias.
    """
    codes_lower = {c.lower() for c in codes}
    return {name for name, _ in LANGUAGE_OPTIONS if iso_codes_for_language_name(name) & codes_lower}


def _extra_codes(codes: list[str]) -> str:
    """Codes in a stored keep-list that no dropdown option covers (hand-typed
    via the old free-text box, or just a rarer code) — round-tripped through
    the "other codes" field instead of silently dropped."""
    covered = _covered_codes()
    return ", ".join(c for c in codes if c.lower() not in covered)


@web_router.get("/rules")
async def ui_rules(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    rules = await get_rule_config(session)
    ctx["rules"] = rules
    ctx["language_options"] = LANGUAGE_OPTIONS
    ctx["selected_audio_languages"] = _selected_language_names(rules.audio_keep_languages)
    ctx["selected_subtitle_languages"] = _selected_language_names(rules.subtitle_keep_languages)
    ctx["audio_extra_codes"] = _extra_codes(rules.audio_keep_languages)
    ctx["subtitle_extra_codes"] = _extra_codes(rules.subtitle_keep_languages)
    return templates.TemplateResponse(request, "rules.html", ctx)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _expand_language_selections(selections: list[str]) -> list[str]:
    """A dropdown selection is a display name ("Norwegian") that expands to
    every ISO code that name could be tagged with (nor/nob/nno), so keeping
    "Norwegian" catches regional variants too. A value that isn't a known
    display name (e.g. typed directly against the API) is treated as an
    already-a-code literal rather than dropped.
    """
    codes: set[str] = set()
    for value in selections:
        value = value.strip()
        if not value:
            continue
        matched = iso_codes_for_language_name(value)
        codes |= matched if matched else {value.lower()}
    return sorted(codes)


@web_router.post("/rules")
async def ui_save_rules(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    audio_codes = set(_expand_language_selections(form.getlist("audio_keep_languages")))
    audio_codes |= set(_split_csv(form.get("audio_keep_languages_extra", "")))
    subtitle_codes = set(_expand_language_selections(form.getlist("subtitle_keep_languages")))
    subtitle_codes |= set(_split_csv(form.get("subtitle_keep_languages_extra", "")))

    rules = RuleConfig(
        audio_keep_languages=sorted(audio_codes),
        subtitle_keep_languages=sorted(subtitle_codes),
        drop_title_patterns=_split_csv(form.get("drop_title_patterns", "")),
        keep_untagged_language="keep_untagged_language" in form,
        always_keep_forced_subtitles="always_keep_forced_subtitles" in form,
        drop_commentary_tracks="drop_commentary_tracks" in form,
        always_keep_original_language="always_keep_original_language" in form,
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
