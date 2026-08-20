"""Shared fixture builders for tests — small helpers, not real media files."""

from __future__ import annotations

from pathlib import Path

from app.analyzer import MediaProbe, MediaStream


def make_stream(
    index: int,
    codec_type: str,
    codec_name: str = "aac",
    language: str | None = "eng",
    title: str | None = None,
    channels: int | None = None,
    is_default: bool = False,
    is_forced: bool = False,
    is_commentary: bool = False,
    is_hearing_impaired: bool = False,
    is_visual_impaired: bool = False,
    duration_seconds: float | None = None,
) -> MediaStream:
    return MediaStream(
        index=index,
        codec_type=codec_type,
        codec_name=codec_name,
        language=language,
        title=title,
        channels=channels,
        is_default=is_default,
        is_forced=is_forced,
        is_commentary=is_commentary,
        is_hearing_impaired=is_hearing_impaired,
        is_visual_impaired=is_visual_impaired,
        duration_seconds=duration_seconds,
    )


def make_probe(streams: list[MediaStream], duration_seconds: float | None = 5400.0, path: str = "movie.mkv") -> MediaProbe:
    return MediaProbe(path=Path(path), duration_seconds=duration_seconds, streams=streams)


# A representative multi-track movie: video + eng/tur/jpn audio (jpn is
# commentary) + eng/tur/eng-forced subtitles + one untagged audio track.
def typical_movie_streams() -> list[MediaStream]:
    return [
        make_stream(0, "video", codec_name="h264", language=None, is_default=True),
        make_stream(1, "audio", codec_name="ac3", language="eng", channels=6, is_default=True),
        make_stream(2, "audio", codec_name="ac3", language="tur", channels=6),
        make_stream(3, "audio", codec_name="aac", language="eng", title="Director's Commentary", is_commentary=True),
        make_stream(4, "audio", codec_name="aac", language=None, title="Unknown Track"),
        make_stream(5, "subtitle", codec_name="subrip", language="eng"),
        make_stream(6, "subtitle", codec_name="subrip", language="eng", title="English (Forced)", is_forced=True),
        make_stream(7, "subtitle", codec_name="subrip", language="tur"),
        make_stream(8, "subtitle", codec_name="subrip", language="jpn"),
    ]
