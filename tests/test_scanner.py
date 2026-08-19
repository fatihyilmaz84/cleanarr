"""Covers the scan/re-scan contract itself: unchanged files must skip the
expensive ffprobe re-run, but must NOT skip rule re-evaluation or
Sonarr/Radarr enrichment — those need to reflect the *current* config every
scan, even for a file whose bytes haven't moved since the last one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

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
async def test_rescan_updates_already_queued_change_instead_of_duplicating(tmp_path, media_dir, monkeypatch):
    """A file whose PendingChange has already been approved (queued by a
    human) must have that same row updated on rescan, never get a second
    PendingChange row created alongside it — a rescan can happen at any time
    (scheduled, or the user hits Scan Now) while something already sits in
    the Queue.
    """
    monkeypatch.setattr("app.scanner.probe_file", lambda path: (_make_probe(path)))

    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)
    media_paths = [MediaPath(path=str(media_dir), library_type="movie")]

    async with session_factory() as session:
        summary1 = await run_scan(session, media_paths, RuleConfig(audio_keep_languages=["eng"]))
    assert summary1.files_with_pending_changes == 1
    assert len(summary1.pending_change_ids) == 1
    change_id = summary1.pending_change_ids[0]

    from app.models import ChangeStatus, PendingChange

    async with session_factory() as session:
        change = await session.get(PendingChange, change_id)
        change.status = ChangeStatus.approved
        session.add(change)
        await session.commit()

    # Rescan with the same rules — the file still has a track to drop, and
    # its change is already approved/queued.
    async with session_factory() as session:
        summary2 = await run_scan(session, media_paths, RuleConfig(audio_keep_languages=["eng"]))

    # Not surfaced for auto_apply (it's approved, a human already queued it —
    # see app/scanner.py's pending_change_ids docstring), but it also must
    # not have spawned a duplicate row.
    assert summary2.pending_change_ids == []

    async with session_factory() as session:
        all_changes = (await session.exec(select(PendingChange))).all()
        assert len(all_changes) == 1
        assert all_changes[0].id == change_id
        assert all_changes[0].status == ChangeStatus.approved  # still queued, untouched by the rescan

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


@pytest.fixture
def multi_media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    for i in range(3):
        (d / f"Movie{i}.mkv").write_bytes(b"x" * 1000)
    return d


@pytest.mark.asyncio
async def test_run_scan_stops_before_starting_new_files_past_deadline(tmp_path, multi_media_dir, monkeypatch):
    monkeypatch.setattr("app.scanner.probe_file", lambda path: _make_probe(path))
    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)
    media_paths = [MediaPath(path=str(multi_media_dir), library_type="movie")]

    already_past = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with session_factory() as session:
        summary = await run_scan(session, media_paths, RuleConfig(), deadline=already_past)

    assert summary.files_total == 3  # directory walk still counts everything up front
    assert summary.files_seen == 0  # but nothing was actually processed
    assert summary.files_scanned == 0
    assert summary.stopped_early is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_scan_completes_normally_when_deadline_not_reached(tmp_path, multi_media_dir, monkeypatch):
    monkeypatch.setattr("app.scanner.probe_file", lambda path: _make_probe(path))
    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)
    media_paths = [MediaPath(path=str(multi_media_dir), library_type="movie")]

    far_future = datetime.now(timezone.utc) + timedelta(hours=1)
    async with session_factory() as session:
        summary = await run_scan(session, media_paths, RuleConfig(), deadline=far_future)

    assert summary.files_seen == 3
    assert summary.files_scanned == 3
    assert summary.stopped_early is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_scan_with_no_deadline_behaves_exactly_as_before(tmp_path, multi_media_dir, monkeypatch):
    monkeypatch.setattr("app.scanner.probe_file", lambda path: _make_probe(path))
    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)
    media_paths = [MediaPath(path=str(multi_media_dir), library_type="movie")]

    async with session_factory() as session:
        summary = await run_scan(session, media_paths, RuleConfig())  # deadline defaults to None

    assert summary.files_seen == 3
    assert summary.stopped_early is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_rescan_reprobes_a_file_whose_cached_tracks_predate_the_channels_column(
    tmp_path, media_dir, monkeypatch
):
    """`channels` was added after these rows were written, so it's NULL on an
    upgraded install — and the normalizer needs it to tell a 5.1 and a stereo
    track of the same language apart. Missing data counts as a reason to
    re-probe so it backfills itself, rather than needing a forced rescan.
    """
    probe_calls = []
    monkeypatch.setattr("app.scanner.probe_file", lambda path: (probe_calls.append(path), _make_probe(path))[1])

    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)
    media_paths = [MediaPath(path=str(media_dir), library_type="movie")]

    async with session_factory() as session:
        await run_scan(session, media_paths, RuleConfig())
    assert len(probe_calls) == 1

    # An unchanged file is normally never re-probed...
    async with session_factory() as session:
        summary = await run_scan(session, media_paths, RuleConfig())
    assert summary.files_skipped_unchanged == 1
    assert len(probe_calls) == 1

    # ...but blank out channels the way an upgraded DB would have it.
    from app.models import StreamRecord

    async with session_factory() as session:
        for record in (await session.exec(select(StreamRecord))).all():
            record.channels = None
            session.add(record)
        await session.commit()

    async with session_factory() as session:
        summary = await run_scan(session, media_paths, RuleConfig())

    assert summary.files_scanned == 1  # re-probed to backfill
    assert len(probe_calls) == 2
    async with session_factory() as session:
        audio = (
            await session.exec(select(StreamRecord).where(StreamRecord.codec_type == "audio"))
        ).all()
        assert all(r.channels is not None for r in audio)

    await engine.dispose()
