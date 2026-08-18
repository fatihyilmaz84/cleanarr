"""Applies one approved pending change: re-probes and re-decides against the
*current* rule config (never trusts the cached scan-time decision, since the
file or the rules may have changed since it was queued), then hands off to
the remux executor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.analyzer import AnalyzerError, probe_file
from app.models import ChangeStatus, HistoryEntry, MediaFile, PendingChange, StreamRecord
from app.remux import RemuxError, apply_remux
from app.rules import RuleConfig, decide


@dataclass
class ApplyResult:
    pending_change_id: int
    success: bool
    message: str
    bytes_reclaimed: int = 0


async def apply_pending_change(session: AsyncSession, pending_change_id: int, rule_config: RuleConfig) -> ApplyResult:
    change = await session.get(PendingChange, pending_change_id)
    if change is None:
        return ApplyResult(pending_change_id, False, "pending change not found")
    if change.status not in (ChangeStatus.pending, ChangeStatus.approved):
        return ApplyResult(pending_change_id, False, f"not applicable, status is '{change.status.value}'")

    media_file = await session.get(MediaFile, change.file_id)
    if media_file is None:
        change.status = ChangeStatus.failed
        change.error_message = "media file record missing"
        session.add(change)
        await session.commit()
        return ApplyResult(pending_change_id, False, "media file record missing")

    path = Path(media_file.path)
    try:
        probe = await asyncio.to_thread(probe_file, path)
        decisions = decide(probe, rule_config)
        result = await asyncio.to_thread(apply_remux, path, decisions)
    except (AnalyzerError, RemuxError) as e:
        change.status = ChangeStatus.failed
        change.error_message = str(e)
        session.add(change)
        await session.commit()
        return ApplyResult(pending_change_id, False, str(e))

    if not result.applied:
        # Nothing left to drop (file or rules changed since it was queued) —
        # not an error, just nothing to do.
        change.status = ChangeStatus.applied
        change.error_message = None
        session.add(change)
        await session.commit()
        return ApplyResult(pending_change_id, True, result.reason)

    stat = path.stat()
    media_file.size_bytes = stat.st_size
    media_file.mtime = stat.st_mtime
    session.add(media_file)

    existing_streams = (await session.exec(select(StreamRecord).where(StreamRecord.file_id == media_file.id))).all()
    for s in existing_streams:
        await session.delete(s)
    for d in decisions:
        if not d.keep:
            continue
        session.add(
            StreamRecord(
                file_id=media_file.id,
                stream_index=d.stream.index,
                codec_type=d.stream.codec_type,
                codec_name=d.stream.codec_name,
                language=d.stream.language,
                title=d.stream.title,
                is_default=d.stream.is_default,
                is_forced=d.stream.is_forced,
                is_commentary=d.stream.is_commentary,
                is_hearing_impaired=d.stream.is_hearing_impaired,
            )
        )

    change.status = ChangeStatus.applied
    change.error_message = None
    session.add(change)

    session.add(
        HistoryEntry(
            file_id=media_file.id,
            streams_removed=result.streams_removed,
            bytes_before=result.bytes_before or 0,
            bytes_after=result.bytes_after or 0,
        )
    )
    await session.commit()

    bytes_reclaimed = (result.bytes_before or 0) - (result.bytes_after or 0)
    return ApplyResult(pending_change_id, True, "applied", bytes_reclaimed=bytes_reclaimed)
