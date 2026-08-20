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


# What each language calls itself. Track titles are written into media files
# and read by whoever watches them, so a Dutch subtitle track saying
# "Nederlands" is more use in a player's track picker than one saying
# "Dutch" — and it's what these files usually already say.
#
# Capitalised even where the language itself lowercases its own name
# (français, polski, dansk): these are titles, they sit alongside "English"
# and "Forced" in the same picker, and a mixed-case list reads as an
# accident rather than a convention.
#
# Keyed by the English display name in LANGUAGE_OPTIONS above, and expanded
# across each language's alias codes by _build_code_to_endonym below.
_ENDONYMS: dict[str, str] = {
    "English": "English",
    "Korean": "한국어",
    "Japanese": "日本語",
    "French": "Français",
    "German": "Deutsch",
    "Spanish": "Español",
    "Italian": "Italiano",
    "Portuguese": "Português",
    "Russian": "Русский",
    "Chinese": "中文",
    "Turkish": "Türkçe",
    "Hindi": "हिन्दी",
    "Arabic": "العربية",
    "Hebrew": "עברית",
    "Dutch": "Nederlands",
    "Swedish": "Svenska",
    "Norwegian": "Norsk",
    "Danish": "Dansk",
    "Finnish": "Suomi",
    "Polish": "Polski",
    "Czech": "Čeština",
    "Greek": "Ελληνικά",
    "Hungarian": "Magyar",
    "Romanian": "Română",
    "Ukrainian": "Українська",
    "Thai": "ไทย",
    "Vietnamese": "Tiếng Việt",
    "Indonesian": "Bahasa Indonesia",
    "Malay": "Bahasa Melayu",
    "Tamil": "தமிழ்",
    "Telugu": "తెలుగు",
    "Kannada": "ಕನ್ನಡ",
    "Malayalam": "മലയാളം",
    "Persian": "فارسی",
    "Icelandic": "Íslenska",
    "Slovak": "Slovenčina",
    "Slovenian": "Slovenščina",
    "Croatian": "Hrvatski",
    "Serbian": "Српски",
    "Bulgarian": "Български",
    "Macedonian": "Македонски",
    "Albanian": "Shqip",
    "Basque": "Euskara",
    "Catalan": "Català",
    "Galician": "Galego",
    "Lithuanian": "Lietuvių",
    "Latvian": "Latviešu",
    "Estonian": "Eesti",
    "Filipino": "Filipino",
    "Bosnian": "Bosanski",
    "Kazakh": "Қазақша",
    "Mongolian": "Монгол",
    "Welsh": "Cymraeg",
    "Bengali": "বাংলা",
    "Dhivehi": "ދިވެހި",
    "Azerbaijani": "Azərbaycan",
    "Armenian": "Հայերեն",
    "Georgian": "ქართული",
}


def _build_code_to_name() -> dict[str, str]:
    # Built from LANGUAGE_OPTIONS (not the alias dict directly) so aliases
    # like Cantonese/Mandarin never displace Chinese as the canonical name
    # for a shared code — LANGUAGE_OPTIONS deliberately excludes them.
    mapping: dict[str, str] = {}
    for name, _primary_code in LANGUAGE_OPTIONS:
        for code in iso_codes_for_language_name(name):
            mapping.setdefault(code, name)
    return mapping


def _build_code_to_endonym() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name, _primary_code in LANGUAGE_OPTIONS:
        endonym = _ENDONYMS.get(name)
        if not endonym:
            continue
        for code in iso_codes_for_language_name(name):
            mapping.setdefault(code, endonym)
    return mapping


_CODE_TO_NAME: dict[str, str] = _build_code_to_name()
_CODE_TO_ENDONYM: dict[str, str] = _build_code_to_endonym()


def language_name_for_code(code: str | None) -> str | None:
    """Reverse of iso_codes_for_language_name: "eng" -> "English". None for
    an unrecognized or unset code — never guesses.
    """
    if not code:
        return None
    return _CODE_TO_NAME.get(code.strip().lower())


def endonym_for_code(code: str | None) -> str | None:
    """What a track in this language calls itself: "dut" -> "Nederlands".

    Used for the titles the normalizer writes into files, where the reader
    is whoever is watching. The English name (language_name_for_code) stays
    the app's own vocabulary — it's what the Rules keep-lists and the
    settings dropdowns are written in, and those are read by the operator.

    Falls back to the English name for a language with no endonym recorded,
    so a gap in the table costs a nicety rather than the whole title, and
    None for a code that isn't recognised at all — which the normalizer
    treats as "leave this track alone".
    """
    if not code:
        return None
    code = code.strip().lower()
    return _CODE_TO_ENDONYM.get(code) or _CODE_TO_NAME.get(code)
