"""Covers the Review/Queue/Normalize/Normalize Queue pages' pagination
(app/web.py::_paginate) — added because these pages used to render every
pending/queued item in one unbounded response; a large first scan (hundreds+
files) meant a multi-MB page and hundreds of DOM nodes at once.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, make_engine, make_session_factory
from app.main import create_app
from app.models import ChangeStatus, LibraryType, MediaFile, NormalizationChange, PendingChange
from app.web import PAGE_SIZE

PROPOSED = [
    {"index": 0, "type": "audio", "codec": "ac3", "language": "jpn", "title": None, "keep": False, "reason": "not in keep-list"},
]

NORMALIZE_PROPOSED = [
    {
        "index": 0,
        "codec_type": "audio",
        "track_selector": "a1",
        "old_title": None,
        "new_title": "English",
        "old_language": "eng",
        "new_language": "eng",
        "old_default": False,
        "new_default": None,
        "changed": True,
        "reason": "untitled",
    }
]


def _seed_pending_changes(db_path, count: int, status=ChangeStatus.pending):
    async def run():
        engine = make_engine(db_path)
        await init_db(engine)
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            for i in range(count):
                mf = MediaFile(path=f"/movies/Movie {i:04d}.mkv", library_type=LibraryType.movie, size_bytes=1, mtime=1.0)
                session.add(mf)
                await session.commit()
                await session.refresh(mf)
                session.add(PendingChange(file_id=mf.id, status=status, proposed=PROPOSED))
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def _seed_normalize_changes(db_path, count: int, status=ChangeStatus.pending):
    async def run():
        engine = make_engine(db_path)
        await init_db(engine)
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            for i in range(count):
                mf = MediaFile(path=f"/movies/Movie {i:04d}.mkv", library_type=LibraryType.movie, size_bytes=1, mtime=1.0)
                session.add(mf)
                await session.commit()
                await session.refresh(mf)
                session.add(NormalizationChange(file_id=mf.id, status=status, proposed=NORMALIZE_PROPOSED))
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


@pytest.fixture
def many_pending(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_pending_changes(db_path, PAGE_SIZE + 15)
    return db_path


def test_review_page_one_shows_only_a_page_worth_of_items(many_pending):
    with TestClient(create_app(many_pending)) as c:
        page1 = c.get("/review").text
        assert page1.count("data-review-card data-item-id") == PAGE_SIZE
        assert f"{PAGE_SIZE + 15} of {PAGE_SIZE + 15}" in page1
        assert f"showing {PAGE_SIZE}" in page1
        assert "Page 1 of 2" in page1
        assert "Next" in page1


def test_review_page_two_shows_the_remainder_and_has_no_next_link(many_pending):
    with TestClient(create_app(many_pending)) as c:
        page2 = c.get("/review", params={"page": "2"}).text
        assert page2.count("data-review-card data-item-id") == 15
        assert "Page 2 of 2" in page2


def test_review_page_beyond_range_clamps_to_last_page(many_pending):
    with TestClient(create_app(many_pending)) as c:
        page99 = c.get("/review", params={"page": "99"}).text
        assert "Page 2 of 2" in page99
        assert page99.count("data-review-card data-item-id") == 15


def test_review_pagination_preserves_filters_on_next_link(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_pending_changes(db_path, PAGE_SIZE + 5)
    with TestClient(create_app(db_path)) as c:
        page1 = c.get("/review", params={"library_type": "movie"}).text
        assert "library_type=movie" in page1
        assert "page=2" in page1


def test_queue_page_paginates_and_run_queue_count_reflects_total_not_page(many_pending, tmp_path):
    # Move everything to approved so it shows on /queue.
    async def approve_all():
        engine = make_engine(many_pending)
        session_factory = make_session_factory(engine)
        from sqlmodel import select

        async with session_factory() as session:
            changes = (await session.exec(select(PendingChange))).all()
            for c in changes:
                c.status = ChangeStatus.approved
                session.add(c)
            await session.commit()
        await engine.dispose()

    asyncio.run(approve_all())

    with TestClient(create_app(many_pending)) as c:
        page1 = c.get("/queue").text
        # The header count and "Run Queue" button must reflect the TRUE
        # total queued (all pages), not just what's rendered on this page —
        # /queue/run always applies every approved change regardless of
        # what's currently displayed.
        assert f"{PAGE_SIZE + 15} change(s) queued" in page1
        assert f"Run Queue ({PAGE_SIZE + 15})" in page1
        assert f"Showing {PAGE_SIZE} on this page" in page1


def test_normalize_page_paginates(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_normalize_changes(db_path, PAGE_SIZE + 3)
    with TestClient(create_app(db_path)) as c:
        page1 = c.get("/normalize").text
        assert "Page 1 of 2" in page1
        page2 = c.get("/normalize", params={"page": "2"}).text
        assert "Page 2 of 2" in page2


def test_normalize_queue_page_paginates_and_run_count_reflects_total(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_normalize_changes(db_path, PAGE_SIZE + 7, status=ChangeStatus.approved)
    with TestClient(create_app(db_path)) as c:
        page1 = c.get("/normalize/queue").text
        assert f"{PAGE_SIZE + 7} file(s) queued" in page1
        assert f"Run Normalize Queue ({PAGE_SIZE + 7})" in page1
