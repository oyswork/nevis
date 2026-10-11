import re

import pytest

from nevis.search.clients import like_escape, regex_escape


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("\\", "\\\\"),
        ("%", "\\%"),
        ("_", "\\_"),
        ("50%_off\\", "50\\%\\_off\\\\"),
    ],
)
def test_like_escape_makes_wildcards_literal(text: str, expected: str) -> None:
    assert like_escape(text) == expected


@pytest.mark.unit
@pytest.mark.parametrize("character", list(".+([\\^$|?*&~#- {}]"))
def test_regex_escape_makes_each_metacharacter_literal(character: str) -> None:
    escaped = regex_escape(f"a{character}b")

    assert re.fullmatch(escaped, f"a{character}b")
    assert not re.fullmatch(escaped, "axb")
    assert not re.fullmatch(escaped, "ab")
