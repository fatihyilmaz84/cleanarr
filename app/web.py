"""Server-rendered UI. Plain HTML forms + full-page redirects rather than
htmx/JS — this is a small single-operator admin tool, and classic
POST-redirect-GET is the simplest thing that can't get out of sync with the
job queue's actual state. The topbar's Idle/Running indicator plus a 2s
meta-refresh while a job is in flight give it a "live" feel without any
client-side JS.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel.ext.asyncio.session import AsyncSession

from app.actions import build_arr_client, submit_apply_job, submit_scan_job
from app.deps import get_session
from app.jobs import JobManager, job_status
from app.languages import LANGUAGE_OPTIONS, iso_codes_for_language_name
from app.models import ChangeStatus, PendingChange
from app.queries import list_history_items, list_review_items, normalize_stats, overview_stats
from app.rules import RuleConfig
from app.settings_store import (
    ArrConfig,
    ArrConnectionStatus,
    DisplaySettings,
    MediaPath,
    RulePreset,
    Schedule,
    get_arr_config,
    get_arr_status,
    get_display_settings,
    get_media_paths,
    get_normalizer_presets,
    get_rule_config,
    get_rule_presets,
    get_schedules,
    set_arr_config,
    set_arr_status,
    set_display_settings,
    set_media_paths,
    set_rule_config,
    set_rule_presets,
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
    """First-paint copy of what /api/status streams — same shape, so the
    server-rendered bar and the polled one can't disagree about a job that
    was already running when the page was requested.
    """
    job = job_manager.current()
    if job is None:
        return None
    return job_status(job, job_manager.queued_count())


async def _base_context(request: Request, session: AsyncSession) -> dict:
    job_manager: JobManager = request.app.state.job_manager
    stats = await overview_stats(session)
    norm_stats = await normalize_stats(session)
    msg = request.query_params.get("msg")
    current_job = _current_job(job_manager)
    display_settings = await get_display_settings(session)
    return {
        "request": request,
        "pending_review_count": stats["pending_review_count"],
        "queued_count": stats["queued_count"],
        "normalize_pending_count": norm_stats["pending_count"],
        "normalize_queued_count": norm_stats["queued_count"],
        "current_job": current_job,
        "messages": [msg] if msg else [],
        "display_timezone": display_settings.timezone,
        "_overview_stats": stats,  # let ui_overview reuse this instead of re-querying
    }


def parse_index_list(form, field: str) -> list[int] | None:
    """Integer values of a repeated form field, or None if any of them isn't
    one.

    These carry stream indices out of the review forms, and a value that
    isn't a number means the submission didn't come from the rendered form
    intact. Crashing on it gave a 500 error page; the caller turns None into
    the same "malformed submission, nothing changed" the neighbouring
    guards already produce.
    """
    values = []
    for raw in form.getlist(field):
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            return None
    return values


def _redirect(path: str, msg: str | None = None) -> RedirectResponse:
    # `path` may already carry a query string (e.g. "/rules?preset=<id>"),
    # in which case the message is a second param — appending a second "?"
    # would fold "msg=..." into the *value* of the preceding param and both
    # the message and that param would be lost.
    if msg:
        path = f"{path}{'&' if '?' in path else '?'}msg={quote(msg)}"
    return RedirectResponse(path, status_code=303)


PAGE_SIZE = 50


def _paginate(items: list[dict], request: Request, page_size: int = PAGE_SIZE) -> tuple[list[dict], dict]:
    """Slices an already-fetched, already-filtered item list for display —
    Review/Queue/Normalize used to render every pending item in one
    response; for a large first scan (hundreds+ files) that's a multi-MB
    page and hundreds of DOM nodes. Other query params (filters, etc.) are
    preserved on the prev/next links.
    """
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    total = len(items)
    total_pages = max(1, -(-total // page_size))  # ceil div
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]

    def _page_url(p: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(p)
        return f"{request.url.path}?{urlencode(params)}"

    return page_items, {
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "page_size": page_size,
        "prev_url": _page_url(page - 1) if page > 1 else None,
        "next_url": _page_url(page + 1) if page < total_pages else None,
    }


@web_router.get("/")
async def ui_overview(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    # _base_context already ran overview_stats for the nav badges — reuse it
    # instead of querying the same aggregates a second time.
    ctx["stats"] = ctx["_overview_stats"]
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

    filtered_count = len(items)
    page_items, pagination = _paginate(items, request)

    ctx["items"] = page_items
    ctx["filtered_count"] = filtered_count
    ctx["total_count"] = len(all_items)
    ctx["pagination"] = pagination
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
        # A legitimate "uncheck everything, force-keep every track" submit
        # still carries this hidden field (see review.html); its absence
        # means the POST didn't come from the real form at all (malformed,
        # stripped, or missing body) — reject rather than silently treating
        # it as "drop nothing".
        if "approve_submitted" not in form:
            return _redirect("/review", "Approve failed — malformed submission, nothing changed.")
        # Every proposed-drop stream renders as a checked-by-default
        # checkbox (see review.html) — whatever stayed checked is what
        # actually gets dropped; anything unchecked becomes an override
        # that force-keeps that stream instead.
        drop_indices = parse_index_list(form, "drop_index")
        if drop_indices is None:
            return _redirect("/review", "Approve failed — malformed submission, nothing changed.")
        confirmed_drops = set(drop_indices)
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
    all_items = await list_review_items(session, ChangeStatus.approved)
    page_items, pagination = _paginate(all_items, request)
    ctx["items"] = page_items
    ctx["total_count"] = len(all_items)
    ctx["pagination"] = pagination
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
    """One form, two targets: with no `?preset=` it edits the Default rules
    (what manual Scan Now uses); with one, it edits that saved RulePreset
    instead. An unknown/deleted id silently falls back to Default rather
    than 404ing — same forgiving behavior as resolve_rule_config.
    """
    ctx = await _base_context(request, session)
    presets = await get_rule_presets(session)
    preset_id = request.query_params.get("preset") or None
    editing = next((p for p in presets if p.id == preset_id), None)

    rules = editing.config if editing else await get_rule_config(session)
    ctx["rules"] = rules
    ctx["presets"] = presets
    ctx["editing_preset"] = editing
    ctx["form_action"] = f"/rules?preset={editing.id}" if editing else "/rules"
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

    preset_id = request.query_params.get("preset") or None
    if preset_id:
        presets = await get_rule_presets(session)
        for preset in presets:
            if preset.id == preset_id:
                preset.config = rules
                await set_rule_presets(session, presets)
                return _redirect(f"/rules?preset={preset_id}", f"Preset '{preset.name}' saved.")
        # Preset deleted in another tab between GET and POST — don't
        # silently write these edits into Default instead.
        return _redirect("/rules", "That preset no longer exists — nothing saved.")

    await set_rule_config(session, rules)
    return _redirect("/rules", "Rules saved.")


@web_router.post("/rules/presets")
async def ui_add_rule_preset(request: Request, session: AsyncSession = Depends(get_session)):
    """Creates a named preset seeded from the current Default rules — you
    then Edit it to diverge. Seeding from Default (rather than from empty)
    means a new preset never starts out as an inert "keep nothing"
    config that a schedule could quietly attach to.
    """
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return _redirect("/rules", "Preset needs a name — not saved.")

    presets = await get_rule_presets(session)
    if any(p.name.lower() == name.lower() for p in presets):
        return _redirect("/rules", f"A preset named '{name}' already exists.")

    presets.append(RulePreset(name=name, config=await get_rule_config(session)))
    await set_rule_presets(session, presets)
    return _redirect("/rules", f"Preset '{name}' created from your current rules.")


@web_router.post("/rules/presets/{preset_id}/delete")
async def ui_delete_rule_preset(preset_id: str, session: AsyncSession = Depends(get_session)):
    presets = await get_rule_presets(session)
    remaining = [p for p in presets if p.id != preset_id]
    await set_rule_presets(session, remaining)
    # Schedules and already-proposed changes still referencing this id fall
    # back to Default (see resolve_rule_config) rather than breaking, so
    # deleting one is never destructive to a queued change.
    return _redirect("/rules", "Preset deleted — anything using it falls back to Default rules.")


@web_router.get("/settings")
async def ui_settings(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    ctx["media_paths"] = await get_media_paths(session)
    ctx["arr"] = (await get_arr_config(session)).redacted()
    # Last *stored* test result, not a fresh one — testing on every page load
    # would block the render on two network round trips (up to 15s each).
    ctx["arr_status"] = await get_arr_status(session)
    ctx["available_timezones"] = sorted(available_timezones())
    return templates.TemplateResponse(request, "settings.html", ctx)


async def _test_and_store_arr(session: AsyncSession, services: list[str]) -> list[str]:
    """Test each named service, persist the result, return one summary line
    per service for the redirect message.
    """
    client = build_arr_client(await get_arr_config(session))
    messages = []
    for service in services:
        result = await client.test_connection(service)
        await set_arr_status(
            session,
            service,
            ArrConnectionStatus(ok=result.ok, detail=result.detail, checked_at=datetime.now(timezone.utc)),
        )
        label = service.capitalize()
        messages.append(f"{label}: {'OK — ' if result.ok else 'failed — '}{result.detail}")
    return messages


@web_router.post("/settings/arr/test")
async def ui_test_arr(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    requested = form.get("service", "").strip()
    services = [requested] if requested in ("radarr", "sonarr") else ["radarr", "sonarr"]
    messages = await _test_and_store_arr(session, services)
    return _redirect("/settings", " · ".join(messages))


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

    # Test right after saving rather than making the user click a second
    # button: getting these details wrong is the whole failure mode, and the
    # answer is worth the round trip at exactly this moment. A service left
    # unconfigured just reports that and costs no request.
    messages = await _test_and_store_arr(session, ["radarr", "sonarr"])
    return _redirect("/settings", "Connection saved. " + " · ".join(messages))


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
    """One form, two targets — the same shape /rules already uses for
    presets: with no `?edit=` the form adds a new schedule, with one it
    edits that saved schedule in place. An unknown/deleted id falls back to
    the add form rather than 404ing.
    """
    ctx = await _base_context(request, session)
    schedules = await get_schedules(session)
    editing = next((s for s in schedules if s.id == (request.query_params.get("edit") or None)), None)
    try:
        tz = ZoneInfo(ctx["display_timezone"])
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)

    rule_presets = await get_rule_presets(session)
    normalizer_presets = await get_normalizer_presets(session)
    rule_preset_names = {p.id: p.name for p in rule_presets}
    normalizer_preset_names = {p.id: p.name for p in normalizer_presets}

    def _preset_label(names: dict[str, str], preset_id: str | None) -> str:
        # A schedule can outlive the preset it points at; show that plainly
        # rather than a bare id, since the run will fall back to Default.
        if not preset_id:
            return "Default"
        return names.get(preset_id, "deleted preset → Default")

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
            "rule_preset_label": _preset_label(rule_preset_names, s.rule_preset_id),
            "normalizer_preset_label": _preset_label(normalizer_preset_names, s.normalizer_preset_id),
        }
        for s in schedules
    ]
    ctx["day_names"] = DAY_NAMES
    ctx["rule_presets"] = rule_presets
    ctx["normalizer_presets"] = normalizer_presets
    ctx["editing"] = editing
    # A brand-new Schedule() carries exactly the defaults the add form used
    # to hardcode (04:00, every day, clean on, everything else off), so a
    # single set of prefilled inputs serves both modes with no duplicate
    # "add form" / "edit form" markup to keep in sync.
    ctx["form_schedule"] = editing or Schedule()
    ctx["form_action"] = f"/schedule?edit={editing.id}" if editing else "/schedule"
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


def _parse_schedule_form(form) -> dict:
    """The Schedule fields the add/edit form carries, as a dict — shared by
    both modes so an edit can never drift from what an add accepts. Raises
    ValueError carrying the user-facing message for input that can't be
    saved; the caller turns that into a redirect.

    Deliberately returns only the *form's* fields: `id` and `enabled` are
    owned elsewhere (the schedule itself and the Enable/Disable button), so
    leaving them out is what lets an edit merge cleanly onto the existing
    schedule.
    """
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
        raise ValueError("Invalid time — not saved.") from None
    days_of_week = parse_index_list(form, "days_of_week")
    if days_of_week is None:
        raise ValueError("Invalid days — not saved.")
    days_of_week = [d for d in days_of_week if 0 <= d <= 6]

    run_clean = "run_clean" in form
    run_normalize = "run_normalize" in form
    if not run_clean and not run_normalize:
        raise ValueError("Pick at least one of Clean or Normalize — nothing saved.")

    return {
        "label": form.get("label", "").strip(),
        "hour": hour,
        "minute": minute,
        "end_hour": end_hour,
        "end_minute": end_minute,
        "days_of_week": days_of_week or list(range(7)),
        "run_clean": run_clean,
        # Empty string (the "Default" option) means no preset attached.
        "rule_preset_id": form.get("rule_preset_id", "").strip() or None,
        "auto_apply": "auto_apply" in form,
        "apply_queued": "apply_queued" in form,
        "run_normalize": run_normalize,
        "normalizer_preset_id": form.get("normalizer_preset_id", "").strip() or None,
        "normalize_auto_apply": "normalize_auto_apply" in form,
        "normalize_apply_queued": "normalize_apply_queued" in form,
    }


@web_router.post("/schedule")
async def ui_save_schedule(request: Request, session: AsyncSession = Depends(get_session)):
    """Appends a new schedule, or — with `?edit=<id>` — overwrites that one
    in place, keeping its position in the list.

    An edit deliberately preserves the schedule's `id` and its
    enabled/disabled state rather than replacing it with a fresh Schedule:
    the id is what the Enable/Disable and Delete buttons address, what a
    run's log line identifies, and what the scheduler's fired-this-minute
    bookkeeping keys off (app/scheduler.py) — so a re-created id would make
    an edit mid-window silently re-fire a schedule that had already run.
    Enabling is its own separate control, so this form doesn't touch it.
    """
    form = await request.form()
    edit_id = request.query_params.get("edit") or None
    # A rejected edit goes back to the edit form, not to the add form —
    # landing on a blank "add" after a typo would look like the schedule
    # had been replaced by an empty one.
    return_path = f"/schedule?edit={edit_id}" if edit_id else "/schedule"
    try:
        fields = _parse_schedule_form(form)
    except ValueError as exc:
        return _redirect(return_path, str(exc))

    schedules = await get_schedules(session)
    if edit_id:
        for index, existing in enumerate(schedules):
            if existing.id == edit_id:
                schedules[index] = existing.model_copy(update=fields)
                await set_schedules(session, schedules)
                return _redirect("/schedule", "Schedule updated.")
        # Deleted in another tab between GET and POST — don't silently add
        # the edits back as a brand-new schedule instead.
        return _redirect("/schedule", "That schedule no longer exists — nothing saved.")

    schedules.append(Schedule(**fields))
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
