"""Server-rendered UI for the track metadata normalizer — a separate menu
item from Rules/Review/Queue (see TODO.md #7's "Architecture" note), but
reusing their propose -> approve -> apply *pattern* and this app's plain-
HTML-forms style. Mirrors app/web.py's shape closely; kept in its own
router/file rather than folded into web.py, which is already large.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from app.actions import submit_normalize_apply_job, submit_normalize_scan_job
from app.deps import get_session
from app.models import ChangeStatus, NormalizationChange
from app.normalizer import NormalizerConfig
from app.queries import list_normalize_items, normalize_stats
from app.settings_store import (
    NormalizerPreset,
    get_normalizer_config,
    get_normalizer_presets,
    set_normalizer_config,
    set_normalizer_presets,
)
from app.web import _base_context, _paginate, _redirect, _split_csv, templates

normalize_router = APIRouter()


@normalize_router.get("/normalize/settings")
async def ui_normalize_settings(request: Request, session: AsyncSession = Depends(get_session)):
    """Same one-form-two-targets shape as app/web.py::ui_rules — no
    `?preset=` edits the Default normalizer config, one edits that saved
    NormalizerPreset.
    """
    ctx = await _base_context(request, session)
    presets = await get_normalizer_presets(session)
    preset_id = request.query_params.get("preset") or None
    editing = next((p for p in presets if p.id == preset_id), None)

    ctx["config"] = editing.config if editing else await get_normalizer_config(session)
    ctx["presets"] = presets
    ctx["editing_preset"] = editing
    ctx["form_action"] = f"/normalize/settings?preset={editing.id}" if editing else "/normalize/settings"
    return templates.TemplateResponse(request, "normalize_settings.html", ctx)


@normalize_router.post("/normalize/settings/presets")
async def ui_add_normalizer_preset(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return _redirect("/normalize/settings", "Preset needs a name — not saved.")

    presets = await get_normalizer_presets(session)
    if any(p.name.lower() == name.lower() for p in presets):
        return _redirect("/normalize/settings", f"A preset named '{name}' already exists.")

    presets.append(NormalizerPreset(name=name, config=await get_normalizer_config(session)))
    await set_normalizer_presets(session, presets)
    return _redirect("/normalize/settings", f"Preset '{name}' created from your current settings.")


@normalize_router.post("/normalize/settings/presets/{preset_id}/delete")
async def ui_delete_normalizer_preset(preset_id: str, session: AsyncSession = Depends(get_session)):
    presets = await get_normalizer_presets(session)
    await set_normalizer_presets(session, [p for p in presets if p.id != preset_id])
    return _redirect("/normalize/settings", "Preset deleted — anything using it falls back to Default settings.")


@normalize_router.post("/normalize/settings")
async def ui_save_normalize_settings(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    config = NormalizerConfig(
        naming_style="space" if form.get("naming_style") == "space" else "dash",
        auto_default_audio="auto_default_audio" in form,
        auto_default_subtitle="auto_default_subtitle" in form,
        preferred_audio_language=form.get("preferred_audio_language", "").strip(),
        preferred_subtitle_language=form.get("preferred_subtitle_language", "").strip(),
        forced_equivalents_enabled="forced_equivalents_enabled" in form,
        detect_subtitle_language="detect_subtitle_language" in form,
        forced_title_patterns=_split_csv(form.get("forced_title_patterns", "")),
        forced_equivalent_patterns=_split_csv(form.get("forced_equivalent_patterns", "")),
        commentary_title_patterns=_split_csv(form.get("commentary_title_patterns", "")),
        hearing_impaired_title_patterns=_split_csv(form.get("hearing_impaired_title_patterns", "")),
        cc_title_patterns=_split_csv(form.get("cc_title_patterns", "")),
        audio_description_title_patterns=_split_csv(form.get("audio_description_title_patterns", "")),
        original_title_patterns=_split_csv(form.get("original_title_patterns", "")),
        dubbed_title_patterns=_split_csv(form.get("dubbed_title_patterns", "")),
    )
    preset_id = request.query_params.get("preset") or None
    if preset_id:
        presets = await get_normalizer_presets(session)
        for preset in presets:
            if preset.id == preset_id:
                preset.config = config
                await set_normalizer_presets(session, presets)
                return _redirect(f"/normalize/settings?preset={preset_id}", f"Preset '{preset.name}' saved.")
        return _redirect("/normalize/settings", "That preset no longer exists — nothing saved.")

    await set_normalizer_config(session, config)
    return _redirect("/normalize/settings", "Normalizer settings saved.")


@normalize_router.get("/normalize")
async def ui_normalize(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    all_items = await list_normalize_items(session, ChangeStatus.pending)
    page_items, pagination = _paginate(all_items, request)
    ctx["items"] = page_items
    ctx["total_count"] = len(all_items)
    ctx["pagination"] = pagination
    return templates.TemplateResponse(request, "normalize.html", ctx)


@normalize_router.post("/normalize/scan")
async def ui_trigger_normalize_scan(request: Request):
    submit_normalize_scan_job(request.app.state.session_factory, request.app.state.job_manager)
    referer = request.headers.get("referer", "/normalize")
    return _redirect(referer, "Scanning for normalizable tracks — check back in a moment.")


@normalize_router.post("/normalize/{change_id}/approve")
async def ui_normalize_approve(change_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    change = await session.get(NormalizationChange, change_id)
    if change is not None:
        form = await request.form()
        # Every proposed change renders as a checked-by-default checkbox
        # (see normalize.html) — whatever stayed checked gets applied;
        # anything unchecked becomes an override that leaves that track
        # untouched instead.
        confirmed = {int(v) for v in form.getlist("change_index")}
        proposed_changed = {p["index"] for p in change.proposed if p["changed"]}
        change.overrides = sorted(proposed_changed - confirmed)
        change.status = ChangeStatus.approved
        session.add(change)
        await session.commit()
    return _redirect("/normalize", "Added to Normalize Queue.")


@normalize_router.post("/normalize/{change_id}/skip")
async def ui_normalize_skip(change_id: int, session: AsyncSession = Depends(get_session)):
    change = await session.get(NormalizationChange, change_id)
    if change is not None:
        change.status = ChangeStatus.skipped
        session.add(change)
        await session.commit()
    return _redirect("/normalize")


@normalize_router.get("/normalize/queue")
async def ui_normalize_queue(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _base_context(request, session)
    all_items = await list_normalize_items(session, ChangeStatus.approved)
    page_items, pagination = _paginate(all_items, request)
    ctx["items"] = page_items
    ctx["total_count"] = len(all_items)
    ctx["pagination"] = pagination
    return templates.TemplateResponse(request, "normalize_queue.html", ctx)


@normalize_router.post("/normalize/queue/run")
async def ui_run_normalize_queue(request: Request, session: AsyncSession = Depends(get_session)):
    queued = await list_normalize_items(session, ChangeStatus.approved)
    change_ids = [i["id"] for i in queued]
    if change_ids:
        submit_normalize_apply_job(request.app.state.session_factory, request.app.state.job_manager, change_ids)
        return _redirect("/normalize/queue", f"Running {len(change_ids)} queued normalization(s) — check back in a moment.")
    return _redirect("/normalize/queue", "Normalize Queue is empty.")


@normalize_router.post("/normalize/queue/{change_id}/remove")
async def ui_remove_from_normalize_queue(change_id: int, session: AsyncSession = Depends(get_session)):
    change = await session.get(NormalizationChange, change_id)
    if change is not None and change.status == ChangeStatus.approved:
        change.status = ChangeStatus.pending
        change.overrides = None
        session.add(change)
        await session.commit()
    return _redirect("/normalize/queue", "Removed from Normalize Queue — back in Normalize.")
