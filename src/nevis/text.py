"""Normalization for search: case, accents, script and whitespace don't matter."""

from anyascii import anyascii


def normalize(text: str) -> str:
    """Transliterate to ASCII (accents dropped, other scripts romanized), lowercase and collapse
    whitespace."""
    return " ".join(anyascii(text).lower().split())


def client_search_text(
    *, first_name: str, last_name: str, email: str, description: str | None
) -> str:
    """The normalized text trigram search runs over: the client's searchable fields, joined."""
    return normalize(" ".join(part for part in (first_name, last_name, email, description) if part))
