"""Async SQLite engine/session setup. Single-file DB on a mounted config
volume so it survives container recreation.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession


def default_db_path() -> Path:
    return Path(os.environ.get("CLEANARR_DB_PATH", "data/cleanarr.db"))


def make_engine(db_path: Path | None = None) -> AsyncEngine:
    resolved = db_path if db_path is not None else default_db_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{resolved}", echo=False)

    # Scanning does a commit per file (needed so a rule/arr-config change is
    # reflected file-by-file, see app/scanner.py) — on a scan of thousands of
    # files, SQLite's default per-commit fsync makes that thousands of disk
    # syncs. WAL mode + synchronous=NORMAL keeps commits durable across an
    # app/OS crash while dropping most of that fsync cost.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _add_missing_columns(conn, "media_files", {"original_language": "VARCHAR"})
        await _add_missing_columns(conn, "pending_changes", {"overrides": "JSON"})
        await _add_missing_index(conn, "ix_pending_changes_status", "pending_changes", "status")
        await _add_missing_index(conn, "ix_normalization_changes_status", "normalization_changes", "status")


async def _add_missing_columns(conn, table: str, columns: dict[str, str]) -> None:
    """create_all only creates missing tables, it never alters an existing
    one — so a column added to a model after the table already exists on
    disk (e.g. an upgrade of a running instance) needs a manual ALTER TABLE.
    """
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    existing = {row[1] for row in result.fetchall()}
    for name, sql_type in columns.items():
        if name not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


async def _add_missing_index(conn, index_name: str, table: str, column: str) -> None:
    """Same reasoning as _add_missing_columns — create_all() skips a table
    entirely once it exists, so it never adds an index to a model field
    that gained `index=True` after the table was already on disk.
    """
    await conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})")


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
