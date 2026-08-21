from app.rules import RuleConfig, apply_overrides, decide
from tests.fixtures import make_probe, make_stream, typical_movie_streams


def decisions_by_index(probe, config, original_language=None):
    return {d.stream.index: d for d in decide(probe, config, original_language)}


def test_empty_rule_config_keeps_everything():
    probe = make_probe(typical_movie_streams())
    config = RuleConfig()  # default/inert config, as shipped

    decisions = decide(probe, config)

    assert all(d.keep for d in decisions), [d.reason for d in decisions if not d.keep]


def test_language_filtering_drops_unwanted_audio_and_subs():
    probe = make_probe(typical_movie_streams())
    config = RuleConfig(
        audio_keep_languages=["eng", "tur"],
        subtitle_keep_languages=["eng"],
        drop_commentary_tracks=True,
    )

    by_index = decisions_by_index(probe, config)

    assert by_index[1].keep is True  # eng audio
    assert by_index[2].keep is True  # tur audio
    assert by_index[3].keep is False  # eng commentary -> dropped (commentary rule enabled)
    assert by_index[4].keep is True  # untagged audio -> kept by default
    assert by_index[5].keep is True  # eng subtitle
    assert by_index[6].keep is True  # forced eng subtitle, always kept
    assert by_index[7].keep is False  # tur subtitle not in subtitle keep-list
    assert by_index[8].keep is False  # jpn subtitle not in keep-list


def test_video_streams_always_kept():
    probe = make_probe(typical_movie_streams())
    config = RuleConfig(audio_keep_languages=["fre"], subtitle_keep_languages=[])

    by_index = decisions_by_index(probe, config)

    assert by_index[0].keep is True
    assert "video" in by_index[0].reason


def test_commentary_kept_by_default():
    probe = make_probe(typical_movie_streams())
    config = RuleConfig(audio_keep_languages=["eng"])  # drop_commentary_tracks defaults False

    by_index = decisions_by_index(probe, config)

    assert by_index[3].keep is True  # commentary kept unless explicitly opted to drop


def test_commentary_dropped_when_configured():
    probe = make_probe(typical_movie_streams())
    config = RuleConfig(audio_keep_languages=["eng"], drop_commentary_tracks=True)

    by_index = decisions_by_index(probe, config)

    assert by_index[3].keep is False


def test_untagged_language_dropped_when_disabled():
    probe = make_probe(typical_movie_streams())
    config = RuleConfig(audio_keep_languages=["eng"], keep_untagged_language=False)

    by_index = decisions_by_index(probe, config)

    assert by_index[4].keep is False


def test_forced_subtitle_survives_drop_title_pattern_precedence():
    # forced-subtitle-always-keep is evaluated before title-pattern drop
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "subtitle", language="eng", title="Forced", is_forced=True),
    ]
    probe = make_probe(streams)
    config = RuleConfig(
        audio_keep_languages=["eng"],
        subtitle_keep_languages=[],
        drop_title_patterns=["forced"],
    )

    by_index = decisions_by_index(probe, config)
    assert by_index[2].keep is True


def test_title_pattern_drops_matching_track_even_if_language_kept():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "audio", language="eng", title="Audio Description"),
    ]
    probe = make_probe(streams)
    config = RuleConfig(audio_keep_languages=["eng"], drop_title_patterns=["audio description"])

    by_index = decisions_by_index(probe, config)
    assert by_index[1].keep is True
    assert by_index[2].keep is False


def test_never_drops_every_audio_track():
    # Rules would drop every audio track, but the safety override must keep one.
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="jpn"),
        make_stream(2, "audio", language="jpn", is_default=True),
    ]
    probe = make_probe(streams)
    config = RuleConfig(audio_keep_languages=["eng"], keep_untagged_language=False)

    decisions = decide(probe, config)
    audio_decisions = [d for d in decisions if d.stream.codec_type == "audio"]

    assert any(d.keep for d in audio_decisions)
    kept = next(d for d in audio_decisions if d.keep)
    assert kept.stream.is_default is True  # prefers the original default track
    assert "safety override" in kept.reason


