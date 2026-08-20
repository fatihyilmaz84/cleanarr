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

import dataclasses
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.analyzer import MediaStream
from app.languages import iso_codes_for_language_name, language_name_for_code
from app.text_patterns import matches_any_pattern

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

    # A subtitle whose title literally says "forced" IS a forced track —
    # that's the marker itself, not an assumed equivalent, so it's always
    # honoured alongside ffprobe's disposition flag. Many real releases set
    # only the title and never the flag ("English Forced", "Forced ENG"),
    # and dropping that marker silently breaks a player's forced-subtitle
    # auto-selection.
    forced_title_patterns: list[str] = Field(default_factory=lambda: ["forced"])

    # Foreign/Forced Narrative/Signs & Songs are a *different* claim — that
    # those labels MEAN forced — and stay opt-in (per the original spec),
    # matching this app's general "never assume the aggressive option"
    # stance. Unlike forced_title_patterns above, these are guesses.
    forced_equivalents_enabled: bool = False
    forced_equivalent_patterns: list[str] = Field(
        default_factory=lambda: ["foreign", "forced.?narrative", "signs.*songs"]
    )

    # Same title-text-fallback mechanism as RuleConfig's commentary/
    # hearing-impaired patterns (app/rules.py) — a separate config instance
    # by design, so the two systems stay decoupled, even though the
    # matching mechanism (app/text_patterns.py::matches_any_pattern) is
    # identical in shape.
    # Every pattern set here must also match the label this module emits for
    # it (ATTR_* below), or normalization isn't idempotent: pass 1 rewrites
    # "Closed Captions" -> "English - CC", then pass 2 no longer recognizes
    # "CC" and strips it back to "English". Hence the "\bcc\b" / "\bad\b"
    # entries alongside the long forms — they're what make the output
    # re-match itself. Same reason "sdh" (not just "hearing.impaired") and
    # "forced" are listed.
    commentary_title_patterns: list[str] = Field(default_factory=lambda: ["commentary"])
    hearing_impaired_title_patterns: list[str] = Field(default_factory=lambda: ["sdh", "hearing.impaired"])
    cc_title_patterns: list[str] = Field(default_factory=lambda: ["closed.caption", "\\bcc\\b"])
    audio_description_title_patterns: list[str] = Field(
        default_factory=lambda: ["audio.description", "descriptive", "\\bad\\b"]
    )
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


def _build_title(language_name: str, attributes: list[str], style: str) -> str:
    if not attributes:
        return language_name
    joined = " ".join(attributes)
    return f"{language_name} - {joined}" if style != "space" else f"{language_name} {joined}"


# The labels this module emits. Each one must be matched by its own config
# pattern set (see NormalizerConfig) so a normalized title survives a second
# pass unchanged.
ATTR_COMMENTARY = "Commentary"
ATTR_FORCED = "Forced"
ATTR_SDH = "SDH"
ATTR_CC = "CC"
ATTR_AD = "AD"
ATTR_ORIGINAL = "Original"
ATTR_DUBBED = "Dubbed"


def _is_commentary(stream: MediaStream, config: NormalizerConfig) -> bool:
    return bool(stream.is_commentary or matches_any_pattern(stream.title, config.commentary_title_patterns))


def _is_forced(stream: MediaStream, config: NormalizerConfig) -> bool:
    """Disposition flag, or the title literally saying "forced", or — only
    when the user opted in — one of the looser equivalents.
    """
    if stream.is_forced or matches_any_pattern(stream.title, config.forced_title_patterns):
        return True
    return config.forced_equivalents_enabled and bool(
        matches_any_pattern(stream.title, config.forced_equivalent_patterns)
    )


def _audio_attributes(stream: MediaStream, config: NormalizerConfig) -> list[str]:
    attributes = []
    if _is_commentary(stream, config):
        attributes.append(ATTR_COMMENTARY)
    if stream.is_visual_impaired or matches_any_pattern(stream.title, config.audio_description_title_patterns):
        attributes.append(ATTR_AD)
    if matches_any_pattern(stream.title, config.original_title_patterns):
        attributes.append(ATTR_ORIGINAL)
    if matches_any_pattern(stream.title, config.dubbed_title_patterns):
        attributes.append(ATTR_DUBBED)
    return attributes


