from itertools import pairwise

import pytest

from nevis.documents.chunking import CHUNK_OVERLAP_WORDS, CHUNK_TEXT_MAX_BYTES, chunk_text


def words_of(chunks: list[str]) -> list[str]:
    """The words in the chunks, with each chunk's overlap with the previous one removed."""
    words = chunks[0].split()
    for chunk in chunks[1:]:
        words += chunk.split()[CHUNK_OVERLAP_WORDS:]
    return words


@pytest.mark.unit
def test_short_text_is_one_chunk_with_whitespace_collapsed() -> None:
    assert chunk_text("Utility bill\n\nfrom   Vattenfall.\t") == ["Utility bill from Vattenfall."]


@pytest.mark.unit
def test_long_text_is_split_into_overlapping_chunks_within_the_byte_limit() -> None:
    text = " ".join(f"word{i}" for i in range(2000))

    chunks = chunk_text(text)

    assert len(chunks) > 1
    assert all(len(chunk.encode()) <= CHUNK_TEXT_MAX_BYTES for chunk in chunks)
    for previous, chunk in pairwise(chunks):
        assert chunk.split()[:CHUNK_OVERLAP_WORDS] == previous.split()[-CHUNK_OVERLAP_WORDS:]
    assert words_of(chunks) == text.split()


@pytest.mark.unit
def test_byte_limit_counts_multibyte_characters() -> None:
    text = " ".join(["Grüße", "日本語", "🙂"] * 500)

    chunks = chunk_text(text)

    assert all(len(chunk.encode()) <= CHUNK_TEXT_MAX_BYTES for chunk in chunks)
    assert words_of(chunks) == text.split()


@pytest.mark.unit
def test_word_longer_than_a_chunk_is_split_at_character_boundaries() -> None:
    word = "ä" * CHUNK_TEXT_MAX_BYTES  # 2 bytes per character

    chunks = chunk_text(f"start {word} end")

    assert all(len(chunk.encode()) <= CHUNK_TEXT_MAX_BYTES for chunk in chunks)
    # Pieces of a split word are too big to repeat as overlap, so each appears once.
    assert "".join(chunks).count("ä") == CHUNK_TEXT_MAX_BYTES
    assert chunks[0] == "start"
    assert chunks[-1] == "end"


@pytest.mark.unit
def test_text_that_exactly_fills_a_chunk_stays_one_chunk() -> None:
    text = "x" * CHUNK_TEXT_MAX_BYTES

    assert chunk_text(text) == [text]


@pytest.mark.unit
def test_blank_text_has_no_chunks() -> None:
    assert chunk_text(" \n\t ") == []
