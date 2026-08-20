from app.languages import LANGUAGE_OPTIONS, iso_codes_for_language_name


def test_known_language_name_maps_to_codes():
    assert "kor" in iso_codes_for_language_name("Korean")


def test_lookup_is_case_and_whitespace_insensitive():
    assert iso_codes_for_language_name("  koREAN ") == iso_codes_for_language_name("Korean")


def test_language_with_distinct_bibliographic_and_terminologic_codes():
    codes = iso_codes_for_language_name("French")
    assert "fre" in codes  # ISO 639-2/B, common in older taggers
    assert "fra" in codes  # ISO 639-2/T


def test_unknown_name_returns_empty_set():
    assert iso_codes_for_language_name("Klingon") == frozenset()


def test_none_and_empty_input_return_empty_set():
    assert iso_codes_for_language_name(None) == frozenset()
    assert iso_codes_for_language_name("") == frozenset()


def test_every_dropdown_option_resolves_and_includes_its_own_code():
    names = [name for name, _ in LANGUAGE_OPTIONS]
    assert len(names) == len(set(names))  # no duplicate labels in the dropdown
    for name, code in LANGUAGE_OPTIONS:
        codes = iso_codes_for_language_name(name)
        assert code in codes, f"{name}: primary code {code!r} not in resolved set {codes!r}"


def test_endonym_is_what_the_language_calls_itself():
    from app.languages import endonym_for_code

    assert endonym_for_code("dut") == "Nederlands"
    assert endonym_for_code("ger") == "Deutsch"
    assert endonym_for_code("tur") == "Türkçe"
    assert endonym_for_code("jpn") == "日本語"
    assert endonym_for_code("eng") == "English"


def test_endonym_resolves_through_every_alias_code():
    # Taggers use either the bibliographic or terminologic code, so both
    # spellings of the same language must land on the same title.
    from app.languages import endonym_for_code

    assert endonym_for_code("dut") == endonym_for_code("nld") == "Nederlands"
    assert endonym_for_code("ger") == endonym_for_code("deu") == "Deutsch"
    assert endonym_for_code("fre") == endonym_for_code("fra") == "Français"
    assert endonym_for_code("cze") == endonym_for_code("ces") == "Čeština"


def test_every_offered_language_has_an_endonym():
    # A gap would silently fall back to the English name for that one
    # language, giving a library with one odd entry in it.
    from app.languages import LANGUAGE_OPTIONS, _CODE_TO_ENDONYM

    missing = [name for name, code in LANGUAGE_OPTIONS if code not in _CODE_TO_ENDONYM]
    assert missing == []


def test_endonym_is_unknown_for_an_unrecognised_code():
    from app.languages import endonym_for_code

    assert endonym_for_code("zzz") is None
    assert endonym_for_code(None) is None
    assert endonym_for_code("") is None


def test_the_apps_own_vocabulary_stays_english():
    """Endonyms are for titles written into files, read by whoever watches.
    The Rules keep-lists and settings dropdowns are read by the operator and
    stay in English — switching those too would mean picking "Nederlands"
    out of a dropdown to keep Dutch audio.
    """
    from app.languages import LANGUAGE_OPTIONS, language_name_for_code

    assert language_name_for_code("dut") == "Dutch"
    assert ("Dutch", "dut") in LANGUAGE_OPTIONS
