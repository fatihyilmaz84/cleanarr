"""Pure decision logic: given a file's streams and a rule config, decide what
to keep and what to drop. No I/O, no side effects — fully unit-testable and
directly reusable by the review UI to render a preview.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.analyzer import MediaProbe, MediaStream
from app.languages import iso_codes_for_language_name


class RuleConfig(BaseModel):
    """User-editable rule set. Empty lists mean "keep nothing extra" for that
    track type — the app ships with `audio_keep_languages=[]` /
    `subtitle_keep_languages=[]` by design, so it's inert until configured.
    """

    audio_keep_languages: list[str] = Field(default_factory=list)
    subtitle_keep_languages: list[str] = Field(default_factory=list)

    keep_untagged_language: bool = True
    always_keep_forced_subtitles: bool = True
    drop_commentary_tracks: bool = False

    # When a Sonarr/Radarr connection resolves a file's original language
    # (e.g. "Korean" for a Korean movie), keep tracks in that language even
    # if it isn't in the keep-lists above — so a global "eng only" rule
    # doesn't strip a foreign film's own native-language audio/subs.
    always_keep_original_language: bool = True

    # Regex patterns (case-insensitive) matched against a stream's title tag.
    # A match forces a drop, e.g. ["commentary", "sdh"].
    drop_title_patterns: list[str] = Field(default_factory=list)

    def normalized_audio_languages(self) -> set[str]:
        return {lang.strip().lower() for lang in self.audio_keep_languages if lang.strip()}

    def normalized_subtitle_languages(self) -> set[str]:
        return {lang.strip().lower() for lang in self.subtitle_keep_languages if lang.strip()}


@dataclass(frozen=True)
class StreamDecision:
    stream: MediaStream
    keep: bool
    reason: str


def _matches_drop_pattern(title: str | None, patterns: list[str]) -> str | None:
    if not title:
        return None
    for pattern in patterns:
        try:
            if re.search(pattern, title, re.IGNORECASE):
                return pattern
        except re.error:
            continue
    return None


def _matches_original_language(stream: MediaStream, config: RuleConfig, original_language_codes: frozenset[str]) -> bool:
    return (
        config.always_keep_original_language
        and stream.language is not None
        and stream.language.lower() in original_language_codes
    )


def _decide_audio(stream: MediaStream, config: RuleConfig, original_language_codes: frozenset[str]) -> StreamDecision:
    drop_pattern = _matches_drop_pattern(stream.title, config.drop_title_patterns)
    if drop_pattern:
        return StreamDecision(stream, False, f"title matches drop pattern '{drop_pattern}'")

    if stream.is_commentary and config.drop_commentary_tracks:
        return StreamDecision(stream, False, "commentary track, drop_commentary_tracks enabled")

    allowed_languages = config.normalized_audio_languages()
    if not allowed_languages:
        return StreamDecision(stream, True, "no audio language filter configured, kept")

    if stream.language is None:
        if config.keep_untagged_language:
            return StreamDecision(stream, True, "no language tag, kept (untagged-keep enabled)")
        return StreamDecision(stream, False, "no language tag, dropped (untagged-keep disabled)")

    if stream.language.lower() in allowed_languages:
        return StreamDecision(stream, True, f"language '{stream.language}' in keep-list")

    if _matches_original_language(stream, config, original_language_codes):
        return StreamDecision(stream, True, f"language '{stream.language}' matches media's original language, kept")

    return StreamDecision(stream, False, f"language '{stream.language}' not in keep-list")


def _decide_subtitle(stream: MediaStream, config: RuleConfig, original_language_codes: frozenset[str]) -> StreamDecision:
    if stream.is_forced and config.always_keep_forced_subtitles:
        return StreamDecision(stream, True, "forced subtitle, always kept")

    drop_pattern = _matches_drop_pattern(stream.title, config.drop_title_patterns)
    if drop_pattern:
        return StreamDecision(stream, False, f"title matches drop pattern '{drop_pattern}'")

    allowed_languages = config.normalized_subtitle_languages()
    if not allowed_languages:
        return StreamDecision(stream, True, "no subtitle language filter configured, kept")

    if stream.language is None:
        if config.keep_untagged_language:
            return StreamDecision(stream, True, "no language tag, kept (untagged-keep enabled)")
        return StreamDecision(stream, False, "no language tag, dropped (untagged-keep disabled)")

    if stream.language.lower() in allowed_languages:
        return StreamDecision(stream, True, f"language '{stream.language}' in keep-list")

    if _matches_original_language(stream, config, original_language_codes):
        return StreamDecision(stream, True, f"language '{stream.language}' matches media's original language, kept")

    return StreamDecision(stream, False, f"language '{stream.language}' not in keep-list")


def _apply_keep_at_least_one_audio(decisions: list[StreamDecision]) -> list[StreamDecision]:
    """Never let the rule engine strip every audio track — that would produce
    an unplayable file. If everything would be dropped, force-keep the
    original default track (or the first one, failing that).
    """
    audio = [d for d in decisions if d.stream.codec_type == "audio"]
    if not audio or any(d.keep for d in audio):
        return decisions

    fallback = next((d for d in audio if d.stream.is_default), audio[0])
    fixed = StreamDecision(
        fallback.stream,
        True,
        f"safety override: would have dropped every audio track ({fallback.reason}); "
        "keeping this one so the file stays playable",
    )
    return [fixed if d.stream.index == fallback.stream.index else d for d in decisions]


def decide(probe: MediaProbe, config: RuleConfig, original_language: str | None = None) -> list[StreamDecision]:
    """Evaluate every stream in `probe` against `config`. Video streams are
    always kept — this app only ever touches audio/subtitle tracks.

    `original_language` is the media's own language as reported by
    Sonarr/Radarr (e.g. "Korean"), if known — used only when
    `config.always_keep_original_language` is set, to protect that
    language's tracks regardless of the configured keep-lists.
    """
    original_language_codes = iso_codes_for_language_name(original_language)
    decisions: list[StreamDecision] = []

    for stream in probe.streams:
        if stream.codec_type == "video":
            decisions.append(StreamDecision(stream, True, "video track, always kept"))
        elif stream.codec_type == "audio":
            decisions.append(_decide_audio(stream, config, original_language_codes))
        elif stream.codec_type == "subtitle":
            decisions.append(_decide_subtitle(stream, config, original_language_codes))
        else:
            decisions.append(StreamDecision(stream, True, f"unrecognized stream type '{stream.codec_type}', kept"))

    return _apply_keep_at_least_one_audio(decisions)


def apply_overrides(decisions: list[StreamDecision], overrides: list[int] | None) -> list[StreamDecision]:
    """Force-keep any stream index the user explicitly chose to keep at
    approval time (e.g. "drop the audio but not this subtitle"), overriding
    what the rule engine proposed. Only ever pushes a decision from drop to
    keep — never the reverse — so this can't undermine the "keep at least
    one audio track" safety net in `decide`.
    """
    if not overrides:
        return decisions
    override_set = set(overrides)
    return [
        StreamDecision(d.stream, True, "kept: manually overridden during approval")
        if d.stream.index in override_set and not d.keep
        else d
        for d in decisions
    ]
