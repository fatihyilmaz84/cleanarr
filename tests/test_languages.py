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
