import pytest
from sqlmodel import select

from app.db import init_db, make_engine, make_session_factory
from app.models import ChangeStatus, LibraryType, MediaFile, PendingChange, StreamRecord


@pytest.mark.asyncio
async def test_init_db_adds_missing_status_indices_to_an_existing_db(tmp_path):
    """PendingChange.status/NormalizationChange.status gained `index=True`
    after those tables already existed on deployed installs — create_all()
    only creates missing *tables*, never adds an index to one that's already
    there, so init_db() needs its own explicit CREATE INDEX step (mirroring
    _add_missing_columns for the same reason). Simulate an "old" DB missing
    the index and check init_db() adds it without erroring.
    """
    engine = make_engine(tmp_path / "old.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE pending_changes (id INTEGER PRIMARY KEY, file_id INTEGER, status VARCHAR)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE normalization_changes (id INTEGER PRIMARY KEY, file_id INTEGER, status VARCHAR)"
        )

    await init_db(engine)

    async with engine.begin() as conn:
        result = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pending_changes'"
        )
        assert "ix_pending_changes_status" in {row[0] for row in result.fetchall()}
        result = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='normalization_changes'"
        )
        assert "ix_normalization_changes_status" in {row[0] for row in result.fetchall()}

    await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_and_roundtrip(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    await init_db(engine)
    session_factory = make_session_factory(engine)

    async with session_factory() as session:
        media_file = MediaFile(
            path="/media/movies/Example (2020)/Example.mkv",
            library_type=LibraryType.movie,
            size_bytes=10_000_000_000,
            mtime=1700000000.0,
        )
        session.add(media_file)
        await session.commit()
        await session.refresh(media_file)

        stream = StreamRecord(
            file_id=media_file.id,
            stream_index=1,
            codec_type="audio",
            codec_name="ac3",
            language="eng",
        )
        session.add(stream)

        change = PendingChange(
            file_id=media_file.id,
            status=ChangeStatus.pending,
            proposed=[{"index": 2, "keep": False, "reason": "language 'jpn' not in keep-list"}],
        )
        session.add(change)
        await session.commit()

    async with session_factory() as session:
        result = await session.exec(select(MediaFile).where(MediaFile.path.contains("Example")))
        fetched = result.one()
        assert fetched.library_type == LibraryType.movie
        assert fetched.size_bytes == 10_000_000_000

        streams = (await session.exec(select(StreamRecord).where(StreamRecord.file_id == fetched.id))).all()
        assert len(streams) == 1
        assert streams[0].language == "eng"

        changes = (await session.exec(select(PendingChange).where(PendingChange.file_id == fetched.id))).all()
        assert len(changes) == 1
        assert changes[0].status == ChangeStatus.pending
        assert changes[0].proposed[0]["reason"].startswith("language")

    await engine.dispose()
