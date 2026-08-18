"""Covers app/normalize_service.py: proposing normalizations from already-
scanned data (no ffprobe), applying them via mkvpropedit, and the
interaction with app/rules.py's drop engine — a track the rule engine
currently proposes dropping must be excluded from normalization (TODO.md
#7's "Architecture" note).
"""

from __future__ import annotations

import subprocess

import pytest
from sqlmodel import select

from app.db import init_db, make_engine, make_session_factory
from app.models import ChangeStatus, LibraryType, MediaFile, NormalizationChange, PendingChange, StreamRecord
from app.normalize_service import apply_normalization_change, propose_normalizations
from app.normalizer import NormalizerConfig


@pytest.fixture
async def session_factory(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    factory = make_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_file(session_factory, path: str, streams: list[dict]) -> int:
    async with session_factory() as session:
        mf = MediaFile(path=path, library_type=LibraryType.movie, size_bytes=1000, mtime=1.0)
        session.add(mf)
        await session.commit()
        await session.refresh(mf)
        for s in streams:
            session.add(StreamRecord(file_id=mf.id, **s))
        await session.commit()
        return mf.id


@pytest.mark.asyncio
async def test_propose_normalizations_creates_change_for_untitled_track(session_factory, tmp_path):
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [
            {"stream_index": 0, "codec_type": "video", "codec_name": "h264"},
            {"stream_index": 1, "codec_type": "audio", "codec_name": "ac3", "language": "eng"},
        ],
    )

    async with session_factory() as session:
        summary = await propose_normalizations(session, NormalizerConfig())

    assert summary.files_considered == 1
    assert summary.files_with_changes == 1

    async with session_factory() as session:
        change = (await session.exec(select(NormalizationChange))).one()
        assert change.status == ChangeStatus.pending
        assert any(p["new_title"] == "English" for p in change.proposed)


@pytest.mark.asyncio
async def test_propose_normalizations_skips_already_normalized_file(session_factory, tmp_path):
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng", "title": "English"}],
    )

    async with session_factory() as session:
        summary = await propose_normalizations(session, NormalizerConfig())

    assert summary.files_with_changes == 0

    async with session_factory() as session:
        assert (await session.exec(select(NormalizationChange))).all() == []


@pytest.mark.asyncio
async def test_propose_normalizations_excludes_track_marked_for_removal(session_factory, tmp_path):
    """Core interaction rule: a track app/rules.py's drop engine currently
    proposes removing must be excluded from normalization — there's nothing
    left to retitle once it's gone.
    """
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    file_id = await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [
            {"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"},
            {"stream_index": 1, "codec_type": "audio", "codec_name": "aac", "language": "jpn"},
        ],
    )

    # Simulate the rule engine having already proposed dropping the jpn track.
    async with session_factory() as session:
        session.add(
            PendingChange(
                file_id=file_id,
                status=ChangeStatus.pending,
                proposed=[
                    {"index": 0, "type": "audio", "codec": "ac3", "language": "eng", "title": None, "keep": True, "reason": "kept"},
                    {"index": 1, "type": "audio", "codec": "aac", "language": "jpn", "title": None, "keep": False, "reason": "dropped"},
                ],
            )
        )
        await session.commit()

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())

    async with session_factory() as session:
        change = (await session.exec(select(NormalizationChange))).one()
        indices = {p["index"] for p in change.proposed}
        assert indices == {0}  # index 1 (marked for removal) excluded entirely


@pytest.mark.asyncio
async def test_propose_normalizations_includes_track_overridden_back_to_keep(session_factory, tmp_path):
    """The reverse case: a track the drop engine would remove, but the user
    already force-kept via an override (app/rules.py::apply_overrides) —
    since it survives the remux, it's fair game for normalization again.
    """
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    file_id = await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "aac", "language": "jpn"}],
    )

    async with session_factory() as session:
        session.add(
            PendingChange(
                file_id=file_id,
                status=ChangeStatus.pending,
                proposed=[{"index": 0, "type": "audio", "codec": "aac", "language": "jpn", "title": None, "keep": False, "reason": "dropped"}],
                overrides=[0],  # user unchecked the drop -> force-kept
            )
        )
        await session.commit()

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())

    async with session_factory() as session:
        change = (await session.exec(select(NormalizationChange))).one_or_none()
        assert change is not None
        assert {p["index"] for p in change.proposed} == {0}


def _fake_mkvpropedit(calls):
    def run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


@pytest.mark.asyncio
async def test_apply_normalization_change_runs_mkvpropedit_and_updates_cache(session_factory, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_mkvpropedit(calls))

    (tmp_path / "Movie.mkv").write_bytes(b"x")
    file_id = await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}],
    )

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())
        change = (await session.exec(select(NormalizationChange))).one()
        change_id = change.id

    async with session_factory() as session:
        result = await apply_normalization_change(session, change_id, NormalizerConfig())

    assert result.success is True
    assert result.tracks_updated == 1
    assert len(calls) == 1
    assert "name=English" in calls[0]

    async with session_factory() as session:
        record = (await session.exec(select(StreamRecord).where(StreamRecord.file_id == file_id))).one()
        assert record.title == "English"  # cache updated so a future pass sees it as already-normalized

        updated_change = await session.get(NormalizationChange, change_id)
        assert updated_change.status == ChangeStatus.applied


@pytest.mark.asyncio
async def test_apply_normalization_change_rejects_non_mkv(session_factory, tmp_path):
    (tmp_path / "Movie.mp4").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mp4"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "aac", "language": "eng"}],
    )

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())
        change = (await session.exec(select(NormalizationChange))).one()
        change_id = change.id

    async with session_factory() as session:
        result = await apply_normalization_change(session, change_id, NormalizerConfig())

    assert result.success is False
    assert "MKV" in result.message


@pytest.mark.asyncio
async def test_apply_normalization_change_respects_overrides(session_factory, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_mkvpropedit(calls))

    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [
            {"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"},
            {"stream_index": 1, "codec_type": "subtitle", "codec_name": "subrip", "language": "eng"},
        ],
    )

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())
        change = (await session.exec(select(NormalizationChange))).one()
        change.overrides = [1]  # skip the subtitle track
        session.add(change)
        await session.commit()
        change_id = change.id

    async with session_factory() as session:
        result = await apply_normalization_change(session, change_id, NormalizerConfig())

    assert result.success is True
    assert result.tracks_updated == 1  # only the audio track was written
    assert "track:s1" not in calls[0]
    assert "track:a1" in calls[0]
