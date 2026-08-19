"""Covers saved rule/normalizer presets (app/settings_store.py) and the
invariant that makes them safe: a change is always applied with the same
config that proposed it.

That invariant matters because app/apply.py and app/normalize_service.py
both deliberately re-decide from scratch at apply time rather than trusting
the cached `proposed` — so if the config used at apply time differed from
the one used at propose time, what actually got dropped/renamed would not
match what the user was shown and approved.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, make_engine, make_session_factory
from app.main import create_app
from app.rules import RuleConfig
from app.settings_store import (
    NormalizerPreset,
    RulePreset,
    get_rule_presets,
    resolve_normalizer_config,
    resolve_rule_config,
    set_normalizer_presets,
    set_rule_config,
    set_rule_presets,
)


@pytest.fixture
async def session_factory(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    factory = make_session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_rule_config_returns_default_when_no_preset_id(session_factory):
    async with session_factory() as session:
        await set_rule_config(session, RuleConfig(audio_keep_languages=["eng"]))

    async with session_factory() as session:
        assert (await resolve_rule_config(session, None)).audio_keep_languages == ["eng"]


@pytest.mark.asyncio
async def test_resolve_rule_config_returns_the_named_preset(session_factory):
    preset = RulePreset(name="Korean", config=RuleConfig(audio_keep_languages=["kor"]))
    async with session_factory() as session:
        await set_rule_config(session, RuleConfig(audio_keep_languages=["eng"]))
        await set_rule_presets(session, [preset])

    async with session_factory() as session:
        assert (await resolve_rule_config(session, preset.id)).audio_keep_languages == ["kor"]


@pytest.mark.asyncio
async def test_resolve_rule_config_falls_back_to_default_for_a_deleted_preset(session_factory):
    """A schedule or an already-queued change can outlive the preset it
    points at. That must degrade to Default, never raise — otherwise
    deleting a preset would wedge scheduled runs and block queued changes
    from ever being applied.
    """
    async with session_factory() as session:
        await set_rule_config(session, RuleConfig(audio_keep_languages=["eng"]))
        await set_rule_presets(session, [])

    async with session_factory() as session:
        resolved = await resolve_rule_config(session, "an-id-that-no-longer-exists")
        assert resolved.audio_keep_languages == ["eng"]


@pytest.mark.asyncio
async def test_resolve_normalizer_config_falls_back_to_default_for_a_deleted_preset(session_factory):
    async with session_factory() as session:
        await set_normalizer_presets(session, [])

    async with session_factory() as session:
        resolved = await resolve_normalizer_config(session, "gone")
        assert resolved.naming_style == "dash"  # the NormalizerConfig default


@pytest.mark.asyncio
async def test_normalizer_preset_round_trips_its_config(session_factory):
    from app.normalizer import NormalizerConfig

    preset = NormalizerPreset(name="Plex", config=NormalizerConfig(naming_style="space"))
    async with session_factory() as session:
        await set_normalizer_presets(session, [preset])

    async with session_factory() as session:
        assert (await resolve_normalizer_config(session, preset.id)).naming_style == "space"


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app, follow_redirects=True) as c:
        yield c


def test_create_edit_and_delete_a_rule_preset_via_the_ui(client: TestClient):
    client.post("/rules", data={"audio_keep_languages": "English"})

    resp = client.post("/rules/presets", data={"name": "Weekly deep clean"})
    assert "created from your current rules" in resp.text
    assert "Weekly deep clean" in resp.text

    preset_id = _first_rule_preset_id(client)

    # Editing the preset must not touch Default.
    client.post(f"/rules?preset={preset_id}", data={"audio_keep_languages": "Korean"})

    async def _configs():
        async with client.app.state.session_factory() as session:
            default = await resolve_rule_config(session, None)
            preset = await resolve_rule_config(session, preset_id)
            return default.audio_keep_languages, preset.audio_keep_languages

    import asyncio

    default_langs, preset_langs = asyncio.run(_configs())
    assert "eng" in default_langs and "kor" not in default_langs
    assert "kor" in preset_langs and "eng" not in preset_langs

    resp = client.post(f"/rules/presets/{preset_id}/delete")
    assert "falls back to Default" in resp.text
    assert "Weekly deep clean" not in resp.text


def _first_rule_preset_id(client: TestClient) -> str:
    import asyncio

    async def _get():
        async with client.app.state.session_factory() as session:
            return (await get_rule_presets(session))[0].id

    return asyncio.run(_get())


def test_rule_preset_names_must_be_unique_and_non_empty(client: TestClient):
    assert "needs a name" in client.post("/rules/presets", data={"name": "  "}).text

    client.post("/rules/presets", data={"name": "Nightly"})
    resp = client.post("/rules/presets", data={"name": "nightly"})  # case-insensitive
    assert "already exists" in resp.text


def test_editing_a_preset_that_was_deleted_does_not_write_into_default(client: TestClient):
    """Guards a genuinely reachable two-tab race: open a preset's edit form,
    delete the preset elsewhere, then submit. Writing those edits into the
    Default rules would silently change what every unscheduled scan does.
    """
    client.post("/rules", data={"audio_keep_languages": "English"})
    client.post("/rules/presets", data={"name": "Temp"})
    preset_id = _first_rule_preset_id(client)
    client.post(f"/rules/presets/{preset_id}/delete")

    resp = client.post(f"/rules?preset={preset_id}", data={"audio_keep_languages": "Korean"})
    assert "no longer exists" in resp.text

    import asyncio

    async def _default():
        async with client.app.state.session_factory() as session:
            return (await resolve_rule_config(session, None)).audio_keep_languages

    assert "eng" in asyncio.run(_default())  # untouched


def test_normalizer_preset_create_and_delete_via_the_ui(client: TestClient):
    resp = client.post("/normalize/settings/presets", data={"name": "Plex-friendly"})
    assert "created from your current settings" in resp.text
    assert "Plex-friendly" in resp.text

    settings_page = client.get("/normalize/settings").text
    assert "Plex-friendly" in settings_page


def test_a_change_is_applied_with_the_preset_that_proposed_it_not_the_default(tmp_path, monkeypatch):
    """The core safety property of attaching rules to a schedule.

    The two configs disagree on every track, deliberately:
      - Default keeps eng audio + eng subs   -> would drop the jpn audio
      - Preset keeps eng+jpn audio, fra subs -> drops the eng subtitle

    A scan run under the preset must both propose AND apply under that
    preset. If apply fell back to Default, ffmpeg would strip the Japanese
    audio track that the user was never shown as droppable, and keep the
    subtitle they were told would go.
    """
    import asyncio
    import subprocess

    from app.actions import submit_apply_job, submit_scan_job
    from tests.test_api import _fake_subprocess_run

    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "Movie (2020).mkv").write_bytes(b"x" * 1000)

    app = create_app(tmp_path / "test.db")
    with TestClient(app) as c:
        # Default: English only. Preset: English + Japanese.
        c.put("/api/settings/rules", json={"audio_keep_languages": ["eng"], "subtitle_keep_languages": ["eng"]})
        c.put("/api/settings/media-paths", json=[{"path": str(media_dir), "library_type": "movie"}])

        preset = RulePreset(
            name="Keep Japanese",
            # subtitle keep-list is non-empty on purpose: an empty one means
            # "keep every subtitle", which would propose nothing at all.
            config=RuleConfig(audio_keep_languages=["eng", "jpn"], subtitle_keep_languages=["fra"]),
        )

        async def _save_preset():
            async with app.state.session_factory() as session:
                await set_rule_presets(session, [preset])

        asyncio.run(_save_preset())

        job_id = submit_scan_job(
            app.state.session_factory, app.state.job_manager, rule_preset_id=preset.id
        )
        _wait_for_job(c, job_id)

        pending = c.get("/api/review", params={"status": "pending"}).json()
        assert len(pending) == 1
        change = pending[0]
        dropped_langs = {p["language"] for p in change["proposed"] if not p["keep"]}
        # Proposed under the preset: jpn audio survives, eng subtitle goes.
        assert dropped_langs == {"eng"}

        async def _stamped_preset_id():
            async with app.state.session_factory() as session:
                from app.models import PendingChange

                row = await session.get(PendingChange, change["id"])
                return row.rule_preset_id

        assert asyncio.run(_stamped_preset_id()) == preset.id

        # Apply it through the normal path, which resolves the config per row.
        apply_job = submit_apply_job(app.state.session_factory, app.state.job_manager, [change["id"]])
        _wait_for_job(c, apply_job)

        history = c.get("/api/history").json()
        assert len(history) == 1
        removed = {s["language"] for s in history[0]["streams_removed"]}
        # The Japanese track Default would have dropped is untouched.
        assert removed == {"eng"}
        assert "jpn" not in removed


def test_schedule_form_attaches_presets_for_both_systems(client: TestClient):
    import asyncio

    from app.settings_store import get_normalizer_presets, get_schedules

    client.post("/rules/presets", data={"name": "Weekly deep clean"})
    client.post("/normalize/settings/presets", data={"name": "Plex-friendly"})

    rule_preset_id = _first_rule_preset_id(client)

    async def _norm_preset_id():
        async with client.app.state.session_factory() as session:
            return (await get_normalizer_presets(session))[0].id

    normalizer_preset_id = asyncio.run(_norm_preset_id())

    resp = client.post(
        "/schedule",
        data={
            "label": "Sunday deep pass",
            "hour": "3",
            "minute": "0",
            "run_clean": "on",
            "rule_preset_id": rule_preset_id,
            "run_normalize": "on",
            "normalizer_preset_id": normalizer_preset_id,
            "normalize_apply_queued": "on",
        },
    )
    assert "Schedule added" in resp.text
    # Both attachments are visible at a glance on the schedule list.
    assert "CLEAN · Weekly deep clean" in resp.text
    assert "NORMALIZE · Plex-friendly" in resp.text

    async def _saved():
        async with client.app.state.session_factory() as session:
            return (await get_schedules(session))[0]

    saved = asyncio.run(_saved())
    assert saved.run_clean is True
    assert saved.rule_preset_id == rule_preset_id
    assert saved.run_normalize is True
    assert saved.normalizer_preset_id == normalizer_preset_id
    assert saved.normalize_apply_queued is True
    assert saved.normalize_auto_apply is False


def test_schedule_with_no_task_selected_is_rejected(client: TestClient):
    resp = client.post("/schedule", data={"hour": "4", "minute": "0"})  # neither box checked
    assert "Pick at least one" in resp.text
    assert "No schedules yet" in resp.text


def test_schedule_list_flags_a_preset_that_was_deleted(client: TestClient):
    """The schedule keeps working (it falls back to Default), but the list
    should say so rather than showing a stale name or a bare id.
    """
    client.post("/rules/presets", data={"name": "Temporary"})
    preset_id = _first_rule_preset_id(client)
    client.post("/schedule", data={"hour": "4", "minute": "0", "run_clean": "on", "rule_preset_id": preset_id})
    client.post(f"/rules/presets/{preset_id}/delete")

    page = client.get("/schedule").text
    assert "deleted preset → Default" in page


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise TimeoutError(f"job {job_id} did not finish in time")
