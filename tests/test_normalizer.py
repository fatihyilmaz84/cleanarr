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
    assert result[0].new_title == "日本語 - Original"
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
    streams = [make_stream(0, "audio", language="jpn", title="日本語", is_default=True)]
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


def test_distinct_titles_are_not_collapsed_into_identical_ones():
    """Three Chinese subtitle tracks labelled 廣東話 / 中文（繁體） / 中文（简体）
    all carry the same ISO code, so they all normalize to "Chinese" — and
    nothing intrinsic (no channel count on a subtitle, same codec) can
    disambiguate them. Renaming would leave a track picker showing three
    identical entries, having destroyed the only thing that told them apart.
    """
    streams = [
        make_stream(0, "video", codec_name="h264", language=None),
        make_stream(1, "subtitle", codec_name="subrip", language="chi", title="廣東話"),
        make_stream(2, "subtitle", codec_name="subrip", language="chi", title="中文（繁體）"),
        make_stream(3, "subtitle", codec_name="subrip", language="chi", title="中文（简体）"),
    ]

    results = {n.index: n for n in normalize_streams(streams, NormalizerConfig())}

    for index, original in [(1, "廣東話"), (2, "中文（繁體）"), (3, "中文（简体）")]:
        assert results[index].changed is False
        assert results[index].new_title == original
        assert "identical to another track" in results[index].reason


def test_identical_titles_are_still_normalized_together():
    # Nothing is lost here — the two tracks were already indistinguishable,
    # so collapsing them to the same canonical name destroys no information.
    streams = [
        make_stream(1, "subtitle", codec_name="subrip", language="spa", title="español"),
        make_stream(2, "subtitle", codec_name="subrip", language="spa", title="español"),
    ]

    results = normalize_streams(streams, NormalizerConfig())

    assert [n.new_title for n in results] == ["Español", "Español"]
    assert all(n.changed for n in results)


def test_a_group_that_can_be_disambiguated_still_is():
    # 5.1 vs stereo separates these, so they get suffixes rather than being
    # left alone — the existing behaviour must not regress.
    streams = [
        make_stream(1, "audio", codec_name="ac3", language="eng", channels=6),
        make_stream(2, "audio", codec_name="ac3", language="eng", channels=2),
    ]

    results = normalize_streams(streams, NormalizerConfig())

    assert [n.new_title for n in results] == ["English - 5.1", "English - Stereo"]


def test_a_detected_language_names_the_track_and_writes_the_missing_tag():
    """The case this exists for: a track with no language and no title, so
    there is nothing to name it from. Given a language read out of its own
    text, it gets both a title and — crucially — the language tag, without
    which it stays unidentifiable to every other player.
    """
    streams = [
        make_stream(0, "video", codec_name="h264", language=None),
        make_stream(1, "subtitle", codec_name="subrip", language=None, title=None),
    ]

    result = normalize_streams(streams, NormalizerConfig(), detected_languages={1: "dut"})[0]

    assert result.new_title == "Nederlands"
    assert result.old_language is None
    assert result.new_language == "dut"
    assert result.changed is True
    assert "identified from the track's own text" in result.reason


def test_detection_never_overrides_a_language_the_file_already_states():
    # The file's own tag is authoritative; detection only fills a gap.
    streams = [make_stream(1, "subtitle", codec_name="subrip", language="eng", title=None)]

    result = normalize_streams(streams, NormalizerConfig(), detected_languages={1: "dut"})[0]

    assert result.new_language == "eng"
    assert result.new_title == "English"


def test_without_a_detection_an_unlabelled_track_is_left_alone():
    streams = [make_stream(1, "subtitle", codec_name="subrip", language=None, title=None)]

    result = normalize_streams(streams, NormalizerConfig())[0]

    assert result.changed is False
    assert result.new_language is None
    assert "no language tag" in result.reason


def test_tracks_are_titled_in_their_own_language():
    """A track title is written into the file and read by whoever watches it,
    so it says what the language calls itself. Retitling "Nederlands" to
    "Dutch" was the normalizer's own doing, not anything configured.
    """
    streams = [
        make_stream(1, "subtitle", codec_name="subrip", language="dut", title="Nederlands"),
        make_stream(2, "subtitle", codec_name="subrip", language="ger", title="Deutsch"),
        make_stream(3, "subtitle", codec_name="subrip", language="tur", title="Türkçe"),
    ]

    results = normalize_streams(streams, NormalizerConfig())

    assert [n.new_title for n in results] == ["Nederlands", "Deutsch", "Türkçe"]
    # And so a file already labelled this way needs no work at all.
    assert all(n.changed is False for n in results)


def test_an_english_named_track_is_retitled_to_the_endonym():
    streams = [make_stream(1, "subtitle", codec_name="subrip", language="fre", title="French")]

    result = normalize_streams(streams, NormalizerConfig())[0]

    assert result.new_title == "Français"
    assert result.changed is True


def test_markers_still_normalize_alongside_an_endonym():
    # The attribute grammar is unchanged — only the language part of the
    # title moved to the endonym.
    streams = [
        make_stream(1, "subtitle", codec_name="subrip", language="dut", title="Nederlands (SDH)",
                    is_hearing_impaired=True),
        make_stream(2, "subtitle", codec_name="subrip", language="ger", title="Deutsch forced", is_forced=True),
    ]

    results = normalize_streams(streams, NormalizerConfig())

    assert [n.new_title for n in results] == ["Nederlands - SDH", "Deutsch - Forced"]


def test_titling_in_endonyms_is_idempotent():
    """Every pattern set must match the label this module emits, or a second
    pass strips what the first added. That property has to hold for endonym
    titles too, since schedules run this unattended.
    """
    streams = [
        make_stream(1, "subtitle", codec_name="subrip", language="dut", title="Nederlands - SDH",
                    is_hearing_impaired=True),
        make_stream(2, "audio", codec_name="ac3", language="tur", title="Türkçe - Commentary", is_commentary=True),
    ]

    results = normalize_streams(streams, NormalizerConfig())

    assert [n.new_title for n in results] == ["Nederlands - SDH", "Türkçe - Commentary"]
    assert all(n.changed is False for n in results)
