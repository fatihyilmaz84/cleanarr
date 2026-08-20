"""Web-level tests for the normalizer's own menu item (app/normalize_web.py)
— settings round-trip, propose -> approve/skip -> queue -> run flow, and
that it renders as a genuinely separate page from Rules/Review/Queue.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import LibraryType, MediaFile, StreamRecord


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app, follow_redirects=True) as c:
        yield c


def _wait_for_idle(client: TestClient, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()
        if not jobs or jobs[0]["state"] in ("done", "error"):
            return
        time.sleep(0.02)
    raise TimeoutError("no job settled in time")


def _fake_media_tools(calls):
    """Serves ffprobe for the single eng audio track `_seed_mkv` writes, and
    records mkvpropedit calls. Applying re-probes the file rather than
    trusting cached rows (see app/normalize_service.py), so ffprobe has to
    agree with what was seeded.
    """
    payload = json.dumps(
        {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 6,
                    "tags": {"language": "eng"},
                    "disposition": {},
                }
            ],
        }
    )

    def run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


async def _seed_mkv(session_factory, path) -> int:
    async with session_factory() as session:
        mf = MediaFile(path=str(path), library_type=LibraryType.movie, size_bytes=1000, mtime=1.0)
        session.add(mf)
        await session.commit()
        await session.refresh(mf)
        session.add(StreamRecord(file_id=mf.id, stream_index=0, codec_type="audio", codec_name="ac3", language="eng"))
        await session.commit()
        return mf.id


def test_empty_state_pages_render(client: TestClient):
    for path in ["/normalize", "/normalize/queue", "/normalize/settings"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "Cleanarr" in resp.text


def test_normalize_is_a_separate_nav_item_from_rules_and_review(client: TestClient):
    page = client.get("/").text
    assert 'href="/normalize"' in page
    assert 'href="/normalize/queue"' in page
    assert 'href="/normalize/settings"' in page
    # distinct from the rule-based remover's own nav entries
    assert 'href="/review"' in page
    assert 'href="/rules"' in page


def test_save_normalizer_settings_via_form(client: TestClient):
    resp = client.post(
        "/normalize/settings",
        data={
            "naming_style": "space",
            "preferred_audio_language": "English",
            "auto_default_audio": "on",
            "commentary_title_patterns": "commentary, cast chat",
        },
    )
    assert "Normalizer settings saved" in resp.text

    settings_page = client.get("/normalize/settings").text
    assert '<option value="space" selected>' in settings_page
    assert 'value="English"' in settings_page
    assert 'value="commentary, cast chat"' in settings_page


def test_full_normalize_scan_approve_queue_run_flow(client: TestClient, tmp_path, monkeypatch):
    import asyncio

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    movie = media_dir / "Movie.mkv"
    movie.write_bytes(b"x" * 1000)

    asyncio.run(_seed_mkv(client.app.state.session_factory, movie))

    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_media_tools(calls))

    resp = client.post("/normalize/scan")
    assert resp.status_code == 200
    _wait_for_idle(client)

    review_page = client.get("/normalize")
    assert "English" in review_page.text
    assert "Add to Normalize Queue" in review_page.text

    pending_id = _first_pending_normalize_id(client)
    resp = client.post(f"/normalize/{pending_id}/approve", data={"change_index": "0"})
    assert resp.status_code == 200

    queue_page = client.get("/normalize/queue").text
    assert "Run Normalize Queue (1)" in queue_page
    assert len(calls) == 0  # nothing written yet, only queued

    resp = client.post("/normalize/queue/run")
    assert resp.status_code == 200
    _wait_for_idle(client)

    assert len(calls) == 1
    assert "name=English" in calls[0]
    assert "Nothing queued" in client.get("/normalize/queue").text


def _first_pending_normalize_id(client: TestClient) -> int:
    import re

    page = client.get("/normalize").text
    match = re.search(r'/normalize/(\d+)/approve', page)
    assert match, "no pending normalize change found on the page"
    return int(match.group(1))


def test_normalize_skip_does_not_queue(client: TestClient, tmp_path, monkeypatch):
    import asyncio

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    movie = media_dir / "Movie.mkv"
    movie.write_bytes(b"x" * 1000)
    asyncio.run(_seed_mkv(client.app.state.session_factory, movie))

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    client.post("/normalize/scan")
    _wait_for_idle(client)

    pending_id = _first_pending_normalize_id(client)
    client.post(f"/normalize/{pending_id}/skip")

    assert "Nothing to normalize" in client.get("/normalize").text
    assert "Nothing queued" in client.get("/normalize/queue").text


def test_remove_from_normalize_queue_reverts_to_pending(client: TestClient, tmp_path, monkeypatch):
    import asyncio

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    movie = media_dir / "Movie.mkv"
    movie.write_bytes(b"x" * 1000)
    asyncio.run(_seed_mkv(client.app.state.session_factory, movie))

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    client.post("/normalize/scan")
    _wait_for_idle(client)

    pending_id = _first_pending_normalize_id(client)
    client.post(f"/normalize/{pending_id}/approve", data={"change_index": "0"})
    assert "Nothing queued" not in client.get("/normalize/queue").text

    client.post(f"/normalize/queue/{pending_id}/remove")
    assert "Nothing queued" in client.get("/normalize/queue").text
    assert "Add to Normalize Queue" in client.get("/normalize").text


def test_a_default_flag_change_is_described_as_such_not_as_a_no_op(client: TestClient):
    """A change that only sets the default flag rendered as
    `subtitle: "English" -> "English"` — a pointless-looking no-op that makes
    the whole proposal look broken, when what it does is mark that track as
    the default.
    """
    from app.queries import _describe_normalization

    assert _describe_normalization(
        {"old_title": "English", "new_title": "English", "old_default": False, "new_default": True}
    ) == "set as default"

    assert _describe_normalization(
        {"old_title": "English (SDH)", "new_title": "English - SDH", "old_default": False, "new_default": False}
    ) == '"English (SDH)" → "English - SDH"'

    # Both at once reads as one line, not two contradictory ones.
    assert _describe_normalization(
        {"old_title": "eng", "new_title": "English", "old_default": False, "new_default": True}
    ) == '"eng" → "English", set as default'

    # An untitled track gaining a title still reads correctly.
    assert _describe_normalization(
        {"old_title": None, "new_title": "English", "old_default": True, "new_default": True}
    ) == '"" → "English"'


def test_a_track_the_cleaner_will_delete_says_so_rather_than_just_unchanged():
    """The normalizer deliberately skips renaming a track the drop engine is
    about to remove. That was reported as "skipped by user override" and
    rendered as plain "(unchanged)", so a library whose rules keep only a few
    languages showed every other subtitle as untouched — indistinguishable
    from the normalizer failing on those languages, which is what it looked
    like on a real 42-track file.
    """
    from app.normalizer import SKIPPED_BY_USER, SKIPPED_PENDING_REMOVAL
    from app.queries import _unchanged_note

    assert _unchanged_note({"reason": SKIPPED_PENDING_REMOVAL}) == "queued for removal"
    assert _unchanged_note({"reason": SKIPPED_BY_USER}) == "you skipped this"
    assert _unchanged_note({"reason": "already normalized"}) == "already correct"
    assert _unchanged_note({"reason": "no language tag, left untouched"}) == "no usable language tag"
    assert (
        _unchanged_note({"reason": "renaming to 'Chinese' would make this identical to another track ..."})
        == "kept, renaming would duplicate another track"
    )


def test_the_two_skip_reasons_are_not_reported_as_the_same_thing():
    from app.normalizer import SKIPPED_BY_USER, SKIPPED_PENDING_REMOVAL, apply_overrides
    from tests.fixtures import make_stream
    from app.normalizer import NormalizerConfig, normalize_streams

    streams = [make_stream(1, "subtitle", codec_name="subrip", language="dan", title="dansk")]
    proposed = normalize_streams(streams, NormalizerConfig())

    user = apply_overrides(proposed, [1])
    cleaner = apply_overrides(proposed, [1], reason=SKIPPED_PENDING_REMOVAL)

    assert user[0].reason == SKIPPED_BY_USER
    assert cleaner[0].reason == SKIPPED_PENDING_REMOVAL
    assert user[0].changed is False and cleaner[0].changed is False
