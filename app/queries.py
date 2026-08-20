"""Read-side query helpers shared by the JSON API and the server-rendered
UI, so the two can't drift out of sync on what a "pending change" or
"history entry" looks like.
"""

from __future__ import annotations

from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import ChangeStatus, HistoryEntry, MediaFile, NormalizationChange, PendingChange
from app.normalizer import SKIPPED_BY_USER, SKIPPED_PENDING_REMOVAL


def _effective_review(change: PendingChange, mf: MediaFile | None) -> dict:
    # Overrides (see app/rules.py::apply_overrides) force-keep specific
    # stream indices at apply time — reflect that here so "kept"/"dropped"
    # always shows the *actual* plan, not just the raw rule proposal. For a
    # still-pending item overrides is always empty, so this is a no-op there.
    overrides = set(change.overrides or [])
    effective = [{**p, "keep": True} if p["index"] in overrides else p for p in change.proposed]
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
        "kept": [p for p in effective if p["keep"]],
        "dropped": [p for p in effective if not p["keep"]],
        "error_message": change.error_message,
        "created_at": change.created_at,
    }


async def review_item(session: AsyncSession, change: PendingChange) -> dict:
    mf = await session.get(MediaFile, change.file_id)
    return _effective_review(change, mf)


async def _media_files_by_id(session: AsyncSession, file_ids: list[int]) -> dict[int, MediaFile]:
    # Batch-fetch once instead of a session.get() per row — the N+1 pattern
    # this replaced meant one extra round trip per item on every review/
    # history/normalize listing.
    if not file_ids:
        return {}
    rows = (await session.exec(select(MediaFile).where(MediaFile.id.in_(set(file_ids))))).all()
    return {mf.id: mf for mf in rows}


async def list_review_items(session: AsyncSession, status: ChangeStatus) -> list[dict]:
    changes = (
        await session.exec(
            select(PendingChange).where(PendingChange.status == status).order_by(PendingChange.created_at.desc())
        )
    ).all()
    files_by_id = await _media_files_by_id(session, [c.file_id for c in changes])
    return [_effective_review(c, files_by_id.get(c.file_id)) for c in changes]


def _history_dict(entry: HistoryEntry, mf: MediaFile | None) -> dict:
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


async def history_item(session: AsyncSession, entry: HistoryEntry) -> dict:
    mf = await session.get(MediaFile, entry.file_id)
    return _history_dict(entry, mf)


async def list_history_items(session: AsyncSession, limit: int = 50) -> list[dict]:
    entries = (await session.exec(select(HistoryEntry).order_by(HistoryEntry.applied_at.desc()).limit(limit))).all()
    files_by_id = await _media_files_by_id(session, [h.file_id for h in entries])
    return [_history_dict(h, files_by_id.get(h.file_id)) for h in entries]


async def overview_stats(session: AsyncSession) -> dict:
    # Aggregated in SQL rather than fetching every row into Python just to
    # len()/sum() it — this runs on every page load (see
    # app/web.py::_base_context), so a full-table fetch-and-deserialize here
    # was paid on every single request regardless of the page.
    pending_count = (
        await session.exec(
            select(func.count()).select_from(PendingChange).where(PendingChange.status == ChangeStatus.pending)
        )
    ).one()
    queued_count = (
        await session.exec(
            select(func.count()).select_from(PendingChange).where(PendingChange.status == ChangeStatus.approved)
        )
    ).one()
    total_files = (await session.exec(select(func.count()).select_from(MediaFile))).one()
    total_applied_count = (await session.exec(select(func.count()).select_from(HistoryEntry))).one()
    total_bytes_reclaimed = (
        await session.exec(select(func.coalesce(func.sum(HistoryEntry.bytes_before - HistoryEntry.bytes_after), 0)))
    ).one()

    return {
        "total_files": total_files,
        "pending_review_count": pending_count,
        "queued_count": queued_count,
        "total_bytes_reclaimed": total_bytes_reclaimed,
        "total_applied_count": total_applied_count,
    }


def _track_label(p: dict) -> str:
    """How to refer to a track in the UI. Its title if it has one, otherwise
    its position and format ("s2 · SRT", "a1 · AC-3 5.1").

    A track with no title *and* no language showed as `""`, which names
    nothing and gives no way to find it again in a player. Its selector and
    format are always known.
    """
    title = p.get("old_title") or ""
    if title:
        return f'"{title}"'
    selector = p.get("track_selector") or ""
    fmt = p.get("format_label") or ""
    return " · ".join(bit for bit in (selector, fmt) if bit) or "untitled track"


