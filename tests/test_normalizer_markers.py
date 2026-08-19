"""Regression tests for the normalizer's attribute-detection layer.

Every case here is one that previously *destroyed* a meaningful marker —
rewriting e.g. "English Forced" to a bare "English" — or produced two
identically-named tracks. The common thread: the engine recognised a marker
in one narrow form and silently dropped everything it didn't recognise.

The idempotency tests at the bottom pin the rule that makes this safe to run
repeatedly (and on a schedule): every label the normalizer emits must be
matched by its own detection patterns, or the *next* pass strips it back off.
"""

from __future__ import annotations

import pytest

from app.normalizer import NormalizerConfig, normalize_streams
from tests.fixtures import make_stream


def title_of(streams, config=None):
    result = normalize_streams(streams, config or NormalizerConfig())
    return {n.index: n.new_title for n in result}


# --- Forced subtitles -------------------------------------------------------
# Lots of real releases set only the title and never the disposition flag;
# dropping the marker breaks a player's forced-subtitle auto-selection.


@pytest.mark.parametrize(
    "existing_title",
    ["English Forced", "Forced ENG", "Forced", "English (forced)", "English [Forced]"],
)
def test_forced_in_the_title_is_kept_without_a_disposition_flag(existing_title):
    streams = [make_stream(0, "subtitle", language="eng", title=existing_title)]
    assert title_of(streams)[0] == "English - Forced"


def test_forced_disposition_flag_still_works_on_its_own():
    streams = [make_stream(0, "subtitle", language="eng", is_forced=True)]
    assert title_of(streams)[0] == "English - Forced"


def test_forced_equivalents_remain_opt_in():
    """"Foreign"/"Signs & Songs" claim to *mean* forced — that's a guess, and
    stays behind the existing opt-in, unlike the literal word "forced".
    """
    streams = [make_stream(0, "subtitle", language="eng", title="Foreign Parts")]
    assert title_of(streams)[0] == "English"
    assert title_of(streams, NormalizerConfig(forced_equivalents_enabled=True))[0] == "English - Forced"


# --- Commentary subtitles ---------------------------------------------------


def test_commentary_subtitle_keeps_its_marker():
    """Only the audio side used to check for commentary, so a commentary
    subtitle was renamed to a bare language name — indistinguishable from
    the feature's own subtitle track.
    """
    streams = [make_stream(0, "subtitle", language="eng", title="Commentary by director Charles Shyer")]
    assert title_of(streams)[0] == "English - Commentary"


def test_commentary_disposition_flag_works_on_subtitles_too():
    streams = [make_stream(0, "subtitle", language="eng", is_commentary=True)]
    assert title_of(streams)[0] == "English - Commentary"


# --- Closed captions --------------------------------------------------------


@pytest.mark.parametrize("existing_title", ["English (CC)", "English CC", "English [CC]", "Closed Captions"])
def test_cc_is_detected_in_the_forms_that_actually_occur(existing_title):
    streams = [make_stream(0, "subtitle", language="eng", title=existing_title)]
    assert title_of(streams)[0] == "English - CC"


def test_sdh_still_wins_over_cc_when_a_track_claims_both():
    streams = [make_stream(0, "subtitle", language="eng", title="English [CC][SDH]")]
    assert title_of(streams)[0] == "English - SDH"


# --- Audio description ------------------------------------------------------


@pytest.mark.parametrize("existing_title", ["English AD", "Audio Description", "Descriptive Audio"])
def test_audio_description_keeps_its_marker(existing_title):
    streams = [make_stream(0, "audio", language="eng", title=existing_title)]
    assert title_of(streams)[0] == "English - AD"


def test_audio_description_from_the_visual_impaired_disposition():
    streams = [make_stream(0, "audio", language="eng", is_visual_impaired=True)]
    assert title_of(streams)[0] == "English - AD"


# --- Duplicate titles -------------------------------------------------------


def test_same_language_audio_tracks_are_disambiguated_by_channel_layout():
    streams = [
        make_stream(0, "audio", codec_name="ac3", language="eng", channels=6),
        make_stream(1, "audio", codec_name="aac", language="eng", channels=2),
    ]
    assert title_of(streams) == {0: "English - 5.1", 1: "English - Stereo"}