def _subtitle_attributes(stream: MediaStream, config: NormalizerConfig) -> list[str]:
    attributes = []
    if _is_forced(stream, config):
        attributes.append(ATTR_FORCED)
    # Commentary subtitles (a transcript of a commentary track) are common on
    # disc rips and were previously dropped entirely here — only the audio
    # side checked for them, leaving the sub renamed to a bare language name
    # and indistinguishable from the feature's own subtitle.
    if _is_commentary(stream, config):
        attributes.append(ATTR_COMMENTARY)
    if stream.is_hearing_impaired or matches_any_pattern(stream.title, config.hearing_impaired_title_patterns):
        attributes.append(ATTR_SDH)
    elif matches_any_pattern(stream.title, config.cc_title_patterns):
        # SDH takes priority when a track somehow matches both — they're
        # rendered as mutually exclusive labels, never combined.
        attributes.append(ATTR_CC)
    return attributes


_CHANNEL_LAYOUTS = {1: "Mono", 2: "Stereo", 3: "2.1", 4: "4.0", 6: "5.1", 7: "6.1", 8: "7.1"}

_CODEC_LABELS = {
    "subrip": "SRT",
    "srt": "SRT",
    "ass": "ASS",
    "ssa": "ASS",
    "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "VOBSUB",
    "mov_text": "TX3G",
    "truehd": "TrueHD",
    "opus": "Opus",
}


def _disambiguator(stream: MediaStream) -> str | None:
    """A short, stable label distinguishing two same-language tracks of the
    same type that would otherwise get byte-identical titles (e.g. a 5.1 and
    a stereo English dub, both -> "English", showing up as two identical
    entries in Jellyfin/Plex's track picker).

    Derived only from intrinsic stream properties, never from position, so
    it's stable across rescans. None when there's nothing to go on — better
    two identical names than an arbitrary "English 2".
    """
    if stream.channels:
        return _CHANNEL_LAYOUTS.get(stream.channels, f"{stream.channels}ch")
    if stream.codec_name and stream.codec_name != "unknown":
        return _CODEC_LABELS.get(stream.codec_name.lower(), stream.codec_name.upper())
    return None


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

    # Pass 1: work out each track's selector, language name and attributes,
    # but don't commit to a title yet — two same-language tracks of the same
    # type can produce identical titles, and resolving that needs to see the
    # whole file (pass 2 below).
    planned: list[tuple[MediaStream, str, str, list[str]]] = []

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
        planned.append((stream, track_selector, language_name, attributes))

    # Pass 2: where more than one track of the same type would land on the
    # same title, add a disambiguating suffix so a player's track picker
    # never shows two identical entries.
    #
    # Only applied when the suffixes actually separate the whole group — if
    # two tracks are genuinely indistinguishable (same language, codec and
    # channel count) then appending the same suffix to both adds noise
    # without disambiguating anything, and inventing an index instead would
    # be arbitrary. In that case they're left identical, honestly.
    groups: dict[tuple[str, str], list[MediaStream]] = {}
    for stream, _selector, name, attrs in planned:
        groups.setdefault((stream.codec_type, _build_title(name, attrs, config.naming_style)), []).append(stream)

    suffix_by_index: dict[int, str] = {}
    collapse_would_lose_detail: set[int] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        suffixes = [_disambiguator(s) for s in group]
        if suffixes.count(None) == 0 and len(set(suffixes)) == len(group):
            for stream, suffix in zip(group, suffixes):
                suffix_by_index[stream.index] = suffix
            continue

        # Nothing intrinsic separates these tracks, so renaming them all
        # would produce identical titles. When their *existing* titles are
        # distinct, that is not a cosmetic tie — it is information the file
        # already carries and the rename would destroy: three Chinese
        # subtitle tracks labelled 廣東話 / 中文（繁體） / 中文（简体） all
        # collapse to "Chinese", as do Español (España) and Español
        # (Latinoamérica), leaving a track picker that cannot tell them
        # apart at all. Leave those alone; an imperfect existing label beats
        # three identical ones.
        old_titles = [s.title for s in group]
        if all(t for t in old_titles) and len(set(old_titles)) == len(group):
            collapse_would_lose_detail.update(s.index for s in group)

    for stream, track_selector, language_name, attributes in planned:
        if stream.index in collapse_would_lose_detail:
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
                    reason=(
                        f"renaming to '{_build_title(language_name, attributes, config.naming_style)}' would make this "
                        "identical to another track that currently has its own distinct title, left untouched"
                    ),
                )
            )
            continue

        suffix = suffix_by_index.get(stream.index)
        if suffix is not None:
            attributes = [*attributes, suffix]
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
        dataclasses.replace(
            n,
            new_title=n.old_title or "",
            new_language=n.old_language,
            new_default=None,
            changed=False,
            reason="skipped by user override",
        )
        if n.index in skip_set
        else n
        for n in normalizations
    ]
