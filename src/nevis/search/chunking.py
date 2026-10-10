"""Split document content into overlapping chunks, each embedded and indexed on its own.

The embedder reads at most EMBEDDED_TEXT_MAX_BYTES of "title: chunk"; chunks stay well under
that (titles are at most 200 characters), so nothing a chunk holds is cut off.
"""

from collections.abc import Iterator

CHUNK_TEXT_MAX_BYTES = 1200
# Words repeated from the end of the previous chunk, so text at a boundary keeps its context.
CHUNK_OVERLAP_WORDS = 20
# The overlap never takes more than this, so every chunk adds new text.
CHUNK_OVERLAP_MAX_BYTES = CHUNK_TEXT_MAX_BYTES // 2


def chunk_text(text: str) -> list[str]:
    """Chunks of whole words (whitespace collapsed), each at most CHUNK_TEXT_MAX_BYTES."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0  # bytes of " ".join(current)
    for word in _words(text):
        word_size = len(word.encode())
        if current and size + 1 + word_size > CHUNK_TEXT_MAX_BYTES:
            chunks.append(" ".join(current))
            current = _overlap(current)
            size = _joined_size(current)
            if current and size + 1 + word_size > CHUNK_TEXT_MAX_BYTES:
                current, size = [], 0
        size += word_size + (1 if current else 0)
        current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _words(text: str) -> Iterator[str]:
    """The words of the text; words longer than a chunk come in chunk-sized pieces."""
    for word in text.split():
        if len(word.encode()) <= CHUNK_TEXT_MAX_BYTES:
            yield word
            continue
        piece: list[str] = []
        piece_size = 0
        for character in word:
            character_size = len(character.encode())
            if piece_size + character_size > CHUNK_TEXT_MAX_BYTES:
                yield "".join(piece)
                piece, piece_size = [], 0
            piece.append(character)
            piece_size += character_size
        yield "".join(piece)


def _overlap(words: list[str]) -> list[str]:
    tail = words[-CHUNK_OVERLAP_WORDS:]
    while tail and _joined_size(tail) > CHUNK_OVERLAP_MAX_BYTES:
        tail = tail[1:]
    return tail


def _joined_size(words: list[str]) -> int:
    return sum(len(word.encode()) for word in words) + max(len(words) - 1, 0)
