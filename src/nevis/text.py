"""Normalization for search: case, accents and whitespace don't matter."""

import unicodedata

# Letters NFKD doesn't decompose into a base letter and an accent. (ß is covered by casefold.)
_UNDECOMPOSED_LETTERS = str.maketrans(
    {
        "ø": "o",
        "ł": "l",
        "đ": "d",
        "ħ": "h",
        "\u0131": "i",  # dotless i
        "ð": "d",
        "þ": "th",
        "æ": "ae",
        "œ": "oe",
    }
)


def normalize(text: str) -> str:
    """Lowercase (casefold, so ß becomes ss), strip accents and collapse whitespace."""
    # Casefold first: it can produce combining marks (İ becomes i and U+0307) for NFKD to split.
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(without_marks.translate(_UNDECOMPOSED_LETTERS).split())


def client_search_text(
    *, first_name: str, last_name: str, email: str, description: str | None
) -> str:
    """The normalized text trigram search runs over: the client's searchable fields, joined."""
    return normalize(" ".join(part for part in (first_name, last_name, email, description) if part))
