"""Read-side query helpers shared by the JSON API and the server-rendered
UI, so the two can't drift out of sync on what a "pending change" or
"history entry" looks like.
"""

from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import ChangeStatus, HistoryEntry, MediaFile, PendingChange


async def review_item(session: AsyncSession, change: PendingChange) -> dict:
    mf = await session.get(MediaFile, change.file_id)
    return {
        "id": change.id,
        "file_id": change.file_id,
        "path": mf.path if mf else None,
        "display_title": mf.display_title if mf else None,
        "poster_url": mf.poster_url if mf else None,
        "library_type": mf.library_type.value if mf else None,
        "original_language": mf.original_language if mf else None,
        "status": change.status.value,
        "proposed": change.proposed,
        "kept": [p for p in change.proposed if p["keep"]],
        "dropped": [p for p in change.proposed if not p["keep"]],
        "error_message": change.error_message,
        "created_at": change.created_at,
    }


async def list_review_items(session: AsyncSession, status: ChangeStatus) -> list[dict]:
    result = await session.exec(
        select(PendingChange).where(PendingChange.status == status).order_by(PendingChange.created_at.desc())
    )
    return [await review_item(session, c) for c in result.all()]


async def history_item(session: AsyncSession, entry: HistoryEntry) -> dict:
    mf = await session.get(MediaFile, entry.file_id)
    return {
        "id": entry.id,
        "file_id": entry.file_id,
        "path": mf.path if mf else None,
        "display_title": mf.display_title if mf else None,
        "applied_at": entry.applied_at,
        "streams_removed": entry.streams_removed,
        "bytes_before": entry.bytes_before,
        "bytes_after": entry.bytes_after,
        "bytes_reclaimed": entry.bytes_before - entry.bytes_after,
    }


async def list_history_items(session: AsyncSession, limit: int = 50) -> list[dict]:
    result = await session.exec(select(HistoryEntry).order_by(HistoryEntry.applied_at.desc()).limit(limit))
    return [await history_item(session, h) for h in result.all()]


async def overview_stats(session: AsyncSession) -> dict:
    pending = (await session.exec(select(PendingChange).where(PendingChange.status == ChangeStatus.pending))).all()
    all_files = (await session.exec(select(MediaFile))).all()
    all_history = (await session.exec(select(HistoryEntry))).all()

    return {
        "total_files": len(all_files),
        "pending_review_count": len(pending),
        "total_bytes_reclaimed": sum(h.bytes_before - h.bytes_after for h in all_history),
        "total_applied_count": len(all_history),
    }
