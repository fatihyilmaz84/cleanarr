"""Pure decision logic: given a file's streams and a rule config, decide what
to keep and what to drop. No I/O, no side effects — fully unit-testable and
directly reusable by the review UI to render a preview.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.analyzer import MediaProbe, MediaStream
from app.languages import iso_codes_for_language_name
from app.text_patterns import matches_any_pattern


class RuleConfig(BaseModel):
    """User-editable rule set. Empty lists mean "keep nothing extra" for that
    track type — the app ships with `audio_keep_languages=[]` /
    `subtitle_keep_languages=[]` by design, so it's inert until configured.
    """

    audio_keep_languages: list[str] = Field(default_factory=list)
    subtitle_keep_languages: list[str] = Field(default_factory=list)

    keep_untagged_language: bool = True
    always_keep_forced_subtitles: bool = True
    # Narrows the rule above to languages that actually survive in the file.
    #
    # A forced subtitle exists to translate the bits of dialogue that aren't
    # in the language you're listening to. That makes it useful to someone
    # who reads it — but a forced Italian track on a file whose Italian
    # audio and full Italian subtitles are both being removed serves nobody,
    # and leaves the odd result that a language is *almost* gone.
    #
    # Off by default: it drops tracks the setting above promises to keep, so
    # it has to be asked for. See _drop_orphaned_forced_subtitles for exactly
    # which four ways a forced track earns its place.
    drop_orphaned_forced_subtitles: bool = False
    drop_commentary_tracks: bool = False
    drop_hearing_impaired_tracks: bool = False

    # When a Sonarr/Radarr connection resolves a file's original language
    # (e.g. "Korean" for a Korean movie), keep tracks in that language even
    # if it isn't in the keep-lists above — so a global "eng only" rule
    # doesn't strip a foreign film's own native-language audio/subs.
    always_keep_original_language: bool = True

    # Regex patterns (case-insensitive) matched against a stream's title tag.
    # A match forces a drop, e.g. ["commentary", "sdh"].
    drop_title_patterns: list[str] = Field(default_factory=list)

    # Different muxers tag commentary/hearing-impaired tracks inconsistently
    # — some set ffprobe's disposition flags correctly, others only put it
    # in the free-text title (e.g. "Director's Commentary", "English (SDH)").
    # These patterns are a title-text fallback for is_commentary/
    # is_hearing_impaired classification, checked in addition to the
    # disposition flags — unlike the keep-lists above, "commentary"/"SDH"
    # are unambiguous, universal signals, so these ship with sensible
    # defaults rather than empty; still fully editable in Rules.
    commentary_title_patterns: list[str] = Field(default_factory=lambda: ["commentary"])
    hearing_impaired_title_patterns: list[str] = Field(default_factory=lambda: ["sdh", "hearing.impaired"])

    def normalized_audio_languages(self) -> set[str]:
        return {lang.strip().lower() for lang in self.audio_keep_languages if lang.strip()}

    def normalized_subtitle_languages(self) -> set[str]:
        return {lang.strip().lower() for lang in self.subtitle_keep_languages if lang.strip()}


@dataclass(frozen=True)
class StreamDecision:
    stream: MediaStream
    keep: bool
    reason: str


def _matches_original_language(stream: MediaStream, config: RuleConfig, original_language_codes: frozenset[str]) -> bool:
    return (
        config.always_keep_original_language
        and stream.language is not None
        and stream.language.lower() in original_language_codes
    )


def _commentary_reason(stream: MediaStream, config: RuleConfig) -> str | None:
    """Why this stream counts as commentary, or None if it doesn't — checks
    ffprobe's disposition flag first, then falls back to title-text
    patterns for containers that don't set the flag correctly.
    """
    if stream.is_commentary:
        return "commentary track (disposition flag)"
    pattern = matches_any_pattern(stream.title, config.commentary_title_patterns)
    if pattern:
        return f"title matches commentary pattern '{pattern}'"
    return None


def _hearing_impaired_reason(stream: MediaStream, config: RuleConfig) -> str | None:
    """Same idea as `_commentary_reason`, for hearing-impaired (SDH) subs."""
    if stream.is_hearing_impaired:
        return "hearing-impaired subtitle (disposition flag)"
    pattern = matches_any_pattern(stream.title, config.hearing_impaired_title_patterns)
    if pattern:
        return f"title matches hearing-impaired pattern '{pattern}'"
    return None


def _decide_audio(stream: MediaStream, config: RuleConfig, original_language_codes: frozenset[str]) -> StreamDecision:
    drop_pattern = matches_any_pattern(stream.title, config.drop_title_patterns)
    if drop_pattern:
        return StreamDecision(stream, False, f"title matches drop pattern '{drop_pattern}'")

    commentary_reason = _commentary_reason(stream, config)
    if commentary_reason and config.drop_commentary_tracks:
        return StreamDecision(stream, False, f"{commentary_reason}, drop_commentary_tracks enabled")

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
        return StreamDecision(stream, True, FORCED_KEPT_REASON)

    drop_pattern = matches_any_pattern(stream.title, config.drop_title_patterns)
    if drop_pattern:
        return StreamDecision(stream, False, f"title matches drop pattern '{drop_pattern}'")

    hi_reason = _hearing_impaired_reason(stream, config)
    if hi_reason and config.drop_hearing_impaired_tracks:
        return StreamDecision(stream, False, f"{hi_reason}, drop_hearing_impaired_tracks enabled")

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


FORCED_KEPT_REASON = "forced subtitle, always kept"


def _drop_orphaned_forced_subtitles(
    decisions: list[StreamDecision], config: RuleConfig, original_language_codes: frozenset[str]
) -> list[StreamDecision]:
    """Withdraw the forced-subtitle exemption from languages that aren't
    staying in the file anyway.

    A forced track keeps its exemption if any of these hold, and each is
    someone it genuinely serves:

      - its language is in the subtitle keep-list — they read it. This is
        the one that matters most: a Korean film keeps its Korean audio as
        the original language, and its *English* forced subtitles are
        exactly what an English speaker needs for the on-screen signs.
        Judging by "does this language's audio survive" would have thrown
        those away.
      - a kept audio track is in that language — the forced subs pair with
        a dub that's staying.
      - it's the media's own original language, mirroring
        always_keep_original_language.
      - it has no language tag at all, which keep_untagged_language governs.

    Does nothing when no subtitle filter is configured, since then every
    subtitle is being kept and there is no language to be orphaned from.

    Runs after the keep-at-least-one-audio net below, so it sees the audio
    that will really survive rather than what the language filter alone
    proposed.
    """
    if not config.drop_orphaned_forced_subtitles:
        return decisions
    allowed_subtitles = config.normalized_subtitle_languages()
    if not allowed_subtitles:
        return decisions

    kept_audio_languages = {
        d.stream.language.lower()
        for d in decisions
        if d.stream.codec_type == "audio" and d.keep and d.stream.language
    }

    result = []
    for d in decisions:
        language = (d.stream.language or "").lower()
        orphaned = (
            d.keep
            and d.stream.codec_type == "subtitle"
            and d.stream.is_forced
            and language
            and language not in allowed_subtitles
            and language not in kept_audio_languages
            and language not in original_language_codes
        )
        if orphaned:
            result.append(
                StreamDecision(
                    d.stream,
                    False,
                    f"forced subtitle in '{d.stream.language}', but nothing in that language is being "
                    "kept — no audio, no subtitles, and it isn't the original language",
                )
            )
        else:
            result.append(d)
    return result


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

    decisions = _apply_keep_at_least_one_audio(decisions)
    return _drop_orphaned_forced_subtitles(decisions, config, original_language_codes)


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
