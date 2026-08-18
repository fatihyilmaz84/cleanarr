"""DB-touching orchestration for the normalizer: proposing changes (reading
already-scanned MediaFile/StreamRecord data — no ffprobe needed, unlike
app/scanner.py, since the normalizer is a pure function of data the
regular scan already collected) and applying them (via app/mkv_metadata.py).
Mirrors the scanner.py/apply.py split, but "proposing" here is a DB read +
pure computation, never a filesystem walk.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.analyzer import MediaStream
from app.mkv_metadata import MkvMetadataError, apply_metadata_changes, is_mkv
from app.models import ChangeStatus, MediaFile, NormalizationChange, PendingChange, StreamRecord
from app.normalizer import NormalizerConfig, TrackNormalization, apply_overrides, normalize_streams


def _now():
    return datetime.now(timezone.utc)


@dataclass
class NormalizeScanSummary:
    files_considered: int = 0
    files_with_changes: int = 0
    errors: list[str] = field(default_factory=list)


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


async def _dropped_indices_for_file(session: AsyncSession, file_id: int) -> set[int]:
    """Stream indices app/rules.py's drop engine currently proposes removing
    for this file (status pending or approved) — excluded from
    normalization, since there's nothing left to retitle once a track is
    gone. See TODO.md #7's per-track exclusivity note; the reverse
    direction (a normalize-selected track protected from removal) is
    handled on the rules.py side via the same PendingChange.overrides
    mechanism, not here.
    """
    result = await session.exec(
        select(PendingChange).where(
            PendingChange.file_id == file_id,
            PendingChange.status.in_([ChangeStatus.pending, ChangeStatus.approved]),
        )
    )
    change = result.first()
    if change is None:
        return set()
    overrides = set(change.overrides or [])
    return {p["index"] for p in change.proposed if not p["keep"] and p["index"] not in overrides}


def _serialize(n: TrackNormalization) -> dict:
    return {
        "index": n.index,
        "codec_type": n.codec_type,
        "track_selector": n.track_selector,
        "old_title": n.old_title,
        "new_title": n.new_title,
        "old_language": n.old_language,
        "new_language": n.new_language,
        "old_default": n.old_default,
        "new_default": n.new_default,
        "changed": n.changed,
        "reason": n.reason,
    }


async def propose_normalizations(
    session: AsyncSession,
    config: NormalizerConfig,
    progress_cb: Callable[[NormalizeScanSummary], None] | None = None,
) -> NormalizeScanSummary:
    summary = NormalizeScanSummary()

    media_files = (await session.exec(select(MediaFile))).all()
    for media_file in media_files:
        summary.files_considered += 1
        records = (
            await session.exec(
                select(StreamRecord).where(StreamRecord.file_id == media_file.id).order_by(StreamRecord.stream_index)
            )
        ).all()
        if not records:
            if progress_cb:
                progress_cb(summary)
            continue

        dropped = await _dropped_indices_for_file(session, media_file.id)
        eligible_streams = [_stream_from_record(r) for r in records if r.stream_index not in dropped]

        normalizations = normalize_streams(eligible_streams, config)
        changed = [n for n in normalizations if n.changed]

        existing = (
            await session.exec(
                select(NormalizationChange).where(
                    NormalizationChange.file_id == media_file.id, NormalizationChange.status == ChangeStatus.pending
                )
            )
        ).one_or_none()

        if changed:
            summary.files_with_changes += 1
            proposed = [_serialize(n) for n in normalizations]
            if existing:
                existing.proposed = proposed
                existing.updated_at = _now()
                session.add(existing)
            else:
                session.add(NormalizationChange(file_id=media_file.id, status=ChangeStatus.pending, proposed=proposed))
        elif existing:
            # File now matches the naming scheme (e.g. config changed since
            # it was queued) — the stale suggestion no longer applies.
            await session.delete(existing)

        await session.commit()
        if progress_cb:
            progress_cb(summary)

    return summary


@dataclass
class NormalizeApplyResult:
    change_id: int
    success: bool
    message: str
    tracks_updated: int = 0


async def apply_normalization_change(
    session: AsyncSession, change_id: int, config: NormalizerConfig
) -> NormalizeApplyResult:
    change = await session.get(NormalizationChange, change_id)
    if change is None:
        return NormalizeApplyResult(change_id, False, "normalization change not found")
    if change.status not in (ChangeStatus.pending, ChangeStatus.approved):
        return NormalizeApplyResult(change_id, False, f"not applicable, status is '{change.status.value}'")

    media_file = await session.get(MediaFile, change.file_id)
    if media_file is None:
        change.status = ChangeStatus.failed
        change.error_message = "media file record missing"
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, False, "media file record missing")

    path = Path(media_file.path)

    # Re-decide from scratch against the *current* streams and config —
    # never trust the cached `proposed`, same reasoning as app/apply.py.
    records = (
        await session.exec(
            select(StreamRecord).where(StreamRecord.file_id == media_file.id).order_by(StreamRecord.stream_index)
        )
    ).all()
    dropped = await _dropped_indices_for_file(session, media_file.id)
    eligible_streams = [_stream_from_record(r) for r in records if r.stream_index not in dropped]
    normalizations = normalize_streams(eligible_streams, config)
    normalizations = apply_overrides(normalizations, change.overrides)
    changed = [n for n in normalizations if n.changed]

    if not changed:
        change.status = ChangeStatus.applied
        change.error_message = None
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, True, "nothing to change, file already matches the naming scheme")

    if not is_mkv(path):
        message = "only MKV files are supported for normalization right now"
        change.status = ChangeStatus.failed
        change.error_message = message
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, False, message)

    try:
        tracks_updated = await asyncio.to_thread(apply_metadata_changes, path, changed)
    except MkvMetadataError as e:
        change.status = ChangeStatus.failed
        change.error_message = str(e)
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, False, str(e))

    # Reflect the new titles/defaults in the cached StreamRecord rows so a
    # future scan/normalize pass doesn't see stale data.
    by_index = {n.index: n for n in changed}
    for record in records:
        n = by_index.get(record.stream_index)
        if n is None:
            continue
        record.title = n.new_title
        if n.new_default is not None:
            record.is_default = n.new_default
        session.add(record)

    change.status = ChangeStatus.applied
    change.error_message = None
    session.add(change)
    await session.commit()

    return NormalizeApplyResult(change_id, True, "applied", tracks_updated=tracks_updated)
