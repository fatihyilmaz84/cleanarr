"""Pure decision logic for the track metadata normalizer: given a file's
streams and a NormalizerConfig, decide the canonical title/language/
default-flag each surviving track should have. No I/O, no side effects —
mirrors app/rules.py's split between "decide" and "apply".

This is a separate, independent system from app/rules.py's drop engine
(see TODO.md #7) — it renames/retags tracks, never removes them. The two
interact at exactly one point: app/normalize_service.py excludes any
stream index the drop engine already proposes removing for that file, and
protects any stream a user has selected for normalization from being
dropped, by writing into the same PendingChange.overrides mechanism
app/rules.py::apply_overrides already reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.analyzer import MediaStream
from app.languages import iso_codes_for_language_name, language_name_for_code

_TRACK_TYPE_LETTER = {"video": "v", "audio": "a", "subtitle": "s"}


class NormalizerConfig(BaseModel):
    """User-editable, independent of RuleConfig — the normalizer is its own
    menu item with its own settings, not a variant of Rules (see TODO.md
    #7's "Architecture" note).
    """

    naming_style: str = "dash"  # "dash" -> "English - SDH", "space" -> "English SDH"

    auto_default_audio: bool = False
    auto_default_subtitle: bool = False
    preferred_audio_language: str = ""
    preferred_subtitle_language: str = ""

    # Forced/Foreign/Forced Narrative/Signs & Songs are NOT assumed
    # equivalent unless explicitly enabled (per the original spec) — off by
    # default, matching this app's general "never assume the aggressive
    # option" stance.
    forced_equivalents_enabled: bool = False
    forced_equivalent_patterns: list[str] = Field(
        default_factory=lambda: ["foreign", "forced.?narrative", "signs.*songs"]
    )

    # Same title-text-fallback mechanism as RuleConfig's commentary/
    # hearing-impaired patterns (app/rules.py) — a separate config instance
    # by design, so the two systems stay decoupled, even though the
    # matching mechanism (_matches_pattern below) is identical in shape.
    commentary_title_patterns: list[str] = Field(default_factory=lambda: ["commentary"])
    hearing_impaired_title_patterns: list[str] = Field(default_factory=lambda: ["sdh", "hearing.impaired"])
    cc_title_patterns: list[str] = Field(default_factory=lambda: ["closed.caption"])
    original_title_patterns: list[str] = Field(default_factory=lambda: ["original"])
    dubbed_title_patterns: list[str] = Field(default_factory=lambda: ["dubbed", "\\bdub\\b"])


@dataclass(frozen=True)
class TrackNormalization:
    index: int  # ffprobe stream index — matches StreamRecord.stream_index
    codec_type: str
    track_selector: str  # mkvpropedit "track:aN/sN/vN" selector, 1-based within its type
    old_title: str | None
    new_title: str
    old_language: str | None
    new_language: str | None
    old_default: bool
    new_default: bool | None  # None = leave the default flag alone
    changed: bool
    reason: str


def _matches_pattern(title: str | None, patterns: list[str]) -> str | None:
    if not title:
        return None
    for pattern in patterns:
        try:
            if re.search(pattern, title, re.IGNORECASE):
                return pattern
        except re.error:
            continue
    return None


def _build_title(language_name: str, attributes: list[str], style: str) -> str:
    if not attributes:
        return language_name
    joined = " ".join(attributes)
    return f"{language_name} - {joined}" if style != "space" else f"{language_name} {joined}"


def _audio_attributes(stream: MediaStream, config: NormalizerConfig) -> list[str]:
    attributes = []
    if stream.is_commentary or _matches_pattern(stream.title, config.commentary_title_patterns):
        attributes.append("Commentary")
    if _matches_pattern(stream.title, config.original_title_patterns):
        attributes.append("Original")
    if _matches_pattern(stream.title, config.dubbed_title_patterns):
        attributes.append("Dubbed")
    return attributes


def _subtitle_attributes(stream: MediaStream, config: NormalizerConfig) -> list[str]:
    attributes = []
    is_forced = stream.is_forced or (
        config.forced_equivalents_enabled and bool(_matches_pattern(stream.title, config.forced_equivalent_patterns))
    )
    if is_forced:
        attributes.append("Forced")
    if stream.is_hearing_impaired or _matches_pattern(stream.title, config.hearing_impaired_title_patterns):
        attributes.append("SDH")
    elif _matches_pattern(stream.title, config.cc_title_patterns):
        # SDH takes priority when a track somehow matches both — they're
        # rendered as mutually exclusive labels, never combined.
        attributes.append("CC")
    return attributes


def _pick_default_index(streams: list[MediaStream], codec_type: str, preferred_codes: frozenset[str]) -> int | None:
    """Which single stream of `codec_type` should become the default when
    auto-default is enabled — the already-default track if it already
    matches the preferred language, else the first match. None if nothing
    matches (leaves every default flag exactly as it is).
    """
    if not preferred_codes:
        return None
    candidates = [s for s in streams if s.codec_type == codec_type and s.language and s.language.lower() in preferred_codes]
    if not candidates:
        return None
    already_default = next((s for s in candidates if s.is_default), None)
    return (already_default or candidates[0]).index


def normalize_streams(streams: list[MediaStream], config: NormalizerConfig) -> list[TrackNormalization]:
    """Evaluate every audio/subtitle stream against `config`. Video and
    unrecognized stream types are never touched — this only ever retitles/
    retags audio and subtitle tracks, same scope as app/rules.py's drop
    engine.
    """
    preferred_audio_codes = iso_codes_for_language_name(config.preferred_audio_language)
    preferred_subtitle_codes = iso_codes_for_language_name(config.preferred_subtitle_language)
    audio_default_index = _pick_default_index(streams, "audio", preferred_audio_codes) if config.auto_default_audio else None
    subtitle_default_index = (
        _pick_default_index(streams, "subtitle", preferred_subtitle_codes) if config.auto_default_subtitle else None
    )

    type_counts: dict[str, int] = {}
    results: list[TrackNormalization] = []

    for stream in streams:
        if stream.codec_type not in ("audio", "subtitle"):
            continue

        type_counts[stream.codec_type] = type_counts.get(stream.codec_type, 0) + 1
        track_selector = f"{_TRACK_TYPE_LETTER[stream.codec_type]}{type_counts[stream.codec_type]}"

        language_name = language_name_for_code(stream.language)
        if language_name is None:
            results.append(
                TrackNormalization(
                    index=stream.index,
                    codec_type=stream.codec_type,
                    track_selector=track_selector,
                    old_title=stream.title,
                    new_title=stream.title or "",
                    old_language=stream.language,
                    new_language=stream.language,
                    old_default=stream.is_default,
                    new_default=None,
                    changed=False,
                    reason=f"language '{stream.language}' not recognized, left untouched"
                    if stream.language
                    else "no language tag, left untouched",
                )
            )
            continue

        attributes = _audio_attributes(stream, config) if stream.codec_type == "audio" else _subtitle_attributes(stream, config)
        new_title = _build_title(language_name, attributes, config.naming_style)

        if stream.codec_type == "audio" and audio_default_index is not None:
            new_default = stream.index == audio_default_index
        elif stream.codec_type == "subtitle" and subtitle_default_index is not None:
            new_default = stream.index == subtitle_default_index
        else:
            new_default = None

        title_changed = new_title != (stream.title or "")
        default_changed = new_default is not None and new_default != stream.is_default
        changed = title_changed or default_changed

        reason_parts = []
        if title_changed:
            reason_parts.append(f"title '{stream.title or ''}' -> '{new_title}'")
        if default_changed:
            reason_parts.append(f"default -> {new_default}")
        reason = "; ".join(reason_parts) if reason_parts else "already normalized"

        results.append(
            TrackNormalization(
                index=stream.index,
                codec_type=stream.codec_type,
                track_selector=track_selector,
                old_title=stream.title,
                new_title=new_title,
                old_language=stream.language,
                new_language=stream.language,  # normalization never changes the language code itself, only how it's displayed
                old_default=stream.is_default,
                new_default=new_default,
                changed=changed,
                reason=reason,
            )
        )

    return results


def apply_overrides(normalizations: list[TrackNormalization], skip_indices: list[int] | None) -> list[TrackNormalization]:
    """Force a stream to `changed=False` (skip it) regardless of what was
    proposed — mirrors app/rules.py::apply_overrides in shape, but here an
    override means "don't touch this track" rather than "force-keep it".
    """
    if not skip_indices:
        return normalizations
    skip_set = set(skip_indices)
    return [
        TrackNormalization(
            n.index, n.codec_type, n.track_selector, n.old_title, n.old_title or "", n.old_language, n.old_language,
            n.old_default, None, False, "skipped by user override",
        )
        if n.index in skip_set
        else n
        for n in normalizations
    ]
