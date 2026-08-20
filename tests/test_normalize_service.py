"""Covers app/normalize_service.py: proposing normalizations from already-
scanned data (no ffprobe), applying them via mkvpropedit, and the
interaction with app/rules.py's drop engine — a track the rule engine
currently proposes dropping must be excluded from normalization (TODO.md
#7's "Architecture" note).
"""

from __future__ import annotations

import json
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
        by_index = {p["index"]: p for p in change.proposed}
        # Both tracks are present — selectors are computed from the full
        # physical track list, never a trimmed one (see
        # _dropped_indices_from_change's docstring in app/normalize_service.py)
        # — but the track marked for removal is excluded from the actual
        # *output*: it's left unchanged rather than retitled.
        assert set(by_index) == {0, 1}
        assert by_index[0]["changed"] is True
        assert by_index[1]["changed"] is False


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


@pytest.mark.asyncio
async def test_propose_normalizations_updates_an_approved_change_instead_of_duplicating(session_factory, tmp_path):
    """A NormalizationChange already queued (approved) must be updated in
    place on a re-propose, never left alone while a second row is created
    for the same file — the duplicate-queue-entry bug that was fixed in
    app/scanner.py had the same shape here.
    """
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}],
    )

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())
        change = (await session.exec(select(NormalizationChange))).one()
        change.status = ChangeStatus.approved
        session.add(change)
        await session.commit()
        change_id = change.id

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())

    async with session_factory() as session:
        all_changes = (await session.exec(select(NormalizationChange))).all()
        assert len(all_changes) == 1
        assert all_changes[0].id == change_id
        assert all_changes[0].status == ChangeStatus.approved  # still queued


@pytest.mark.asyncio
async def test_propose_normalizations_stops_at_the_deadline_between_files(session_factory, tmp_path):
    from datetime import datetime, timedelta, timezone

    for i in range(3):
        (tmp_path / f"Movie{i}.mkv").write_bytes(b"x")
        await _seed_file(
            session_factory,
            str(tmp_path / f"Movie{i}.mkv"),
            [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}],
        )

    already_past = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with session_factory() as session:
        summary = await propose_normalizations(session, NormalizerConfig(), deadline=already_past)

    assert summary.stopped_early is True
    assert summary.files_considered == 0  # checked before the first file, nothing started

    # A generous deadline lets the whole pass finish normally.
    async with session_factory() as session:
        summary = await propose_normalizations(
            session, NormalizerConfig(), deadline=datetime.now(timezone.utc) + timedelta(hours=1)
        )
    assert summary.stopped_early is False
    assert summary.files_considered == 3


@pytest.mark.asyncio
async def test_propose_normalizations_stamps_and_reports_its_preset_and_change_ids(session_factory, tmp_path):
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}],
    )

    async with session_factory() as session:
        summary = await propose_normalizations(session, NormalizerConfig(), normalizer_preset_id="preset-abc")

    async with session_factory() as session:
        change = (await session.exec(select(NormalizationChange))).one()
        assert change.normalizer_preset_id == "preset-abc"
        # change_ids scopes a scheduled auto-apply to this pass's own output.
        assert summary.change_ids == [change.id]


def _ffprobe_payload(streams: list[dict]) -> str:
    """ffprobe JSON matching the same stream dicts `_seed_file` writes to the
    DB — applying re-probes the file rather than trusting the cached rows
    (see app/normalize_service.py), so both have to agree in tests.
    """
    return json.dumps(
        {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "index": s["stream_index"],
                    "codec_type": s["codec_type"],
                    "codec_name": s.get("codec_name", "unknown"),
                    "channels": s.get("channels"),
                    "tags": {
                        k: v
                        for k, v in (("language", s.get("language")), ("title", s.get("title")))
                        if v is not None
                    },
                    "disposition": {},
                }
                for s in streams
            ],
        }
    )


def _fake_media_tools(calls, streams):
    """Serves ffprobe from `streams`; records every mkvpropedit call."""

    def run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout=_ffprobe_payload(streams), stderr="")
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


