"""Walks configured media paths, ffprobes new/changed files, and populates
the review queue. Change detection is mtime+size based so re-scans are cheap
— unchanged files are never re-probed. Rule evaluation and Sonarr/Radarr
enrichment are *not* gated on that check, though: they run against the
already-stored stream data on every scan, so changing the rules or
connecting Sonarr/Radarr takes effect on the next scan without needing the
file itself to change.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.analyzer import AnalyzerError, MediaProbe, MediaStream, probe_file
from app.arr_client import ArrClient, ArrMediaInfo, normalize_path
from app.models import ChangeStatus, LibraryType, MediaFile, PendingChange, StreamRecord
from app.rules import RuleConfig, decide
from app.settings_store import MediaPath

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}


def _now():
    return datetime.now(timezone.utc)


@dataclass
class ScanSummary:
    files_total: int = 0  # known upfront from the directory walk, for progress reporting
    files_seen: int = 0
    files_scanned: int = 0  # actually re-ffprobed (new or changed since last scan)
    files_skipped_unchanged: int = 0
    files_with_pending_changes: int = 0
    errors: list[str] = field(default_factory=list)
    arr_warnings: list[str] = field(default_factory=list)
    # True if a `deadline` (see run_scan) was hit before every file could be
    # scanned — not an error, just means the rest is left for next time.
    stopped_early: bool = False


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
    deadline: datetime | None = None,
) -> ScanSummary:
    summary = ScanSummary()

    arr_index: dict[str, ArrMediaInfo] = {}
    if arr_client is not None:
        arr_index, summary.arr_warnings = await arr_client.build_index()

    # Walk every configured path up front (cheap — directory listing only,
    # no ffprobe) so the total file count is known before work starts and a
    # progress bar has something to divide by.
    work_items: list[tuple[Path, LibraryType]] = []
    for mp in media_paths:
        root = Path(mp.path)
        if not root.exists():
            summary.errors.append(f"media path does not exist: {mp.path}")
            continue

        library_type = _library_type_for(mp)
        for file_path in _iter_media_files(root):
            work_items.append((file_path, library_type))

    summary.files_total = len(work_items)

    for file_path, library_type in work_items:
        # Checked between files, never mid-file — a probe is read-only and
        # quick, so there's no unsafe "abort partway through" state to worry
        # about here (unlike a remux, see app/actions.py::_apply_changes).
        if deadline is not None and _now() >= deadline:
            summary.stopped_early = True
            break

        summary.files_seen += 1
        try:
            await _scan_one_file(session, file_path, library_type, rule_config, arr_index, summary)
        except AnalyzerError as e:
            summary.errors.append(f"{file_path}: {e}")
        if progress_cb:
            progress_cb(summary)

    return summary


def _stream_from_record(record: StreamRecord) -> MediaStream:
    return MediaStream(
        index=record.stream_index,
        codec_type=record.codec_type,
        codec_name=record.codec_name,
        language=record.language,
        title=record.title,
        channels=None,
        is_default=record.is_default,
        is_forced=record.is_forced,
        is_commentary=record.is_commentary,
        is_hearing_impaired=record.is_hearing_impaired,
    )


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
        # Skip the expensive ffprobe re-run, but rule evaluation and
        # Sonarr/Radarr enrichment still run below against the streams
        # already on file — those are cheap and must reflect the *current*
        # rules/arr connection even for a file whose bytes haven't moved.
        summary.files_skipped_unchanged += 1
        existing_streams = (
            await session.exec(select(StreamRecord).where(StreamRecord.file_id == media_file.id))
        ).all()
        streams = [_stream_from_record(s) for s in existing_streams]
    else:
        probe = await asyncio.to_thread(probe_file, file_path)
        summary.files_scanned += 1

        if media_file is None:
            media_file = MediaFile(path=path_str, library_type=library_type)

        media_file.size_bytes = stat.st_size
        media_file.mtime = stat.st_mtime
        media_file.library_type = library_type
        media_file.last_scanned_at = _now()
        session.add(media_file)
        await session.commit()
        await session.refresh(media_file)

        existing_streams = (
            await session.exec(select(StreamRecord).where(StreamRecord.file_id == media_file.id))
        ).all()
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
        streams = probe.streams

    arr_info = arr_index.get(normalize_path(path_str))
    if arr_info:
        media_file.display_title = (
            arr_info.title if arr_info.kind == "movie" else f"{arr_info.series_title} - {arr_info.title}"
        )
        media_file.poster_url = arr_info.poster_url
        media_file.arr_id = arr_info.arr_id
        media_file.arr_kind = arr_info.kind
        media_file.original_language = arr_info.original_language
    session.add(media_file)
    await session.commit()

    fake_probe = MediaProbe(path=file_path, duration_seconds=None, streams=streams)
    decisions = decide(fake_probe, rule_config, media_file.original_language)
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
