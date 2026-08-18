"""Tests for the Review page's filter controls (search / library type /
original language). Seeds pending changes directly into the DB rather than
running a full scan — filtering itself (in app/web.py) is what's under test,
not the scan pipeline.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.db import init_db, make_engine, make_session_factory
from app.main import create_app
from app.models import ChangeStatus, LibraryType, MediaFile, PendingChange

PROPOSED = [
    {"index": 0, "type": "video", "codec": "h264", "language": None, "title": None, "keep": True, "reason": "video track, always kept"},
    {"index": 1, "type": "audio", "codec": "ac3", "language": "eng", "title": None, "keep": True, "reason": "language 'eng' in keep-list"},
    {"index": 2, "type": "audio", "codec": "aac", "language": "jpn", "title": None, "keep": False, "reason": "language 'jpn' not in keep-list"},
]


def _seed(db_path):
    async def run():
        engine = make_engine(db_path)
        await init_db(engine)
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            for path, title, library_type, original_language in [
                ("/movies/Oldboy (2003)/Oldboy.mkv", "Oldboy", LibraryType.movie, "Korean"),
                ("/tv/Show/S01E01.mkv", "Show S01E01", LibraryType.tv, "Japanese"),
            ]:
                media_file = MediaFile(
                    path=path,
                    library_type=library_type,
                    size_bytes=1000,
                    mtime=1.0,
                    display_title=title,
                    original_language=original_language,
                )
                session.add(media_file)
                await session.commit()
                await session.refresh(media_file)
                session.add(PendingChange(file_id=media_file.id, status=ChangeStatus.pending, proposed=PROPOSED))
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_review_filters_by_library_type_language_and_search(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    with TestClient(create_app(db_path)) as c:
        all_items = c.get("/review").text
        assert "Oldboy" in all_items and "Show S01E01" in all_items
        assert "2 of 2" in all_items

        movies_only = c.get("/review", params={"library_type": "movie"}).text
        assert "Oldboy" in movies_only and "Show S01E01" not in movies_only
        # the filter dropdowns themselves must still offer every option, not
        # just the ones present in the already-filtered result set
        assert '<option value="tv"' in movies_only
        assert '<option value="Japanese"' in movies_only

        korean_only = c.get("/review", params={"language": "Korean"}).text
        assert "Oldboy" in korean_only and "Show S01E01" not in korean_only

        search = c.get("/review", params={"q": "oldboy"}).text  # case-insensitive
        assert "Oldboy" in search and "Show S01E01" not in search

        # both seeded items drop an audio track and neither drops a subtitle
        audio_drops = c.get("/review", params={"drop_type": "audio"}).text
        assert "Oldboy" in audio_drops and "Show S01E01" in audio_drops
        subtitle_drops = c.get("/review", params={"drop_type": "subtitle"}).text
        assert "No items match these filters" in subtitle_drops

        no_match = c.get("/review", params={"q": "nonexistent"}).text
        assert "No items match these filters" in no_match
        assert "Clear filters" in no_match
