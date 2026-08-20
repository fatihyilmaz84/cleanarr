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

from app.analyzer import TEXT_SUBTITLE_CODECS, AnalyzerError, MediaStream, extract_subtitle_text, probe_file
from app.language_detect import detect_language
from app.mkv_metadata import MkvMetadataError, apply_metadata_changes, is_mkv
from app.models import ChangeStatus, MediaFile, NormalizationChange, PendingChange, StreamRecord
from app.normalizer import (
    SKIPPED_PENDING_REMOVAL,
    NormalizerConfig,
    TrackNormalization,
    apply_overrides,
    normalize_streams,
)


def _now():
    return datetime.now(timezone.utc)


@dataclass
class NormalizeScanSummary:
    files_total: int = 0  # known upfront, for progress reporting
    files_considered: int = 0
    files_with_changes: int = 0
    # Files skipped because their container can't be edited in place (see
    # app/mkv_metadata.py — mkvpropedit is Matroska-only). Counted rather
    # than proposed: proposing a change that apply is guaranteed to reject
    # just fills the queue with items that always fail.
    files_unsupported_container: int = 0
    # NormalizationChange.id for every change this run itself created or
    # updated — lets a scheduled normalize_auto_apply act on exactly what
    # this run produced instead of re-querying and sweeping up unrelated
    # pending suggestions. Mirrors ScanSummary.pending_change_ids.
    change_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # True if a `deadline` was hit before every file could be considered —
    # not an error, the rest is just left for the next run.
    stopped_early: bool = False


def _stream_from_record(record: StreamRecord) -> MediaStream:
    return MediaStream(
        index=record.stream_index,
        codec_type=record.codec_type,
        codec_name=record.codec_name,
        language=record.language,
        title=record.title,
        channels=record.channels,
        is_default=record.is_default,
        is_forced=record.is_forced,
        is_commentary=record.is_commentary,
        is_hearing_impaired=record.is_hearing_impaired,
        is_visual_impaired=record.is_visual_impaired,
    )


def _record_like(stream: MediaStream) -> StreamRecord:
    """A throwaway StreamRecord view of a freshly probed stream, so
    _detect_missing_languages can run against a file whose cached rows are
    known to be out of date. Never added to the session — pass persist=False.
    """
    return StreamRecord(
        file_id=0,
        stream_index=stream.index,
        codec_type=stream.codec_type,
        codec_name=stream.codec_name,
        language=stream.language,
        title=stream.title,
    )


async def _detect_missing_languages(
    session: AsyncSession, media_file: MediaFile, records: list[StreamRecord], *, persist: bool = True
) -> dict[int, str]:
    """Work out the language of any text subtitle the file labelled with
    neither a language nor a title, by reading the track's own text.

    Results are stored on the StreamRecord so this decodes each track once
    ever, not once per pass: an empty string records "looked, couldn't tell"
    and is as important to persist as a positive answer, since otherwise
    every pass would re-decode the same unidentifiable tracks forever.
    """
    detected: dict[int, str] = {}
    for record in records:
        if record.detected_language:
            detected[record.stream_index] = record.detected_language
            continue
        if record.detected_language is not None:
            continue  # "" — already looked, nothing conclusive
        if record.codec_type != "subtitle" or record.language or (record.title or "").strip():
            continue
        if record.codec_name not in TEXT_SUBTITLE_CODECS:
            continue  # bitmap subtitles would need OCR

        text = await asyncio.to_thread(extract_subtitle_text, Path(media_file.path), record.stream_index)
        code = detect_language(text) if text else None
        record.detected_language = code or ""
        if persist:
            session.add(record)
        if code:
            detected[record.stream_index] = code
    return detected


async def _detected_languages_for_apply(
    session: AsyncSession, media_file: MediaFile, path: Path, probe, config: NormalizerConfig
) -> dict[int, str]:
    """Languages for the untagged tracks of a file about to be written.

    Reading a track's text costs an ffmpeg decode of the subtitle stream, and
    propose already did it and stored the answer — so this reuses that
    whenever the file is byte-for-byte what it was then. Re-deriving it every
    apply would decode every untagged track in the library again for
    information already on hand.

    When the file *has* changed since (an *arr upgrade — the same case that
    makes the re-probe above mandatory), the stored answer describes a
    different file's tracks and must not be written onto this one's, so it
    is worked out again from what is actually there now.
    """
    if not config.detect_subtitle_language:
        return {}

    stat = path.stat()
    if media_file.size_bytes == stat.st_size and media_file.mtime == stat.st_mtime:
        records = (await session.exec(select(StreamRecord).where(StreamRecord.file_id == media_file.id))).all()
        return await _detect_missing_languages(session, media_file, records, persist=True)

    return await _detect_missing_languages(
        session, media_file, [_record_like(s) for s in probe.streams], persist=False
    )


