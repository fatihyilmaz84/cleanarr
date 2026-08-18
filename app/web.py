"""Server-rendered UI. Plain HTML forms + full-page redirects rather than
htmx/JS — this is a small single-operator admin tool, and classic
POST-redirect-GET is the simplest thing that can't get out of sync with the
job queue's actual state. The topbar's Idle/Running indicator plus a 2s
meta-refresh while a job is in flight give it a "live" feel without any
client-side JS.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

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
    DisplaySettings,
    MediaPath,
    Schedule,
    get_arr_config,
    get_display_settings,
    get_media_paths,
    get_rule_config,
    get_schedules,
    set_arr_config,
    set_display_settings,
    set_media_paths,
    set_rule_config,
    set_schedules,
)

templates = Jinja2Templates(directory="app/templates")

web_router = APIRouter()


def _localtime(dt: datetime | None, tz_name: str) -> datetime | None:
    """Jinja filter: convert a stored UTC-aware datetime to the configured
    display timezone. Falls back to UTC for an unrecognized zone name rather
    than raising mid-render.
    """
    if dt is None:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return dt.astimezone(tz)


templates.env.filters["localtime"] = _localtime


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
    display_settings = await get_display_settings(session)
    return {
        "request": request,
        "pending_review_count": stats["pending_review_count"],
        "queued_count": stats["queued_count"],
        "current_job": current_job,
        "auto_refresh": current_job is not None,
        "messages": [msg] if msg else [],
        "display_timezone": display_settings.timezone,
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
    """Moves a change to `approved` (queued) — doesn't apply it. Applying
    happens on the Queue page, one "Run Queue" pass at a time (see
    ui_run_queue below), so several files can be reviewed and then applied
    together instead of triggering a separate job per approval.
    """
    change = await session.get(PendingChange, change_id)
    if change is not None:
        form = await request.form()
        # Every proposed-drop stream renders as a checked-by-default
        # checkbox (see review.html) — whatever stayed checked is what
        # actually gets dropped; anything unchecked becomes an override
        # that force-keeps that stream instead.
        confirmed_drops = {int(v) for v in form.getlist("drop_index")}
        proposed_drops = {p["index"] for p in change.proposed if not p["keep"]}
        change.overrides = sorted(proposed_drops - confirmed_drops)
        change.status = ChangeStatus.approved
        session.add(change)
        await session.commit()
    return _redirect("/review", "Added to queue.")


@web_router.post("/review/{change_id}/skip")
async def ui_skip(change_id: int, session: AsyncSession = Depends(get_session)):
    change = await session.get(PendingChange, change_id)
    if change is not None:
        change.status = ChangeStatus.skipped
        session.add(change)
        await session.commit()
    return _redirect("/review")


@web_router.get("/queue")
async def ui_queue(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["items"] = await list_review_items(session, ChangeStatus.approved)
    return templates.TemplateResponse(request, "queue.html", ctx)


@web_router.post("/queue/run")
async def ui_run_queue(request: Request, session: AsyncSession = Depends(get_session)):
    queued = await list_review_items(session, ChangeStatus.approved)
    change_ids = [i["id"] for i in queued]
    if change_ids:
        submit_apply_job(request.app.state.session_factory, request.app.state.job_manager, change_ids)
        return _redirect("/queue", f"Running {len(change_ids)} queued change(s) — check back in a moment.")
    return _redirect("/queue", "Queue is empty.")


@web_router.post("/queue/{change_id}/remove")
async def ui_remove_from_queue(change_id: int, session: AsyncSession = Depends(get_session)):
    change = await session.get(PendingChange, change_id)
    if change is not None and change.status == ChangeStatus.approved:
        change.status = ChangeStatus.pending
        change.overrides = None
        session.add(change)
        await session.commit()
    return _redirect("/queue", "Removed from queue — back in Review.")


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
        commentary_title_patterns=_split_csv(form.get("commentary_title_patterns", "")),
        hearing_impaired_title_patterns=_split_csv(form.get("hearing_impaired_title_patterns", "")),
        keep_untagged_language="keep_untagged_language" in form,
        always_keep_forced_subtitles="always_keep_forced_subtitles" in form,
        drop_commentary_tracks="drop_commentary_tracks" in form,
        drop_hearing_impaired_tracks="drop_hearing_impaired_tracks" in form,
        always_keep_original_language="always_keep_original_language" in form,
    )
    await set_rule_config(session, rules)
    return _redirect("/rules", "Rules saved.")


@web_router.get("/settings")
async def ui_settings(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["media_paths"] = await get_media_paths(session)
    ctx["arr"] = (await get_arr_config(session)).redacted()
    ctx["available_timezones"] = sorted(available_timezones())
    return templates.TemplateResponse(request, "settings.html", ctx)


@web_router.post("/settings/display")
async def ui_save_display_settings(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    tz_name = form.get("timezone", "UTC").strip() or "UTC"
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return _redirect("/settings", f"Unrecognized timezone '{tz_name}' — not saved.")
    await set_display_settings(session, DisplaySettings(timezone=tz_name))
    return _redirect("/settings", "Display timezone saved.")


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


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _next_run(schedule: Schedule, now: datetime) -> datetime | None:
    """Next datetime (in `now`'s timezone) this schedule will fire, purely
    for display — the scheduler itself doesn't use this, it just checks
    every poll whether "right now" matches (see app/scheduler.py).
    """
    if not schedule.enabled or not schedule.days_of_week:
        return None
    for days_ahead in range(8):
        candidate_date = (now + timedelta(days=days_ahead)).date()
        candidate = datetime.combine(candidate_date, time(schedule.hour, schedule.minute), tzinfo=now.tzinfo)
        if candidate >= now and candidate.weekday() in schedule.days_of_week:
            return candidate
    return None


@web_router.get("/schedule")
async def ui_schedule(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    schedules = await get_schedules(session)
    try:
        tz = ZoneInfo(ctx["display_timezone"])
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    ctx["schedules"] = [
        {
            "schedule": s,
            "next_run": _next_run(s, now),
            "days_label": "every day" if len(s.days_of_week) == 7 else ", ".join(DAY_NAMES[d] for d in sorted(s.days_of_week)),
            "time_label": (
                f"{s.hour:02d}:{s.minute:02d}-{s.end_hour:02d}:{s.end_minute:02d}"
                if s.end_hour is not None
                else f"{s.hour:02d}:{s.minute:02d}"
            ),
        }
        for s in schedules
    ]
    ctx["day_names"] = DAY_NAMES
    return templates.TemplateResponse(request, "schedule.html", ctx)


def _parse_optional_clock_field(form, hour_key: str, minute_key: str) -> tuple[int | None, int | None]:
    """An end-time hour/minute pair — both fields empty means "no window
    configured" (None, None). Only one of the two filled in is treated the
    same way (an incomplete window is as good as no window, not an error).
    """
    hour_raw = form.get(hour_key, "").strip()
    minute_raw = form.get(minute_key, "").strip()
    if not hour_raw or not minute_raw:
        return None, None
    return int(hour_raw), int(minute_raw)


@web_router.post("/schedule")
async def ui_add_schedule(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    try:
        hour = max(0, min(23, int(form.get("hour", 4))))
        minute = max(0, min(59, int(form.get("minute", 0))))
        end_hour, end_minute = _parse_optional_clock_field(form, "end_hour", "end_minute")
        if end_hour is not None:
            end_hour = max(0, min(23, end_hour))
            end_minute = max(0, min(59, end_minute))
            if end_hour == hour and end_minute == minute:
                # zero-length window is meaningless — treat as "no window"
                end_hour = end_minute = None
    except ValueError:
        return _redirect("/schedule", "Invalid time — not saved.")
    days_of_week = [int(d) for d in form.getlist("days_of_week")]

    schedules = await get_schedules(session)
    schedules.append(
        Schedule(
            label=form.get("label", "").strip(),
            hour=hour,
            minute=minute,
            end_hour=end_hour,
            end_minute=end_minute,
            days_of_week=days_of_week or list(range(7)),
            auto_apply="auto_apply" in form,
            apply_queued="apply_queued" in form,
        )
    )
    await set_schedules(session, schedules)
    return _redirect("/schedule", "Schedule added.")


@web_router.post("/schedule/{schedule_id}/toggle")
async def ui_toggle_schedule(schedule_id: str, session: AsyncSession = Depends(get_session)):
    schedules = await get_schedules(session)
    for s in schedules:
        if s.id == schedule_id:
            s.enabled = not s.enabled
    await set_schedules(session, schedules)
    return _redirect("/schedule")


@web_router.post("/schedule/{schedule_id}/delete")
async def ui_delete_schedule(schedule_id: str, session: AsyncSession = Depends(get_session)):
    schedules = await get_schedules(session)
    schedules = [s for s in schedules if s.id != schedule_id]
    await set_schedules(session, schedules)
    return _redirect("/schedule", "Schedule removed.")
