"""Applies track metadata (title/language/default-flag) changes to MKV
files via mkvpropedit (MKVToolNix) — edits the Matroska header/track-
metadata section in place, without touching the audio/video/subtitle
stream data at all, so it's near-instant regardless of file size. Unlike
app/remux.py's `ffmpeg -c copy`, which always reads and rewrites the whole
file, this never does — that's the entire reason the normalizer uses a
different tool than the track remover (see TODO.md #7).

Only MKV is supported this way — mkvpropedit is Matroska-specific. Other
containers (MP4/MOV) don't guarantee the same in-place edit, and aren't
handled yet (see TODO.md #7's non-MKV fallback note).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.normalizer import TrackNormalization

MKVPROPEDIT_BIN = "mkvpropedit"
DEFAULT_TIMEOUT_SECONDS = 120  # in-place header edits are fast; generous anyway


class MkvMetadataError(RuntimeError):
    """Raised when mkvpropedit fails. mkvpropedit validates before writing
    and is not expected to leave a file partially edited on error, but this
    module makes no additional atomicity guarantee beyond what the tool
    itself provides.
    """


def is_mkv(path: Path) -> bool:
    return path.suffix.lower() == ".mkv"


def build_mkvpropedit_command(
    path: Path, changes: list[TrackNormalization], mkvpropedit_bin: str = MKVPROPEDIT_BIN
) -> list[str]:
    real_changes = [c for c in changes if c.changed]
    if not real_changes:
        raise MkvMetadataError("no changed tracks to write — refusing to run mkvpropedit with nothing to do")

    cmd = [mkvpropedit_bin, str(path)]
    for change in real_changes:
        cmd += ["--edit", f"track:{change.track_selector}"]
        if change.new_title != (change.old_title or ""):
            cmd += ["--set", f"name={change.new_title}"]
        if change.new_language and change.new_language != change.old_language:
            cmd += ["--set", f"language={change.new_language}"]
        if change.new_default is not None and change.new_default != change.old_default:
            cmd += ["--set", f"flag-default={1 if change.new_default else 0}"]
    return cmd


def apply_metadata_changes(
    path: Path,
    changes: list[TrackNormalization],
    *,
    mkvpropedit_bin: str = MKVPROPEDIT_BIN,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Runs mkvpropedit for every changed track in `changes` in one
    invocation. Returns the number of tracks actually written. Raises
    MkvMetadataError on failure; the original file's tracks are whatever
    mkvpropedit itself leaves them as (see the class docstring).
    """
    real_changes = [c for c in changes if c.changed]
    if not real_changes:
        return 0

    cmd = build_mkvpropedit_command(path, changes, mkvpropedit_bin=mkvpropedit_bin)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except FileNotFoundError as e:
        raise MkvMetadataError(f"mkvpropedit not found ({mkvpropedit_bin})") from e
    except subprocess.TimeoutExpired as e:
        raise MkvMetadataError(f"mkvpropedit timed out on {path.name}") from e

    if result.returncode != 0:
        raise MkvMetadataError(f"mkvpropedit failed on {path.name}: {result.stderr.strip()[-2000:]}")

    return len(real_changes)
