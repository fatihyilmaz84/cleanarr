"""Lossless track removal via ffmpeg stream copy (`-c copy`) — no decode, no
re-encode, no quality loss. Only the container is rewritten to drop streams;
video and the streams that survive are copied byte-for-byte.

Safety properties this module enforces (see plan: array is ~99% full, no
backup/quarantine, so these are the only safety net):
  - preflight free-space check before touching anything
  - temp output written on the SAME filesystem as the source (required for an
    atomic replace, and to avoid a slow/space-doubling cross-disk copy)
  - the output is re-probed and sanity-checked (the exact set of
    video/audio/subtitle tracks that should have survived, and the length of
    the longest kept track) before the original is replaced
  - the original is only ever replaced via one atomic `os.replace`
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.analyzer import MediaProbe, probe_file
from app.rules import StreamDecision

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

# The track types this app actually decides about. Anything else in a
# container (mp4 chapter/timecode `data` tracks, mkv font attachments) is
# muxer bookkeeping — carried through, but not something to verify counts
# of, since a muxer may legitimately add or drop its own.
MANAGED_STREAM_TYPES = frozenset({"video", "audio", "subtitle"})

DEFAULT_TIMEOUT_SECONDS = 1800  # generous: array writes can be slow with parity
DURATION_TOLERANCE_SECONDS = 2.0
FREE_SPACE_BUFFER_BYTES = 200 * 1024 * 1024  # 200MB cushion on top of file size


class RemuxError(RuntimeError):
    """Raised whenever it's not safe to proceed, or the remux/verify failed.
    The original file is never touched when this is raised.
    """


@dataclass(frozen=True)
class RemuxResult:
    applied: bool
    reason: str
    bytes_before: int | None = None
    bytes_after: int | None = None
    streams_removed: list[dict] = field(default_factory=list)


def _tmp_path_for(source: Path) -> Path:
    # Same directory as source => same filesystem => atomic os.replace works,
    # and the hidden dot-prefix keeps it out of *arr/Jellyfin library scans
    # while it's being written.
    return source.parent / f".cleanarr.tmp.{source.name}"


def preflight_check(source: Path, safety_margin_bytes: int = FREE_SPACE_BUFFER_BYTES) -> None:
    """Refuse to proceed unless there's enough free space on `source`'s
    filesystem to hold a second copy of it (worst case: old + new briefly
    coexist) plus a safety cushion.
    """
    required = source.stat().st_size + safety_margin_bytes
    free = shutil.disk_usage(source.parent).free
    if free < required:
        raise RemuxError(
            f"insufficient free space to safely remux {source.name}: "
            f"need ~{required / 1e9:.2f}GB, have {free / 1e9:.2f}GB free"
        )


def build_ffmpeg_command(
    source: Path, dest: Path, decisions: list[StreamDecision], ffmpeg_bin: str = FFMPEG_BIN
) -> list[str]:
    kept = [d for d in decisions if d.keep]
    if not kept:
        raise RemuxError("rule engine produced zero streams to keep — refusing to write an empty file")

    cmd = [ffmpeg_bin, "-y", "-i", str(source)]
    for d in kept:
        cmd += ["-map", f"0:{d.stream.index}"]
    cmd += ["-map_metadata", "0", "-c", "copy", str(dest)]
    return cmd


def _identity(stream) -> tuple[str, str, str | None]:
    """What makes a track recognizably "the same track" across a stream
    copy. Codec and language both survive `-c copy` untouched, so this
    triple is stable — and specific enough to tell two same-language audio
    tracks apart when they differ in codec (ac3 vs aac).
    """
    return (stream.codec_type, stream.codec_name, stream.language)


def expected_duration_seconds(original: MediaProbe, decisions: list[StreamDecision]) -> float | None:
    """How long the remuxed file *should* be: the longest track we're
    keeping, not the length of the original container.

    A container's duration is its longest track. Dropping that track — a
    subtitle whose last cue runs past the credits, a dubbed audio track
    with trailing padding — legitimately makes the output shorter, which
    is not corruption. Falls back to the original container duration when
    the container records no per-track lengths.
    """
    kept = [d.stream.duration_seconds for d in decisions if d.keep and d.stream.duration_seconds is not None]
    return max(kept) if kept else original.duration_seconds


def verify_output(
    original: MediaProbe,
    output_path: Path,
    decisions: list[StreamDecision],
    ffprobe_bin: str = FFPROBE_BIN,
) -> None:
    """Confirm the remuxed file really is the original minus exactly the
    tracks that were meant to go — checked before the original is replaced,
    so a mismatch costs nothing but a discarded temp file.

    Only video/audio/subtitle are compared. Muxers synthesize their own
    bookkeeping tracks that were never in our `-map` list — the mp4 muxer
    emits a `text`/`bin_data` chapter track of its own whenever the input
    has chapters — so counting those would fail a remux that is in fact
    perfectly correct.
    """
    result = probe_file(output_path, ffprobe_bin=ffprobe_bin)

    expected = Counter(_identity(d.stream) for d in decisions if d.keep and d.stream.codec_type in MANAGED_STREAM_TYPES)
    actual = Counter(_identity(s) for s in result.streams if s.codec_type in MANAGED_STREAM_TYPES)
    if expected != actual:
        missing = expected - actual
        unexpected = actual - expected
        detail = []
        if missing:
            detail.append("missing " + ", ".join(f"{t}/{c}/{lang or 'und'}" for t, c, lang in missing.elements()))
        if unexpected:
            detail.append("unexpected " + ", ".join(f"{t}/{c}/{lang or 'und'}" for t, c, lang in unexpected.elements()))
        raise RemuxError(f"verification failed for {output_path.name}: {'; '.join(detail)}")

    expected_duration = expected_duration_seconds(original, decisions)
    if expected_duration is not None and result.duration_seconds is not None:
        delta = abs(expected_duration - result.duration_seconds)
        if delta > DURATION_TOLERANCE_SECONDS:
            raise RemuxError(
                f"verification failed for {output_path.name}: duration changed by "
                f"{delta:.1f}s (expected ~{expected_duration:.1f}s from the kept tracks, "
                f"remuxed {result.duration_seconds:.1f}s)"
            )


def _watch_output_growth(
    tmp_path: Path, expected_bytes: int, progress_cb: Callable[[float], None], stop_event: threading.Event
) -> None:
    """`-c copy` writes the output progressively — polling its size against
    the source's size (a reasonable proxy, since dropped audio/subtitle
    tracks are a small fraction of a video-dominated file) is a way to show
    real remux progress without touching the ffmpeg invocation itself.
    Capped below 1.0 — only the caller marks true completion, after
    verification and the atomic replace.
    """
    while not stop_event.wait(1.0):
        try:
            size = tmp_path.stat().st_size
        except FileNotFoundError:
            continue
        if expected_bytes > 0:
            progress_cb(min(size / expected_bytes, 0.99))


def apply_remux(
    source: Path,
    decisions: list[StreamDecision],
    *,
    ffmpeg_bin: str = FFMPEG_BIN,
    ffprobe_bin: str = FFPROBE_BIN,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    progress_cb: Callable[[float], None] | None = None,
) -> RemuxResult:
    dropped = [d for d in decisions if not d.keep]
    if not dropped:
        return RemuxResult(applied=False, reason="nothing to drop, file already matches rules")

    original_probe = probe_file(source, ffprobe_bin=ffprobe_bin)
    bytes_before = source.stat().st_size

    preflight_check(source)

    tmp_path = _tmp_path_for(source)
    cmd = build_ffmpeg_command(source, tmp_path, decisions, ffmpeg_bin=ffmpeg_bin)

    stop_event = threading.Event()
    watcher = None
    if progress_cb is not None:
        watcher = threading.Thread(
            target=_watch_output_growth, args=(tmp_path, bytes_before, progress_cb, stop_event), daemon=True
        )
        watcher.start()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        if result.returncode != 0:
            raise RemuxError(f"ffmpeg failed on {source.name}: {result.stderr.strip()[-2000:]}")

        verify_output(original_probe, tmp_path, decisions, ffprobe_bin=ffprobe_bin)

        bytes_after = tmp_path.stat().st_size
        os.replace(tmp_path, source)
    finally:
        stop_event.set()
        if watcher is not None:
            watcher.join(timeout=2)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    if progress_cb is not None:
        progress_cb(1.0)

    return RemuxResult(
        applied=True,
        reason="remuxed successfully",
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        streams_removed=[
            {
                "index": d.stream.index,
                "type": d.stream.codec_type,
                "codec": d.stream.codec_name,
                "language": d.stream.language,
                "title": d.stream.title,
                "reason": d.reason,
            }
            for d in dropped
        ],
    )
