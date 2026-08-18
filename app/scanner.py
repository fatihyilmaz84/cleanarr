"""Walks configured media paths, ffprobes new/changed files, and populates
the review queue. Change detection is mtime+size based so re-scans are cheap
— unchanged files are never re-probed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.analyzer import AnalyzerError, probe_file
from app.arr_client import ArrClient, ArrMediaInfo, normalize_path
from app.models import ChangeStatus, LibraryType, MediaFile, PendingChange, StreamRecord
from app.rules import RuleConfig, decide
from app.settings_store import MediaPath

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}


def _now():
    return datetime.now(timezone.utc)


@dataclass
class ScanSummary:
    files_seen: int = 0
    files_scanned: int = 0  # actually re-ffprobed (new or changed since last scan)
    files_skipped_unchanged: int = 0
    files_with_pending_changes: int = 0
    errors: list[str] = field(default_factory=list)
    arr_warnings: list[str] = field(default_factory=list)


def _iter_media_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            yield path


def _library_type_for(mp: MediaPath) -> LibraryType:
    try:
        return LibraryType(mp.library_type)
    except ValueError:
        return LibraryType.unknown


async def run_scan(
    session: AsyncSession,
    media_paths: list[MediaPath],
    rule_config: RuleConfig,
    arr_client: ArrClient | None = None,
    progress_cb: Callable[[ScanSummary], None] | None = None,
) -> ScanSummary:
    summary = ScanSummary()

    arr_index: dict[str, ArrMediaInfo] = {}
    if arr_client is not None:
        arr_index, summary.arr_warnings = await arr_client.build_index()

    for mp in media_paths:
        root = Path(mp.path)
        if not root.exists():
            summary.errors.append(f"media path does not exist: {mp.path}")
            continue

        library_type = _library_type_for(mp)
        for file_path in _iter_media_files(root):
            summary.files_seen += 1
            try:
                await _scan_one_file(session, file_path, library_type, rule_config, arr_index, summary)
            except AnalyzerError as e:
                summary.errors.append(f"{file_path}: {e}")
            if progress_cb:
                progress_cb(summary)

    return summary


async def _scan_one_file(
    session: AsyncSession,
    file_path: Path,
    library_type: LibraryType,
    rule_config: RuleConfig,
    arr_index: dict[str, ArrMediaInfo],
    summary: ScanSummary,
) -> None:
    stat = file_path.stat()
    path_str = str(file_path)

    result = await session.exec(select(MediaFile).where(MediaFile.path == path_str))
    media_file = result.one_or_none()

    unchanged = media_file is not None and media_file.size_bytes == stat.st_size and media_file.mtime == stat.st_mtime
    if unchanged:
        summary.files_skipped_unchanged += 1
        return

    probe = await asyncio.to_thread(probe_file, file_path)
    summary.files_scanned += 1

    if media_file is None:
        media_file = MediaFile(path=path_str, library_type=library_type)

    media_file.size_bytes = stat.st_size
    media_file.mtime = stat.st_mtime
    media_file.library_type = library_type
    media_file.last_scanned_at = _now()

    arr_info = arr_index.get(normalize_path(path_str))
    if arr_info:
        media_file.display_title = (
            arr_info.title if arr_info.kind == "movie" else f"{arr_info.series_title} - {arr_info.title}"
        )
        media_file.poster_url = arr_info.poster_url
        media_file.arr_id = arr_info.arr_id
        media_file.arr_kind = arr_info.kind

    session.add(media_file)
    await session.commit()
    await session.refresh(media_file)

    existing_streams = (await session.exec(select(StreamRecord).where(StreamRecord.file_id == media_file.id))).all()
    for s in existing_streams:
        await session.delete(s)

    for s in probe.streams:
        session.add(
            StreamRecord(
                file_id=media_file.id,
                stream_index=s.index,
                codec_type=s.codec_type,
                codec_name=s.codec_name,
                language=s.language,
                title=s.title,
                is_default=s.is_default,
                is_forced=s.is_forced,
                is_commentary=s.is_commentary,
                is_hearing_impaired=s.is_hearing_impaired,
            )
        )

    decisions = decide(probe, rule_config)
    dropped = [d for d in decisions if not d.keep]

    existing_change = (
        await session.exec(
            select(PendingChange).where(
                PendingChange.file_id == media_file.id, PendingChange.status == ChangeStatus.pending
            )
        )
    ).one_or_none()

    if dropped:
        summary.files_with_pending_changes += 1
        proposed = [
            {
                "index": d.stream.index,
                "type": d.stream.codec_type,
                "codec": d.stream.codec_name,
                "language": d.stream.language,
                "title": d.stream.title,
                "keep": d.keep,
                "reason": d.reason,
            }
            for d in decisions
        ]
        if existing_change:
            existing_change.proposed = proposed
            existing_change.updated_at = _now()
            session.add(existing_change)
        else:
            session.add(PendingChange(file_id=media_file.id, status=ChangeStatus.pending, proposed=proposed))
    elif existing_change:
        # File now matches the rules (e.g. rules changed since it was queued)
        # — the stale suggestion no longer applies.
        await session.delete(existing_change)

    await session.commit()
