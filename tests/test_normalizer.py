from app.normalizer import NormalizerConfig, apply_overrides, normalize_streams
from tests.fixtures import make_stream


def by_index(streams, config):
    return {n.index: n for n in normalize_streams(streams, config)}


def test_video_streams_are_never_touched():
    streams = [make_stream(0, "video", language=None), make_stream(1, "audio", language="eng")]
    result = by_index(streams, NormalizerConfig())
    assert 0 not in result


def test_plain_language_title_with_no_attributes():
    streams = [make_stream(0, "audio", language="eng")]
    n = by_index(streams, NormalizerConfig())[0]
    assert n.new_title == "English"
    assert n.changed is True  # title was None, now "English"


def test_already_normalized_track_is_not_flagged_changed():
    streams = [make_stream(0, "audio", language="eng", title="English")]
    n = by_index(streams, NormalizerConfig())[0]
    assert n.new_title == "English"
    assert n.changed is False


def test_dash_vs_space_naming_style():
    streams = [make_stream(0, "audio", language="eng", is_commentary=True)]
    dash = by_index(streams, NormalizerConfig(naming_style="dash"))[0]
    space = by_index(streams, NormalizerConfig(naming_style="space"))[0]
    assert dash.new_title == "English - Commentary"
    assert space.new_title == "English Commentary"


def test_commentary_detected_via_disposition_or_title_pattern():
    disposition_flagged = make_stream(0, "audio", language="eng", is_commentary=True)
    title_flagged = make_stream(1, "audio", language="eng", title="Director's Commentary")
    result = by_index([disposition_flagged, title_flagged], NormalizerConfig())
    assert result[0].new_title == "English - Commentary"
    assert result[1].new_title == "English - Commentary"


def test_original_and_dubbed_detected_via_title_pattern():
    streams = [
        make_stream(0, "audio", language="jpn", title="Original"),
        make_stream(1, "audio", language="eng", title="Dubbed"),
    ]
    result = by_index(streams, NormalizerConfig())
    assert result[0].new_title == "Japanese - Original"
    assert result[1].new_title == "English - Dubbed"


def test_forced_subtitle_via_disposition_flag():
    streams = [make_stream(0, "subtitle", language="eng", is_forced=True)]
    n = by_index(streams, NormalizerConfig())[0]
    assert n.new_title == "English - Forced"


def test_forced_equivalents_off_by_default():
    streams = [make_stream(0, "subtitle", language="eng", title="Signs & Songs")]
    n = by_index(streams, NormalizerConfig())[0]
    assert n.new_title != "English - Forced"  # not recognized without opting in


def test_forced_equivalents_when_enabled():
    streams = [make_stream(0, "subtitle", language="eng", title="Signs & Songs")]
    config = NormalizerConfig(forced_equivalents_enabled=True)
    n = by_index(streams, config)[0]
    assert n.new_title == "English - Forced"


def test_sdh_detected_via_disposition_or_title_pattern():
    disposition_flagged = make_stream(0, "subtitle", language="eng", is_hearing_impaired=True)
    title_flagged = make_stream(1, "subtitle", language="eng", title="English (SDH)")
    result = by_index([disposition_flagged, title_flagged], NormalizerConfig())
    assert result[0].new_title == "English - SDH"
    assert result[1].new_title == "English - SDH"


def test_cc_detected_but_sdh_takes_priority_if_both_match():
    cc_only = make_stream(0, "subtitle", language="eng", title="Closed Caption")
    n = by_index([cc_only], NormalizerConfig())[0]
    assert n.new_title == "English - CC"

    both = make_stream(0, "subtitle", language="eng", is_hearing_impaired=True, title="Closed Caption")
    n2 = by_index([both], NormalizerConfig())[0]
    assert n2.new_title == "English - SDH"  # not "SDH CC" — mutually exclusive