@pytest.mark.asyncio
async def test_apply_normalization_change_runs_mkvpropedit_and_updates_cache(session_factory, tmp_path, monkeypatch):
    calls = []
    streams = [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}]
    monkeypatch.setattr(subprocess, "run", _fake_media_tools(calls, streams))

    (tmp_path / "Movie.mkv").write_bytes(b"x")
    file_id = await _seed_file(session_factory, str(tmp_path / "Movie.mkv"), streams)

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
async def test_propose_skips_containers_mkvpropedit_cannot_edit(session_factory, tmp_path):
    """mkvpropedit is Matroska-only, so proposing for an .mp4 just queues an
    item that apply is guaranteed to reject — and since a `failed` row isn't
    matched on the next pass, every pass would add another doomed row.
    """
    (tmp_path / "Movie.mp4").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mp4"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "aac", "language": "eng"}],
    )

    async with session_factory() as session:
        summary = await propose_normalizations(session, NormalizerConfig())

    assert summary.files_unsupported_container == 1
    assert summary.files_with_changes == 0
    async with session_factory() as session:
        assert (await session.exec(select(NormalizationChange))).all() == []


@pytest.mark.asyncio
async def test_propose_clears_a_stale_proposal_for_a_now_unsupported_file(session_factory, tmp_path):
    """Rows left over from before non-MKV files were filtered out (they exist
    on already-deployed installs) get cleaned up rather than lingering as
    permanently-unappliable queue entries.
    """
    (tmp_path / "Movie.mp4").write_bytes(b"x")
    file_id = await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mp4"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "aac", "language": "eng"}],
    )
    async with session_factory() as session:
        session.add(NormalizationChange(file_id=file_id, status=ChangeStatus.pending, proposed=[]))
        await session.commit()

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())

    async with session_factory() as session:
        assert (await session.exec(select(NormalizationChange))).all() == []


@pytest.mark.asyncio
async def test_apply_normalization_change_rejects_non_mkv(session_factory, tmp_path):
    """Defence in depth: propose filters these out now, but a row queued
    before that filter existed must still be refused rather than attempted.
    """
    (tmp_path / "Movie.mp4").write_bytes(b"x")
    file_id = await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mp4"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "aac", "language": "eng"}],
    )
    async with session_factory() as session:
        change = NormalizationChange(file_id=file_id, status=ChangeStatus.approved, proposed=[])
        session.add(change)
        await session.commit()
        await session.refresh(change)
        change_id = change.id

    async with session_factory() as session:
        result = await apply_normalization_change(session, change_id, NormalizerConfig())

    assert result.success is False
    assert "MKV" in result.message


@pytest.mark.asyncio
async def test_apply_normalization_change_respects_overrides(session_factory, tmp_path, monkeypatch):
    calls = []
    streams = [
        {"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"},
        {"stream_index": 1, "codec_type": "subtitle", "codec_name": "subrip", "language": "eng"},
    ]
    monkeypatch.setattr(subprocess, "run", _fake_media_tools(calls, streams))

    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(session_factory, str(tmp_path / "Movie.mkv"), streams)

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


@pytest.mark.asyncio
async def test_skipping_a_file_is_not_undone_by_the_next_pass(session_factory, tmp_path):
    """"Skip" used to be pointless: the skipped row wasn't matched on the
    next pass, so a brand-new pending row was created and the file came
    straight back.
    """
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}],
    )

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())
    async with session_factory() as session:
        change = (await session.exec(select(NormalizationChange))).one()
        change.status = ChangeStatus.skipped
        session.add(change)
        await session.commit()

    async with session_factory() as session:
        summary = await propose_normalizations(session, NormalizerConfig())

    assert summary.files_with_changes == 0
    async with session_factory() as session:
        rows = (await session.exec(select(NormalizationChange))).all()
        assert [r.status for r in rows] == [ChangeStatus.skipped]


@pytest.mark.asyncio
async def test_a_skipped_file_comes_back_when_the_suggestion_itself_changes(session_factory, tmp_path):
    """The other half of skip durability — declining one suggestion must not
    mute the file forever if the config later proposes something different.
    """
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}],
    )

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())
    async with session_factory() as session:
        change = (await session.exec(select(NormalizationChange))).one()
        change.status = ChangeStatus.skipped
        session.add(change)
        await session.commit()

    # Same file, different naming scheme -> a genuinely different proposal.
    async with session_factory() as session:
        await propose_normalizations(
            session, NormalizerConfig(auto_default_audio=True, preferred_audio_language="English")
        )

    async with session_factory() as session:
        rows = (await session.exec(select(NormalizationChange))).all()
        assert [r.status for r in rows] == [ChangeStatus.pending]


