"""Covers the scan/re-scan contract itself: unchanged files must skip the
expensive ffprobe re-run, but must NOT skip rule re-evaluation or
Sonarr/Radarr enrichment — those need to reflect the *current* config every
scan, even for a file whose bytes haven't moved since the last one.
"""

from __future__ import annotations

import httpx
import pytest

from app.analyzer import MediaProbe, MediaStream
from app.arr_client import ArrClient
from app.db import init_db, make_engine, make_session_factory
from app.rules import RuleConfig
from app.scanner import run_scan
from app.settings_store import MediaPath


def _make_probe(path) -> MediaProbe:
    return MediaProbe(
        path=path,
        duration_seconds=3600.0,
        streams=[
            MediaStream(0, "video", "h264", None, None, None, True, False, False, False),
            MediaStream(1, "audio", "ac3", "eng", None, 6, True, False, False, False),
            MediaStream(2, "audio", "aac", "jpn", None, 2, False, False, False, False),
        ],
    )


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    (d / "Movie.mkv").write_bytes(b"x" * 1000)
    return d


@pytest.mark.asyncio
async def test_rescan_applies_new_rules_without_reprobing(tmp_path, media_dir, monkeypatch):
    probe_calls = []
    monkeypatch.setattr("app.scanner.probe_file", lambda path: (probe_calls.append(path), _make_probe(path))[1])

    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)
    media_paths = [MediaPath(path=str(media_dir), library_type="movie")]

    async with session_factory() as session:
        summary1 = await run_scan(session, media_paths, RuleConfig())  # inert rules, ships empty
    assert summary1.files_scanned == 1
    assert summary1.files_with_pending_changes == 0
    assert len(probe_calls) == 1

    # File on disk is untouched, but the rules are now configured. A re-scan
    # must surface the jpn audio track WITHOUT re-invoking ffprobe.
    async with session_factory() as session:
        summary2 = await run_scan(session, media_paths, RuleConfig(audio_keep_languages=["eng"]))

    assert summary2.files_scanned == 0
    assert summary2.files_skipped_unchanged == 1
    assert summary2.files_with_pending_changes == 1
    assert len(probe_calls) == 1  # still only ever probed once

    await engine.dispose()


@pytest.mark.asyncio
async def test_rescan_picks_up_newly_connected_arr_without_reprobing(tmp_path, media_dir, monkeypatch):
    monkeypatch.setattr("app.scanner.probe_file", lambda path: _make_probe(path))

    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)
    media_paths = [MediaPath(path=str(media_dir), library_type="movie")]

    async with session_factory() as session:
        await run_scan(session, media_paths, RuleConfig(), arr_client=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "title": "Example Movie",
                    "year": 2020,
                    "path": str(media_dir),
                    "images": [],
                    "movieFile": {"relativePath": "Movie.mkv"},
                    "originalLanguage": {"id": 1, "name": "Japanese"},
                }
            ],
        )

    arr_client = ArrClient(
        radarr_url="http://radarr:7878",
        radarr_api_key="key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with session_factory() as session:
        summary = await run_scan(session, media_paths, RuleConfig(), arr_client=arr_client)

    assert summary.files_skipped_unchanged == 1
    assert summary.files_scanned == 0

    from sqlmodel import select

    from app.models import MediaFile

    async with session_factory() as session:
        media_file = (await session.exec(select(MediaFile))).one()
        assert media_file.display_title == "Example Movie"
        assert media_file.original_language == "Japanese"

    await engine.dispose()
