"""Async SQLite engine/session setup. Single-file DB on a mounted config
volume so it survives container recreation.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession


def default_db_path() -> Path:
    return Path(os.environ.get("CLEANARR_DB_PATH", "data/cleanarr.db"))


def make_engine(db_path: Path | None = None) -> AsyncEngine:
    resolved = db_path if db_path is not None else default_db_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{resolved}", echo=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
