import pytest

from nevis.text import client_search_text, normalize

NORMALIZE_CASES = [
    ("José Ñúñez", "jose nunez"),
    ("JOHN Doe", "john doe"),
    ("Straße", "strasse"),
    ("  a \t b\n\nc  ", "a b c"),
    ("", ""),
    ("Zoë Çelik-Ångström", "zoe celik-angstrom"),
    # NFKD splits compatibility characters: the ligature.
    ("ﬁnance", "finance"),
    # Dotted capital I casefolds to i plus U+0307, which must go too.
    ("İstanbul", "istanbul"),
    ("Søren Łukasz", "soren lukasz"),
    ("Đorđe Ħamrun Ðór Þór", "dorde hamrun dor thor"),
    ("Ærø Œuvre \u0131", "aero oeuvre i"),  # \u0131: dotless i
    ("日本語 😀", "日本語 😀"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("text", "expected"), NORMALIZE_CASES)
def test_normalize_strips_accents_case_and_extra_whitespace(text: str, expected: str) -> None:
    assert normalize(text) == expected


@pytest.mark.unit
@pytest.mark.parametrize("text", [case[0] for case in NORMALIZE_CASES])
def test_normalize_is_idempotent(text: str) -> None:
    once = normalize(text)

    assert normalize(once) == once


@pytest.mark.unit
def test_client_search_text_joins_all_fields_normalized() -> None:
    result = client_search_text(
        first_name="José", last_name="Ñúñez", email="Jose@Example.com", description="Ünïcode  fan"
    )

    assert result == "jose nunez jose@example.com unicode fan"


@pytest.mark.unit
def test_client_search_text_skips_missing_description() -> None:
    result = client_search_text(
        first_name="John", last_name="Doe", email="john@x.com", description=None
    )

    assert result == "john doe john@x.com"