def _dropped_indices_from_change(change: PendingChange | None) -> set[int]:
    """Stream indices app/rules.py's drop engine currently proposes removing
    for a file (given its pending-or-approved PendingChange row, if any) —
    excluded from normalization *output* (see propose_normalizations/
    apply_normalization_change below), since there's nothing left to
    retitle once a track is actually gone. Deliberately NOT used to filter
    the *input* to normalize_streams() — its mkvpropedit track selectors
    are positional, computed from a track's real rank among the file's
    physical tracks, so trimming the input list before computing them
    would shift every later same-type selector and corrupt which physical
    track gets edited. The exclusion is applied afterwards instead, via
    apply_overrides — see the callers.
    """
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
        "format_label": n.format_label,
    }


async def propose_normalizations(
    session: AsyncSession,
    config: NormalizerConfig,
    progress_cb: Callable[[NormalizeScanSummary], None] | None = None,
    deadline: datetime | None = None,
    normalizer_preset_id: str | None = None,
) -> NormalizeScanSummary:
    """`normalizer_preset_id` identifies which saved NormalizerPreset
    `config` came from (None = Default) and is stamped onto each proposed
    change so applying it re-decides with the same config. `deadline`, if
    given, stops the pass between files — this is a pure DB+CPU pass with
    no partial-write risk, so stopping is always safe here.
    """
    summary = NormalizeScanSummary()

    media_files = (await session.exec(select(MediaFile))).all()
    summary.files_total = len(media_files)
    if not media_files:
        return summary

    file_ids = [mf.id for mf in media_files]

    # Batch-fetch everything up front instead of one query per file inside
    # the loop (an N+1 pattern that meant ~3 queries + a commit per file —
    # for a several-thousand-file library, tens of thousands of round
    # trips for what should be a fast, ffprobe-free pass).
    all_records = (
        await session.exec(
            select(StreamRecord).where(StreamRecord.file_id.in_(file_ids)).order_by(StreamRecord.stream_index)
        )
    ).all()
    records_by_file: dict[int, list[StreamRecord]] = {}
    for r in all_records:
        records_by_file.setdefault(r.file_id, []).append(r)

    active_pending_changes = (
        await session.exec(
            select(PendingChange).where(
                PendingChange.file_id.in_(file_ids),
                PendingChange.status.in_([ChangeStatus.pending, ChangeStatus.approved]),
            )
        )
    ).all()
    pending_change_by_file: dict[int, PendingChange] = {c.file_id: c for c in active_pending_changes}

    # Pending *and* approved — a change already queued must be updated in
    # place on re-propose, not left alone while a second row is created for
    # the same file (the duplicate-queue-entry bug fixed in app/scanner.py;
    # the normalizer had the same shape).
    # `skipped` is matched too, so a file the user explicitly declined isn't
    # silently resurrected as a fresh pending row on the next pass. It's
    # only re-surfaced if the *suggestion itself* changed (different config,
    # or the file's tracks changed) — see the loop below.
    existing_norm_changes = (
        await session.exec(
            select(NormalizationChange).where(
                NormalizationChange.file_id.in_(file_ids),
                NormalizationChange.status.in_(
                    [ChangeStatus.pending, ChangeStatus.approved, ChangeStatus.skipped]
                ),
            )
        )
    ).all()
    existing_norm_by_file: dict[int, NormalizationChange] = {c.file_id: c for c in existing_norm_changes}

    for media_file in media_files:
        if deadline is not None and _now() >= deadline:
            summary.stopped_early = True
            break

        summary.files_considered += 1
        records = records_by_file.get(media_file.id, [])
        if not records:
            if progress_cb:
                progress_cb(summary)
            continue

        # Containers mkvpropedit can't edit are filtered out *here* rather
        # than only at apply time — otherwise every pass proposes changes
        # that apply is guaranteed to reject, and because a `failed` row
        # isn't matched above, each pass would add another doomed row.
        if not is_mkv(Path(media_file.path)):
            summary.files_unsupported_container += 1
            stale = existing_norm_by_file.get(media_file.id)
            if stale is not None and stale.status != ChangeStatus.skipped:
                await session.delete(stale)
            if progress_cb:
                progress_cb(summary)
            continue

        # Compute selectors from every physical track (correct positional
        # numbering), then exclude anything currently proposed for removal
        # as an override — never by trimming the input list. See
        # _dropped_indices_from_change's docstring.
        all_streams = [_stream_from_record(r) for r in records]
        dropped = _dropped_indices_from_change(pending_change_by_file.get(media_file.id))
        detected = (
            # Skipping the tracks already queued for removal: decoding one to
            # work out a language for a track about to be deleted is pure
            # waste, and apply_overrides discards the answer immediately below.
            await _detect_missing_languages(
                session, media_file, [r for r in records if r.stream_index not in dropped]
            )
            if config.detect_subtitle_language
            else {}
        )
        normalizations = normalize_streams(all_streams, config, detected_languages=detected)
        normalizations = apply_overrides(normalizations, sorted(dropped), reason=SKIPPED_PENDING_REMOVAL)
        changed = [n for n in normalizations if n.changed]

        existing = existing_norm_by_file.get(media_file.id)

        if changed:
            proposed = [_serialize(n) for n in normalizations]
            if existing is not None and existing.status == ChangeStatus.skipped:
                if existing.proposed == proposed:
                    # Same suggestion the user already declined — leave it
                    # declined instead of re-queueing it every pass.
                    if progress_cb:
                        progress_cb(summary)
                    continue
                # The suggestion itself changed (different preset, or the
                # file's tracks changed), so it's worth asking again.
                existing.status = ChangeStatus.pending

            summary.files_with_changes += 1
            if existing:
                existing.proposed = proposed
                # Re-stamped, not preserved — same reasoning as
                # app/scanner.py: the proposal was just recomputed under
                # *this* config, so applying it must use that config.
                existing.normalizer_preset_id = normalizer_preset_id
                existing.updated_at = _now()
                session.add(existing)
                await session.flush()
                if existing.status == ChangeStatus.pending:
                    summary.change_ids.append(existing.id)
            else:
                new_change = NormalizationChange(
                    file_id=media_file.id,
                    status=ChangeStatus.pending,
                    proposed=proposed,
                    normalizer_preset_id=normalizer_preset_id,
                )
                session.add(new_change)
                await session.flush()
                summary.change_ids.append(new_change.id)
        elif existing:
            # File now matches the naming scheme (e.g. config changed since
            # it was queued) — the stale suggestion no longer applies.
            await session.delete(existing)

        if progress_cb:
            progress_cb(summary)

    await session.commit()
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

    if not is_mkv(path):
        message = "only MKV files are supported for normalization right now"
        change.status = ChangeStatus.failed
        change.error_message = message
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, False, message)

    # Re-probe rather than trusting the cached StreamRecord rows, exactly as
    # app/apply.py does before a remux. mkvpropedit's track selectors are
    # *positional* ("second audio track"), so if the file was replaced since
    # the scan — an *arr upgrade is the common case — cached rows would aim
    # those selectors at whatever now sits in that position and retitle the
    # wrong track. Metadata-only, so recoverable, but silently wrong.
    try:
        probe = await asyncio.to_thread(probe_file, path)
    except AnalyzerError as e:
        change.status = ChangeStatus.failed
        change.error_message = str(e)
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, False, str(e))

    active_change = (
        await session.exec(
            select(PendingChange).where(
                PendingChange.file_id == media_file.id,
                PendingChange.status.in_([ChangeStatus.pending, ChangeStatus.approved]),
            )
        )
    ).first()
    dropped = _dropped_indices_from_change(active_change)

    all_streams = probe.streams
    detected = await _detected_languages_for_apply(session, media_file, path, probe, config)
    normalizations = normalize_streams(all_streams, config, detected_languages=detected)
    # Both exclusions — currently-proposed-for-removal and the user's own
    # skip selections — are applied the same way, after full-file selector
    # computation, for the same reason: neither may safely trim the input.
    skip_indices = sorted(dropped | set(change.overrides or []))
    normalizations = apply_overrides(normalizations, skip_indices)
    changed = [n for n in normalizations if n.changed]

    if not changed:
        change.status = ChangeStatus.applied
        change.error_message = None
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, True, "nothing to change, file already matches the naming scheme")

    try:
        tracks_updated = await asyncio.to_thread(apply_metadata_changes, path, changed)
    except MkvMetadataError as e:
        change.status = ChangeStatus.failed
        change.error_message = str(e)
        session.add(change)
        await session.commit()
        return NormalizeApplyResult(change_id, False, str(e))

    # Rebuild the cached StreamRecord rows from the probe we just took, with
    # the titles/flags we just wrote folded in — the probe is the truth about
    # this file's track layout, and it may differ from what was cached (that
    # being exactly why we re-probed above).
    stale_records = (
        await session.exec(select(StreamRecord).where(StreamRecord.file_id == media_file.id))
    ).all()
    for record in stale_records:
        await session.delete(record)

    by_index = {n.index: n for n in changed}
    for s in probe.streams:
        n = by_index.get(s.index)
        session.add(
            StreamRecord(
                file_id=media_file.id,
                stream_index=s.index,
                codec_type=s.codec_type,
                codec_name=s.codec_name,
                # The probe was taken *before* mkvpropedit ran, so for a
                # track that just had its language written, s.language is
                # the old empty value — fold in what was written, exactly as
                # the title and default flag beside it already do.
                language=(n.new_language if n is not None and n.new_language else s.language),
                title=n.new_title if n is not None else s.title,
                channels=s.channels,
                is_default=(n.new_default if n is not None and n.new_default is not None else s.is_default),
                is_forced=s.is_forced,
                is_commentary=s.is_commentary,
                is_hearing_impaired=s.is_hearing_impaired,
                is_visual_impaired=s.is_visual_impaired,
            )
        )

    change.status = ChangeStatus.applied
    change.error_message = None
    session.add(change)
    await session.commit()

    return NormalizeApplyResult(change_id, True, "applied", tracks_updated=tracks_updated)