def test_subtitles_can_all_be_dropped_no_safety_override():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "subtitle", language="jpn"),
    ]
    probe = make_probe(streams)
    # subtitle filter must be active (non-empty) for anything to be dropped
    config = RuleConfig(audio_keep_languages=["eng"], subtitle_keep_languages=["eng"])

    decisions = decide(probe, config)
    sub_decisions = [d for d in decisions if d.stream.codec_type == "subtitle"]

    assert all(not d.keep for d in sub_decisions)


def test_original_language_track_kept_even_when_not_in_keep_list():
    # A Korean movie with an "eng only" global rule shouldn't lose its own
    # Korean audio/subs — Sonarr/Radarr told us this file's original
    # language is Korean.
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng"),
        make_stream(2, "audio", language="kor", is_default=True),
        make_stream(3, "subtitle", language="kor"),
        make_stream(4, "subtitle", language="jpn"),
    ]
    probe = make_probe(streams)
    config = RuleConfig(audio_keep_languages=["eng"], subtitle_keep_languages=["eng"])

    by_index = decisions_by_index(probe, config, original_language="Korean")

    assert by_index[1].keep is True  # in keep-list
    assert by_index[2].keep is True  # not in keep-list, but matches original language
    assert "original language" in by_index[2].reason
    assert by_index[3].keep is True  # subtitle also protected
    assert by_index[4].keep is False  # unrelated language, still dropped


def test_original_language_override_can_be_disabled():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "audio", language="kor"),
    ]
    probe = make_probe(streams)
    config = RuleConfig(audio_keep_languages=["eng"], always_keep_original_language=False)

    by_index = decisions_by_index(probe, config, original_language="Korean")

    assert by_index[2].keep is False


def test_unmapped_original_language_name_is_harmless():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "audio", language="kor"),
    ]
    probe = make_probe(streams)
    config = RuleConfig(audio_keep_languages=["eng"])

    by_index = decisions_by_index(probe, config, original_language="Not A Real Language")

    assert by_index[2].keep is False


def test_apply_overrides_force_keeps_specified_dropped_stream():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "audio", language="jpn"),
        make_stream(3, "subtitle", language="tur"),
    ]
    probe = make_probe(streams)
    config = RuleConfig(audio_keep_languages=["eng"], subtitle_keep_languages=["eng"])
    decisions = decide(probe, config)
    assert {d.stream.index for d in decisions if not d.keep} == {2, 3}

    # Only override the subtitle — the jpn audio should still get dropped.
    overridden = apply_overrides(decisions, [3])
    by_index = {d.stream.index: d for d in overridden}

    assert by_index[2].keep is False  # untouched, still dropped
    assert by_index[3].keep is True  # force-kept
    assert "overridden" in by_index[3].reason


def test_apply_overrides_is_noop_for_already_kept_stream():
    streams = [make_stream(0, "video"), make_stream(1, "audio", language="eng", is_default=True)]
    probe = make_probe(streams)
    decisions = decide(probe, RuleConfig())

    overridden = apply_overrides(decisions, [1])  # index 1 is already kept

    assert overridden == decisions  # untouched — override only ever pushes drop -> keep


def test_apply_overrides_handles_none_and_empty():
    streams = [make_stream(0, "video"), make_stream(1, "audio", language="eng", is_default=True)]
    probe = make_probe(streams)
    decisions = decide(probe, RuleConfig())

    assert apply_overrides(decisions, None) == decisions
    assert apply_overrides(decisions, []) == decisions


def test_rule_config_ships_with_sensible_commentary_and_hi_pattern_defaults():
    config = RuleConfig()
    assert config.commentary_title_patterns == ["commentary"]
    assert config.hearing_impaired_title_patterns == ["sdh", "hearing.impaired"]
    # detection is harmless by default — the drop toggles stay off
    assert config.drop_commentary_tracks is False
    assert config.drop_hearing_impaired_tracks is False


def test_commentary_detected_via_title_when_disposition_flag_missing():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        # is_commentary=False (container didn't set the disposition flag) but the title says it is
        make_stream(2, "audio", language="eng", title="Director's Commentary", is_commentary=False),
    ]
    probe = make_probe(streams)
    config = RuleConfig(drop_commentary_tracks=True)

    by_index = decisions_by_index(probe, config)

    assert by_index[2].keep is False
    assert "title matches commentary pattern" in by_index[2].reason


