"""Wraps ffprobe to produce a normalized description of a media file's streams."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FFPROBE_BIN = "ffprobe"


class AnalyzerError(RuntimeError):
    """Raised when ffprobe fails or returns something we can't parse."""


@dataclass(frozen=True)
class MediaStream:
    index: int
    codec_type: str  # "video" | "audio" | "subtitle"
    codec_name: str
    language: str | None  # ISO 639-2 code (e.g. "eng"), None if untagged
    title: str | None
    channels: int | None  # audio only
    is_default: bool
    is_forced: bool
    is_commentary: bool
    is_hearing_impaired: bool
    # ffprobe's `visual_impaired` disposition — an audio-description track.
    # Read so the normalizer can keep that marker instead of retitling such a
    # track to a plain language name, indistinguishable from the main audio.
    is_visual_impaired: bool = False

    @classmethod
    def from_ffprobe_stream(cls, s: dict) -> "MediaStream":
        tags = s.get("tags", {}) or {}
        disposition = s.get("disposition", {}) or {}
        # ffprobe tags are case-inconsistent across containers (mkv lowercases,
        # some mp4 muxers don't) — look up case-insensitively.
        lower_tags = {k.lower(): v for k, v in tags.items()}
        language = lower_tags.get("language")
        if language in (None, "und", ""):
            language = None
        title = lower_tags.get("title") or None
        return cls(
            index=s["index"],
            codec_type=s.get("codec_type", "unknown"),
            codec_name=s.get("codec_name", "unknown"),
            language=language,
            title=title,
            channels=s.get("channels"),
            is_default=bool(disposition.get("default")),
            is_forced=bool(disposition.get("forced")),
            is_commentary=bool(disposition.get("comment")),
            is_hearing_impaired=bool(disposition.get("hearing_impaired")),
            is_visual_impaired=bool(disposition.get("visual_impaired")),
        )


@dataclass(frozen=True)
class MediaProbe:
    path: Path
    duration_seconds: float | None
    streams: list[MediaStream] = field(default_factory=list)

    @property
    def video_streams(self) -> list[MediaStream]:
        return [s for s in self.streams if s.codec_type == "video"]

    @property
    def audio_streams(self) -> list[MediaStream]:
        return [s for s in self.streams if s.codec_type == "audio"]

    @property
    def subtitle_streams(self) -> list[MediaStream]:
        return [s for s in self.streams if s.codec_type == "subtitle"]


def probe_file(path: Path, ffprobe_bin: str = FFPROBE_BIN) -> MediaProbe:
    """Run ffprobe on `path` and return its normalized stream layout.

    Read-only — never touches the file itself.
    """
    if not path.is_file():
        raise AnalyzerError(f"not a file: {path}")

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise AnalyzerError(f"ffprobe not found ({ffprobe_bin})") from e
    except subprocess.TimeoutExpired as e:
        raise AnalyzerError(f"ffprobe timed out probing {path}") from e

    if result.returncode != 0:
        raise AnalyzerError(f"ffprobe failed on {path}: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AnalyzerError(f"ffprobe returned invalid JSON for {path}") from e

    duration = None
    fmt = data.get("format", {})
    if fmt.get("duration") is not None:
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    streams = [MediaStream.from_ffprobe_stream(s) for s in data.get("streams", [])]
    return MediaProbe(path=path, duration_seconds=duration, streams=streams)
