"""SQLModel table definitions. Kept intentionally simple/denormalized —
this app has one writer (its own background worker) and a small dataset
(one row per media file), so there's no need for anything heavier.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlmodel import JSON, Column, Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LibraryType(str, Enum):
    movie = "movie"
    tv = "tv"
    unknown = "unknown"


class ChangeStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    skipped = "skipped"
    applied = "applied"
    failed = "failed"


class MediaFile(SQLModel, table=True):
    __tablename__ = "media_files"

    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(unique=True, index=True)
    library_type: LibraryType = LibraryType.unknown
    size_bytes: int = 0
    mtime: float = 0.0
    last_scanned_at: datetime = Field(default_factory=utcnow)

    # Enrichment from Sonarr/Radarr — nullable, purely for display except
    # original_language, which the rule engine also reads (see app/rules.py).
    arr_kind: str | None = None  # "sonarr" | "radarr"
    arr_id: int | None = None
    display_title: str | None = None
    poster_url: str | None = None
    original_language: str | None = None  # e.g. "Korean"


class StreamRecord(SQLModel, table=True):
    __tablename__ = "streams"

    id: int | None = Field(default=None, primary_key=True)
    file_id: int = Field(foreign_key="media_files.id", index=True)
    stream_index: int
    codec_type: str
    codec_name: str
    language: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False
    is_commentary: bool = False
    is_hearing_impaired: bool = False


class PendingChange(SQLModel, table=True):
    __tablename__ = "pending_changes"

    id: int | None = Field(default=None, primary_key=True)
    file_id: int = Field(foreign_key="media_files.id", index=True)
    status: ChangeStatus = ChangeStatus.pending
    # Serialized list of {index, type, codec, language, title, keep, reason}
    proposed: list = Field(default_factory=list, sa_column=Column(JSON))
    # Stream indices the user force-kept at approval time despite the rule
    # engine proposing to drop them (e.g. "drop the audio but not this
    # subtitle") — applied on top of the rules at apply time, see
    # app/rules.py::apply_overrides. NULL/None for rows from before this
    # column existed, meaning no overrides.
    overrides: list | None = Field(default=None, sa_column=Column(JSON))
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class HistoryEntry(SQLModel, table=True):
    __tablename__ = "history"

    id: int | None = Field(default=None, primary_key=True)
    file_id: int = Field(foreign_key="media_files.id", index=True)
    applied_at: datetime = Field(default_factory=utcnow)
    streams_removed: list = Field(default_factory=list, sa_column=Column(JSON))
    bytes_before: int = 0
    bytes_after: int = 0


class AppSetting(SQLModel, table=True):
    """Generic key/value store for everything that isn't per-file data:
    rule config, media paths, Sonarr/Radarr connection info, scan schedule,
    free-space safety margin. Avoids locking in a rigid settings schema
    before the UI's actual needs are known.
    """

    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