def test_commentary_title_detection_is_inert_without_the_drop_toggle():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "audio", language="eng", title="Director's Commentary", is_commentary=False),
    ]
    probe = make_probe(streams)
    config = RuleConfig(drop_commentary_tracks=False)  # default

    by_index = decisions_by_index(probe, config)

    assert by_index[2].keep is True


def test_hearing_impaired_detected_via_title_when_disposition_flag_missing():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "subtitle", language="eng", title="English (SDH)", is_hearing_impaired=False),
        make_stream(3, "subtitle", language="eng", title="English"),
    ]
    probe = make_probe(streams)
    config = RuleConfig(drop_hearing_impaired_tracks=True)

    by_index = decisions_by_index(probe, config)

    assert by_index[2].keep is False
    assert "title matches hearing-impaired pattern" in by_index[2].reason
    assert by_index[3].keep is True  # plain "English" subtitle unaffected


def test_forced_subtitle_beats_hearing_impaired_drop():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "subtitle", language="eng", title="English (SDH)", is_forced=True),
    ]
    probe = make_probe(streams)
    config = RuleConfig(drop_hearing_impaired_tracks=True, always_keep_forced_subtitles=True)

    by_index = decisions_by_index(probe, config)

    assert by_index[2].keep is True


def test_user_configured_commentary_pattern_is_respected():
    # user adds their own pattern instead of relying on the "commentary" default
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "audio", language="eng", title="Cast & Crew Chat", is_commentary=False),
    ]
    probe = make_probe(streams)
    config = RuleConfig(drop_commentary_tracks=True, commentary_title_patterns=["cast.*crew"])

    by_index = decisions_by_index(probe, config)

    assert by_index[2].keep is False


def test_disposition_flag_still_works_without_any_title_match():
    streams = [
        make_stream(0, "video"),
        make_stream(1, "audio", language="eng", is_default=True),
        make_stream(2, "audio", language="eng", title=None, is_commentary=True),
    ]
    probe = make_probe(streams)
    config = RuleConfig(drop_commentary_tracks=True, commentary_title_patterns=[])  # no patterns at all

    by_index = decisions_by_index(probe, config)

    assert by_index[2].keep is False
    assert "disposition flag" in by_index[2].reason


def test_a_forced_subtitle_is_kept_even_when_its_language_is_not():
    """`always_keep_forced_subtitles` is checked before the language
    keep-list, so a forced subtitle survives a language that is otherwise
    being removed entirely. This is why a file can drop its Hindi audio and
    keep a Hindi subtitle: the kept one is the forced track.
    """
    streams = [
        make_stream(0, "video", language=None),
        make_stream(1, "audio", language="eng"),
        make_stream(2, "audio", language="hin", title="हिन्दी [Dolby Digital Plus 5.1]"),
        make_stream(3, "subtitle", language="hin", title="हिन्दी [Forced]", is_forced=True),
        make_stream(4, "subtitle", language="hin", title="हिन्दी"),
    ]
    config = RuleConfig(audio_keep_languages=["eng"], subtitle_keep_languages=["eng"])

    decisions = {d.stream.index: d for d in decide(make_probe(streams), config)}

    assert decisions[2].keep is False  # the hin audio goes
    assert decisions[3].keep is True   # the forced hin subtitle stays
    assert decisions[3].reason == "forced subtitle, always kept"
    assert decisions[4].keep is False  # the non-forced hin subtitle goes


def test_turning_off_always_keep_forced_lets_the_language_filter_win():
    streams = [
        make_stream(0, "video", language=None),
        make_stream(1, "audio", language="eng"),
        make_stream(2, "subtitle", language="hin", title="हिन्दी [Forced]", is_forced=True),
    ]
    config = RuleConfig(
        audio_keep_languages=["eng"], subtitle_keep_languages=["eng"], always_keep_forced_subtitles=False
    )

    decisions = {d.stream.index: d for d in decide(make_probe(streams), config)}

    assert decisions[2].keep is False
