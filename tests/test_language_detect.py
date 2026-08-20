"""Covers the subtitle language detector.

The samples here are the real thing: the two untagged subtitle tracks of a
release that carries no language and no title on either of them, which is
the case the detector exists for.

The central property is not accuracy but *restraint* — a detected language
is written into a media file as a language tag, so a wrong answer is worse
than no answer. Several tests below assert that it declines.
"""

from __future__ import annotations

from app.language_detect import MIN_TOKENS, detect_language, score_languages

# Verbatim from the untagged tracks of
# "Ocean's Twelve (2004) - WEBDL-1080p Radarr.mkv" — the file that prompted
# this, where ffprobe reports a literally empty tag set for both.
ENGLISH_SAMPLE = """
1
00:00:412,000 --> 00:00:414,000
Hi.
- How'd it go?
- Lousy.
Got a '63 Thunderbird
I would've sold in a day a year ago.
Now they just wanna look at the pictures.
You smell nice.
How was your day?
It was great.
We had a big breakthrough in the Bulgari case.
Really? That is wonderful news for you.
I have to go back to the office
and finish the paperwork tonight.
Do you want me to wait up for you?
No, it is going to be very late.
"""

DUTCH_SAMPLE = """
1
00:00:412,000 --> 00:00:414,000
ROME
Drie en een half jaar geleden
Hoe is 't gegaan?
-Waardeloos.
Een Thunderbird uit '63 die ik
een jaar geleden zo had verkocht.
Nu willen ze alleen de foto's zien.
Je ruikt lekker.
Hoe was jouw dag?
-Fantastisch.
Een doorbraak in de Bulgari-zaak.
Echt...? Dat is geweldig nieuws voor je.
Ik moet terug naar kantoor
en het papierwerk vanavond afmaken.
Wil je dat ik op je wacht?
Nee, het wordt heel erg laat.
"""


def test_identifies_the_real_untagged_english_track():
    assert detect_language(ENGLISH_SAMPLE) == "eng"


def test_identifies_the_real_untagged_dutch_track():
    assert detect_language(DUTCH_SAMPLE) == "dut"


def test_cue_numbers_and_timestamps_do_not_count_as_words():
    # The SRT scaffolding is identical in every language; counting it would
    # dilute every score toward the same value.
    scores = score_languages(ENGLISH_SAMPLE)
    assert scores["eng"] > 0.3


def test_declines_on_a_sample_too_short_to_judge():
    assert detect_language("Hi.\nYes.\nNo.\nOkay.") is None


def test_declines_between_closely_related_languages():
    """Danish and Norwegian bokmål share nearly all their common words. The
    honest answer is that the text does not say which, and a track left
    untagged is better than one tagged wrongly.
    """
    danish = (
        "Og det er ikke det jeg vil have nu. Jeg har ikke set hende her i dag. "
        "Hvad skal vi gøre med det som han har taget med. Det er ikke godt for os. "
    ) * 4
    assert detect_language(danish) is None


def test_declines_on_text_with_no_recognisable_function_words():
    # Song lyrics, sound effects, a signs-only track: nothing to go on.
    assert detect_language("\n".join(["[GUNSHOT]", "[MUSIC PLAYING]", "♪♪♪"] * 30)) is None


def test_scripts_identify_their_language_without_a_word_list():
    cases = {
        "Καλησπέρα σας, τι κάνετε σήμερα εδώ πέρα φίλοι μου": "gre",
        "こんにちは、今日はいい天気ですね。またあとで会いましょう": "jpn",
        "안녕하세요 오늘 날씨가 좋네요 나중에 봐요": "kor",
        "สวัสดีครับ วันนี้อากาศดีมากเลยนะครับ": "tha",
        "नमस्ते आज मौसम बहुत अच्छा है फिर मिलेंगे": "hin",
        "שלום מה שלומך היום הכל בסדר": "heb",
    }
    for text, expected in cases.items():
        assert detect_language(text) == expected, text


def test_cyrillic_is_narrowed_by_letters_unique_to_one_language():
    russian = "Что ты здесь делаешь сегодня вечером мы ещё не решили этот вопрос"
    ukrainian = "Що ти тут робиш сьогодні ввечері їхні справи ще не вирішені їй"
    assert detect_language(russian) == "rus"
    assert detect_language(ukrainian) == "ukr"


def test_han_without_kana_is_chinese_but_kana_settles_japanese():
    # Japanese text is mostly Han characters; the kana are what distinguish
    # it, so a handful of them must outweigh the ideograph count.
    assert detect_language("我们今天要去看电影你想一起来吗") == "chi"
    assert detect_language("私は今日映画を見に行きますよ一緒に来ますか") == "jpn"


def test_min_tokens_guard_is_actually_enforced():
    short = " ".join(["the"] * (MIN_TOKENS - 1))
    assert detect_language(short) is None
    assert detect_language(" ".join(["the and is you not"] * 20)) == "eng"