def _describe_normalization(p: dict) -> str:
    """What this track's proposed change actually does, in words.

    The Normalize page used to render every change as
    `"<old title>" -> "<new title>"`, which for a change that only sets the
    default flag reads as `"English" -> "English"` — a pointless no-op that
    makes the whole proposal look broken, when what it really does is mark
    that track as the default.
    """
    parts = []
    old_title = p.get("old_title") or ""
    new_title = p.get("new_title") or ""
    if old_title != new_title:
        parts.append(f'{_track_label(p)} → "{new_title}"')

    old_language = p.get("old_language") or None
    new_language = p.get("new_language") or None
    if new_language != old_language:
        # Only ever happens for a track the file never labelled, where the
        # language was read out of the track's own text — worth saying so,
        # since this one is inferred rather than reformatted.
        parts.append(f"language identified as {new_language}" if not old_language else f"language {old_language} → {new_language}")

    new_default = p.get("new_default")
    if new_default is not None and bool(new_default) != bool(p.get("old_default")):
        parts.append("set as default" if new_default else "no longer default")

    return ", ".join(parts) or "no change"


def _unchanged_note(p: dict) -> str:
    """Short label for a track the normalizer left alone. "unchanged" is
    true of all of them but explains none: a track the cleaner is about to
    delete, one the user skipped, and one already correctly named are three
    different situations, and lumping them together made a proposal look
    like the normalizer had simply failed on those languages.
    """
    reason = p.get("reason") or ""
    if reason == SKIPPED_PENDING_REMOVAL:
        return "queued for removal"
    if reason == SKIPPED_BY_USER:
        return "you skipped this"
    if "identical to another track" in reason:
        return "kept, renaming would duplicate another track"
    if "not recognized" in reason or "no language tag" in reason:
        return "no usable language tag"
    return "already correct"


def _effective_normalize(change: NormalizationChange, mf: MediaFile | None) -> dict:
    overrides = set(change.overrides or [])
    effective = [
        {**p, "changed": False} if p["index"] in overrides and p["changed"] else p for p in change.proposed
    ]
    return {
        "id": change.id,
        "file_id": change.file_id,
        "path": mf.path if mf else None,
        "display_title": mf.display_title if mf else None,
        "library_type": mf.library_type.value if mf else None,
        "status": change.status.value,
        "proposed": change.proposed,
        "changes": [{**p, "summary": _describe_normalization(p)} for p in effective if p["changed"]],
        "unchanged": [
            {**p, "note": _unchanged_note(p), "label": _track_label(p)} for p in effective if not p["changed"]
        ],
        "error_message": change.error_message,
        "created_at": change.created_at,
        # When this proposal was last recomputed. A normalize pass only runs
        # on demand or from a schedule with Normalize enabled, so a proposal
        # can be arbitrarily old — and one computed under an older build can
        # look plainly wrong next to what the current normalizer would say.
        "updated_at": change.updated_at,
    }


async def normalize_item(session: AsyncSession, change: NormalizationChange) -> dict:
    mf = await session.get(MediaFile, change.file_id)
    return _effective_normalize(change, mf)


async def list_normalize_items(session: AsyncSession, status: ChangeStatus) -> list[dict]:
    changes = (
        await session.exec(
            select(NormalizationChange)
            .where(NormalizationChange.status == status)
            .order_by(NormalizationChange.created_at.desc())
        )
    ).all()
    files_by_id = await _media_files_by_id(session, [c.file_id for c in changes])
    return [_effective_normalize(c, files_by_id.get(c.file_id)) for c in changes]


async def normalize_stats(session: AsyncSession) -> dict:
    pending_count = (
        await session.exec(
            select(func.count())
            .select_from(NormalizationChange)
            .where(NormalizationChange.status == ChangeStatus.pending)
        )
    ).one()
    queued_count = (
        await session.exec(
            select(func.count())
            .select_from(NormalizationChange)
            .where(NormalizationChange.status == ChangeStatus.approved)
        )
    ).one()
    return {"pending_count": pending_count, "queued_count": queued_count}