def test_forced_and_sdh_combine():
    streams = [make_stream(0, "subtitle", language="eng", is_forced=True, is_hearing_impaired=True)]
    n = by_index(streams, NormalizerConfig())[0]
    assert n.new_title == "English - Forced SDH"


def test_unrecognized_language_code_left_untouched():
    streams = [make_stream(0, "audio", language="xx-not-real", title="Weird")]
    n = by_index(streams, NormalizerConfig())[0]
    assert n.changed is False
    assert "not recognized" in n.reason


def test_untagged_language_left_untouched():
    streams = [make_stream(0, "audio", language=None, title="Something")]
    n = by_index(streams, NormalizerConfig())[0]
    assert n.changed is False
    assert "no language tag" in n.reason


def test_track_selector_is_relative_to_type_not_global_index():
    # index 0 = video (skipped), index 1 = audio (a1), index 5 = subtitle (s1)
    streams = [
        make_stream(0, "video", language=None),
        make_stream(1, "audio", language="eng"),
        make_stream(2, "audio", language="jpn"),
        make_stream(5, "subtitle", language="eng"),
    ]
    result = by_index(streams, NormalizerConfig())
    assert result[1].track_selector == "a1"
    assert result[2].track_selector == "a2"
    assert result[5].track_selector == "s1"


def test_auto_default_audio_picks_preferred_language_and_clears_others():
    streams = [
        make_stream(0, "audio", language="jpn", is_default=True),
        make_stream(1, "audio", language="eng"),
    ]
    config = NormalizerConfig(auto_default_audio=True, preferred_audio_language="English")
    result = by_index(streams, config)
    assert result[0].new_default is False  # was default, no longer preferred -> explicitly cleared
    assert result[1].new_default is True
    assert result[0].changed is True
    assert result[1].changed is True


def test_auto_default_prefers_already_default_track_among_candidates():
    # titles pre-set to their already-normalized form so `changed` isolates
    # the default-flag dimension being tested here, not also the title one.
    streams = [
        make_stream(0, "audio", language="eng", title="English"),
        make_stream(1, "audio", language="eng", title="English", is_default=True),
    ]
    config = NormalizerConfig(auto_default_audio=True, preferred_audio_language="English")
    result = by_index(streams, config)
    assert result[1].new_default is True
    assert result[0].new_default is False
    # nothing actually changes (track 1 was already default, track 0 was already not)
    assert result[0].changed is False
    assert result[1].changed is False


def test_auto_default_disabled_leaves_default_flags_alone():
    streams = [make_stream(0, "audio", language="jpn", is_default=True), make_stream(1, "audio", language="eng")]
    result = by_index(streams, NormalizerConfig())  # auto_default_audio defaults False
    assert result[0].new_default is None
    assert result[1].new_default is None


def test_auto_default_with_no_matching_language_touches_nothing():
    streams = [make_stream(0, "audio", language="jpn", title="Japanese", is_default=True)]
    config = NormalizerConfig(auto_default_audio=True, preferred_audio_language="English")
    n = by_index(streams, config)[0]
    assert n.new_default is None
    assert n.changed is False


def test_apply_overrides_skips_selected_indices():
    streams = [make_stream(0, "audio", language="eng", title="Old"), make_stream(1, "subtitle", language="eng", title="Old2")]
    normalizations = normalize_streams(streams, NormalizerConfig())
    assert all(n.changed for n in normalizations)

    overridden = apply_overrides(normalizations, [0])
    result = {n.index: n for n in overridden}
    assert result[0].changed is False
    assert result[0].new_title == "Old"  # reverted to old title, not the proposed new one
    assert result[1].changed is True  # untouched by the override


def test_apply_overrides_handles_none_and_empty():
    streams = [make_stream(0, "audio", language="eng")]
    normalizations = normalize_streams(streams, NormalizerConfig())
    assert apply_overrides(normalizations, None) == normalizations
    assert apply_overrides(normalizations, []) == normalizations
