"""Wraps ffprobe to produce a normalized description of a media file's streams."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FFPROBE_BIN = "ffprobe"


class AnalyzerError(RuntimeError):
    """Raised when ffprobe fails or returns something we can't parse."""


def _parse_clock_or_seconds(raw: object) -> float | None:
    """Accepts either "HH:MM:SS.nnnnnnnnn" (matroska's DURATION tag) or a
    plain seconds float, since both spellings show up in ffprobe output.
    """
    if raw in (None, ""):
        return None
    try:
        parts = str(raw).split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return float(raw)
    except (TypeError, ValueError):
        return None


def _stream_duration_seconds(s: dict, lower_tags: dict, *, trust_stream_duration: bool) -> float | None:
    """One track's own length in seconds, or None when the container doesn't
    record one.

    The per-track DURATION *tag* wins wherever it exists, because in
    matroska ffprobe's `stream.duration` field is not per-track at all — it
    repeats the whole segment's duration on every stream. Trusting it would
    make a two-hour file's subtitle track look exactly as long as the
    longest audio track in the file, which is the specific thing
    app/remux.py::expected_duration_seconds must not be fooled by.

    `trust_stream_duration` is therefore False for matroska: a track with no
    DURATION tag there has an *unknown* length, which is honest and safe,
    rather than the container's length, which is a lie. mp4/mov do record
    real per-track durations in that field, so there it's used.
    """
    tagged = _parse_clock_or_seconds(lower_tags.get("duration"))
    if tagged is not None:
        return tagged
    if trust_stream_duration:
        return _parse_clock_or_seconds(s.get("duration"))
    return None


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
    # This individual track's length. Only read at remux-verify time (see
    # app/remux.py::verify_output), which needs the length of the tracks
    # being *kept* — a container's own duration is the longest track in it,
    # so it legitimately shrinks when the longest track is one being
    # dropped. None when the container doesn't record a per-track length.
    duration_seconds: float | None = None

    @classmethod
    def from_ffprobe_stream(cls, s: dict, *, trust_stream_duration: bool = True) -> "MediaStream":
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
            duration_seconds=_stream_duration_seconds(s, lower_tags, trust_stream_duration=trust_stream_duration),
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


# Subtitle codecs whose payload is text, so it can be read out and looked
# at. Bitmap subtitles (PGS, VobSub) would need OCR and are left alone.
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "subviewer", "text"})

FFMPEG_BIN = "ffmpeg"
SUBTITLE_SAMPLE_SECONDS = 900


def extract_subtitle_text(
    path: Path,
    stream_index: int,
    *,
    ffmpeg_bin: str = FFMPEG_BIN,
    sample_seconds: int = SUBTITLE_SAMPLE_SECONDS,
    timeout: int = 120,
) -> str:
    """The text of one subtitle track, as SRT, for the first
    `sample_seconds` of the timeline.

    Read-only, like everything else in this module — it decodes to stdout
    and never writes near the file. A sample rather than the whole track
    because identifying a language needs a paragraph, not a screenplay.
    Returns "" when the track can't be read, since a track that won't
    decode is simply one that can't be identified, not an error worth
    failing a whole normalize pass over.
    """
    cmd = [
        ffmpeg_bin, "-v", "error",
        "-t", str(sample_seconds),
        "-i", str(path),
        "-map", f"0:{stream_index}",
        "-f", "srt", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


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

    # See _stream_duration_seconds: matroska repeats the segment duration in
    # every stream's `duration` field instead of giving a real per-track one.
    format_name = fmt.get("format_name", "") or ""
    trust_stream_duration = "matroska" not in format_name and "webm" not in format_name

    streams = [
        MediaStream.from_ffprobe_stream(s, trust_stream_duration=trust_stream_duration)
        for s in data.get("streams", [])
    ]
    return MediaProbe(path=path, duration_seconds=duration, streams=streams)