@pytest.mark.asyncio
async def test_apply_reprobes_and_does_not_trust_stale_cached_tracks(session_factory, tmp_path, monkeypatch):
    """mkvpropedit selectors are positional, so applying against a stale
    cache retitles whatever now sits in that position. The file here has been
    replaced since the scan (an *arr upgrade): the cache says one eng audio
    track, the file actually has jpn first and eng second.
    """
    calls = []
    cached = [{"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "eng"}]
    on_disk = [
        {"stream_index": 0, "codec_type": "audio", "codec_name": "ac3", "language": "jpn"},
        {"stream_index": 1, "codec_type": "audio", "codec_name": "ac3", "language": "eng"},
    ]
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    file_id = await _seed_file(session_factory, str(tmp_path / "Movie.mkv"), cached)

    monkeypatch.setattr(subprocess, "run", _fake_media_tools(calls, cached))
    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())
        change_id = (await session.exec(select(NormalizationChange))).one().id

    # The file changes underneath us between propose and apply.
    monkeypatch.setattr(subprocess, "run", _fake_media_tools(calls, on_disk))
    async with session_factory() as session:
        result = await apply_normalization_change(session, change_id, NormalizerConfig())

    assert result.success is True
    # Titles follow the file's real layout, not the cache: a1 is Japanese.
    assert "name=日本語" in calls[0]
    assert "name=English" in calls[0]

    async with session_factory() as session:
        records = (
            await session.exec(select(StreamRecord).where(StreamRecord.file_id == file_id).order_by(StreamRecord.stream_index))
        ).all()
        # Cache rebuilt from the probe, so it now reflects reality.
        assert [(r.stream_index, r.language, r.title) for r in records] == [
            (0, "jpn", "日本語"),
            (1, "eng", "English"),
        ]


@pytest.mark.asyncio
async def test_an_unlabelled_subtitle_is_decoded_once_and_the_result_remembered(session_factory, tmp_path, monkeypatch):
    """Reading a track's text costs an ffmpeg decode, so the answer is stored
    on the StreamRecord. The negative answer matters just as much: without
    persisting "looked, couldn't tell", every pass would re-decode the same
    unidentifiable tracks forever.
    """
    import app.normalize_service as svc

    (tmp_path / "Movie.mkv").write_bytes(b"x")
    file_id = await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [
            {"stream_index": 0, "codec_type": "video", "codec_name": "h264"},
            # no language, no title — nothing to name it from
            {"stream_index": 1, "codec_type": "subtitle", "codec_name": "subrip"},
            # unreadable text, so detection will decline
            {"stream_index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
            # bitmap: never worth decoding, it would need OCR
            {"stream_index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            # already labelled: nothing to fill in
            {"stream_index": 4, "codec_type": "subtitle", "codec_name": "subrip", "language": "eng"},
        ],
    )

    decoded = []

    def fake_extract(path, stream_index, **kwargs):
        decoded.append(stream_index)
        return "Hoe is het gegaan? Je ruikt lekker. " * 20 if stream_index == 1 else "[MUSIC] ♪♪♪ " * 20

    monkeypatch.setattr(svc, "extract_subtitle_text", fake_extract)
    config = NormalizerConfig(detect_subtitle_language=True)

    async with session_factory() as session:
        await propose_normalizations(session, config)

    assert sorted(decoded) == [1, 2]  # not the bitmap track, not the labelled one

    async with session_factory() as session:
        rows = (await session.exec(select(StreamRecord).where(StreamRecord.file_id == file_id))).all()
        by_index = {r.stream_index: r for r in rows}
    assert by_index[1].detected_language == "dut"
    assert by_index[2].detected_language == ""  # looked, nothing conclusive
    assert by_index[3].detected_language is None  # never attempted

    # A second pass must not decode anything again.
    decoded.clear()
    async with session_factory() as session:
        await propose_normalizations(session, config)
    assert decoded == []


@pytest.mark.asyncio
async def test_detection_is_off_unless_enabled(session_factory, tmp_path, monkeypatch):
    import app.normalize_service as svc

    (tmp_path / "Movie.mkv").write_bytes(b"x")
    await _seed_file(
        session_factory,
        str(tmp_path / "Movie.mkv"),
        [
            {"stream_index": 0, "codec_type": "video", "codec_name": "h264"},
            {"stream_index": 1, "codec_type": "subtitle", "codec_name": "subrip"},
        ],
    )

    called = []
    monkeypatch.setattr(svc, "extract_subtitle_text", lambda *a, **k: called.append(1) or "")

    async with session_factory() as session:
        await propose_normalizations(session, NormalizerConfig())

    assert called == []
