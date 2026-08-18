"""Maps the human-readable language names Sonarr/Radarr report (e.g.
"Korean", "originalLanguage.name") to the ISO 639-2 codes ffprobe tags
streams with, so a file's own original-language track can be recognized and
protected regardless of the global audio/subtitle keep-list.

Covers both the ISO 639-2/B (bibliographic) and 639-2/T (terminologic) codes
where they differ (e.g. "fre"/"fra" for French) since taggers use either.
"""

from __future__ import annotations

_LANGUAGE_NAME_TO_ISO639_2: dict[str, frozenset[str]] = {
    "english": frozenset({"eng", "en"}),
    "korean": frozenset({"kor", "ko"}),
    "japanese": frozenset({"jpn", "ja"}),
    "french": frozenset({"fre", "fra", "fr"}),
    "german": frozenset({"ger", "deu", "de"}),
    "spanish": frozenset({"spa", "es"}),
    "italian": frozenset({"ita", "it"}),
    "portuguese": frozenset({"por", "pt"}),
    "russian": frozenset({"rus", "ru"}),
    "chinese": frozenset({"chi", "zho", "zh"}),
    "cantonese": frozenset({"chi", "zho", "zh", "yue"}),
    "mandarin": frozenset({"chi", "zho", "zh", "cmn"}),
    "turkish": frozenset({"tur", "tr"}),
    "hindi": frozenset({"hin", "hi"}),
    "arabic": frozenset({"ara", "ar"}),
    "hebrew": frozenset({"heb", "he"}),
    "dutch": frozenset({"dut", "nld", "nl"}),
    "flemish": frozenset({"dut", "nld", "nl"}),
    "swedish": frozenset({"swe", "sv"}),
    "norwegian": frozenset({"nor", "nob", "nno", "no"}),
    "danish": frozenset({"dan", "da"}),
    "finnish": frozenset({"fin", "fi"}),
    "polish": frozenset({"pol", "pl"}),
    "czech": frozenset({"cze", "ces", "cs"}),
    "greek": frozenset({"gre", "ell", "el"}),
    "hungarian": frozenset({"hun", "hu"}),
    "romanian": frozenset({"rum", "ron", "ro"}),
    "ukrainian": frozenset({"ukr", "uk"}),
    "thai": frozenset({"tha", "th"}),
    "vietnamese": frozenset({"vie", "vi"}),
    "indonesian": frozenset({"ind", "id"}),
    "malay": frozenset({"may", "msa", "ms"}),
    "tamil": frozenset({"tam", "ta"}),
    "telugu": frozenset({"tel", "te"}),
    "kannada": frozenset({"kan", "kn"}),
    "malayalam": frozenset({"mal", "ml"}),
    "persian": frozenset({"per", "fas", "fa"}),
    "farsi": frozenset({"per", "fas", "fa"}),
    "icelandic": frozenset({"ice", "isl", "is"}),
    "slovak": frozenset({"slo", "slk", "sk"}),
    "slovenian": frozenset({"slv", "sl"}),
    "croatian": frozenset({"hrv", "hr"}),
    "serbian": frozenset({"srp", "scc", "sr"}),
    "bulgarian": frozenset({"bul", "bg"}),
    "macedonian": frozenset({"mac", "mkd", "mk"}),
    "albanian": frozenset({"alb", "sqi", "sq"}),
    "basque": frozenset({"baq", "eus", "eu"}),
    "catalan": frozenset({"cat", "ca"}),
    "galician": frozenset({"glg", "gl"}),
    "lithuanian": frozenset({"lit", "lt"}),
    "latvian": frozenset({"lav", "lv"}),
    "estonian": frozenset({"est", "et"}),
    "filipino": frozenset({"fil", "tgl", "tl"}),
    "tagalog": frozenset({"fil", "tgl", "tl"}),
    "bosnian": frozenset({"bos", "bs"}),
    "kazakh": frozenset({"kaz", "kk"}),
    "mongolian": frozenset({"mon", "mn"}),
    "welsh": frozenset({"wel", "cym", "cy"}),
    "bengali": frozenset({"ben", "bn"}),
    "dhivehi": frozenset({"div", "dv"}),
    "azerbaijani": frozenset({"aze", "az"}),
    "armenian": frozenset({"arm", "hye", "hy"}),
    "georgian": frozenset({"geo", "kat", "ka"}),
}


def iso_codes_for_language_name(name: str | None) -> frozenset[str]:
    """Return the set of ISO 639-2 (and 639-1) codes that could tag a stream
    in this language, or an empty set for unknown/unset input. Never raises
    — an unmapped name (e.g. "Original", "No Language", a Radarr/Sonarr
    value not in the table above) just means no track gets special-cased.
    """
    if not name:
        return frozenset()
    return _LANGUAGE_NAME_TO_ISO639_2.get(name.strip().lower(), frozenset())


# (display name, primary ISO 639-2 code) — one entry per distinct language,
# for the Rules page's language dropdowns. Aliases (Cantonese/Mandarin under
# Chinese, Flemish under Dutch, Farsi under Persian, Tagalog under Filipino)
# are deliberately left out here since they'd just duplicate an entry; they
# still resolve correctly via iso_codes_for_language_name.
LANGUAGE_OPTIONS: list[tuple[str, str]] = [
    ("English", "eng"),
    ("Korean", "kor"),
    ("Japanese", "jpn"),
    ("French", "fre"),
    ("German", "ger"),
    ("Spanish", "spa"),
    ("Italian", "ita"),
    ("Portuguese", "por"),
    ("Russian", "rus"),
    ("Chinese", "chi"),
    ("Turkish", "tur"),
    ("Hindi", "hin"),
    ("Arabic", "ara"),
    ("Hebrew", "heb"),
    ("Dutch", "dut"),
    ("Swedish", "swe"),
    ("Norwegian", "nor"),
    ("Danish", "dan"),
    ("Finnish", "fin"),
    ("Polish", "pol"),
    ("Czech", "cze"),
    ("Greek", "gre"),
    ("Hungarian", "hun"),
    ("Romanian", "rum"),
    ("Ukrainian", "ukr"),
    ("Thai", "tha"),
    ("Vietnamese", "vie"),
    ("Indonesian", "ind"),
    ("Malay", "may"),
    ("Tamil", "tam"),
    ("Telugu", "tel"),
    ("Kannada", "kan"),
    ("Malayalam", "mal"),
    ("Persian", "per"),
    ("Icelandic", "ice"),
    ("Slovak", "slo"),
    ("Slovenian", "slv"),
    ("Croatian", "hrv"),
    ("Serbian", "srp"),
    ("Bulgarian", "bul"),
    ("Macedonian", "mac"),
    ("Albanian", "alb"),
    ("Basque", "baq"),
    ("Catalan", "cat"),
    ("Galician", "glg"),
    ("Lithuanian", "lit"),
    ("Latvian", "lav"),
    ("Estonian", "est"),
    ("Filipino", "fil"),
    ("Bosnian", "bos"),
    ("Kazakh", "kaz"),
    ("Mongolian", "mon"),
    ("Welsh", "wel"),
    ("Bengali", "ben"),
    ("Dhivehi", "div"),
    ("Azerbaijani", "aze"),
    ("Armenian", "arm"),
    ("Georgian", "geo"),
]