def test_same_language_subtitles_are_disambiguated_by_codec():
    streams = [
        make_stream(0, "subtitle", codec_name="subrip", language="eng"),
        make_stream(1, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),
    ]
    assert title_of(streams) == {0: "English - SRT", 1: "English - PGS"}


def test_disambiguation_only_fires_on_an_actual_collision():
    streams = [
        make_stream(0, "audio", language="eng", channels=6),
        make_stream(1, "audio", language="jpn", channels=2),
    ]
    assert title_of(streams) == {0: "English", 1: "Japanese"}


def test_audio_and_subtitle_of_the_same_language_do_not_collide():
    """Separate pickers in a player — an "English" audio and an "English"
    subtitle are not ambiguous with each other.
    """
    streams = [
        make_stream(0, "audio", language="eng", channels=6),
        make_stream(1, "subtitle", codec_name="subrip", language="eng"),
    ]
    assert title_of(streams) == {0: "English", 1: "English"}


def test_indistinguishable_tracks_are_left_alone_rather_than_numbered():
    """Same language, codec and channel count — there's nothing real to tell
    them apart, and an arbitrary "English 2" would be worse than honest.
    """
    streams = [
        make_stream(0, "subtitle", codec_name="subrip", language="eng"),
        make_stream(1, "subtitle", codec_name="subrip", language="eng"),
    ]
    assert title_of(streams) == {0: "English", 1: "English"}


def test_disambiguation_composes_with_attributes():
    streams = [
        make_stream(0, "audio", language="eng", channels=6, is_commentary=True),
        make_stream(1, "audio", language="eng", channels=2, is_commentary=True),
    ]
    assert title_of(streams) == {0: "English - Commentary 5.1", 1: "English - Commentary Stereo"}


# --- Idempotency ------------------------------------------------------------


def _renormalize(streams, config):
    """Normalize, pretend the result was written to the file, normalize again."""
    first = normalize_streams(streams, config)
    written = [
        make_stream(
            s.index,
            s.codec_type,
            codec_name=s.codec_name,
            language=s.language,
            title=n.new_title,
            channels=s.channels,
            is_default=n.new_default if n.new_default is not None else s.is_default,
            is_forced=s.is_forced,
            is_commentary=s.is_commentary,
            is_hearing_impaired=s.is_hearing_impaired,
            is_visual_impaired=s.is_visual_impaired,
        )
        for s, n in zip(streams, first)
    ]
    return first, normalize_streams(written, config)


@pytest.mark.parametrize(
    "streams",
    [
        pytest.param([make_stream(0, "subtitle", language="eng", title="English Forced")], id="forced-title"),
        pytest.param([make_stream(0, "subtitle", language="eng", title="English (CC)")], id="cc"),
        pytest.param([make_stream(0, "subtitle", language="eng", title="Closed Captions")], id="cc-longform"),
        pytest.param([make_stream(0, "subtitle", language="eng", title="Commentary by X")], id="commentary-sub"),
        pytest.param([make_stream(0, "audio", language="eng", title="English AD")], id="audio-description"),
        pytest.param([make_stream(0, "subtitle", language="eng", is_hearing_impaired=True)], id="sdh-flag"),
        pytest.param([make_stream(0, "audio", language="ger", title="Dubbed")], id="dubbed"),
        pytest.param([make_stream(0, "audio", language="eng", title="Original English")], id="original"),
        pytest.param(
            [
                make_stream(0, "audio", language="eng", channels=6),
                make_stream(1, "audio", language="eng", channels=2),
            ],
            id="disambiguated-duplicates",
        ),
    ],
)
def test_normalizing_an_already_normalized_file_proposes_nothing(streams):
    """The property that makes this safe to run on a schedule: a second pass
    over its own output must be a no-op. "Closed Captions" -> "English - CC"
    -> "English" used to be the failure here — the emitted label no longer
    matched the pattern that produced it.
    """
    config = NormalizerConfig()
    first, second = _renormalize(streams, config)
    assert any(n.changed for n in first), "fixture should need normalizing on the first pass"
    assert not any(n.changed for n in second), [n.reason for n in second]
