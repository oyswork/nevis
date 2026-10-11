"""Chunks of a document and their embeddings: written with the document, rebuilt by reembed."""

import uuid
from dataclasses import dataclass

from nevis.db import DbConnection
from nevis.embeddings import Embedder
from nevis.search.chunking import chunk_text

INSERT_CHUNKS = """
    INSERT INTO document_chunks (document_id, ordinal, text, embedding)
    VALUES ($1, $2, $3, $4)
"""


@dataclass(frozen=True)
class DocumentChunks:
    """A document's chunk texts with one embedding each, in order."""

    texts: list[str]
    embeddings: list[list[float]]


async def embed_document(embedder: Embedder, title: str, content: str) -> DocumentChunks:
    """Chunk the content and embed each chunk as "title: chunk" (the title gives it context)."""
    texts = chunk_text(content)
    embeddings = await embedder.embed_passages([f"{title}: {text}" for text in texts])
    return DocumentChunks(texts, embeddings)


async def insert_chunks(
    connection: DbConnection, document_id: uuid.UUID, chunks: DocumentChunks
) -> None:
    # executemany, not unnest($n::vector[]): asyncpg doesn't apply the vector codec to the array.
    await connection.executemany(
        INSERT_CHUNKS,
        [
            (document_id, ordinal, text, embedding)
            for ordinal, (text, embedding) in enumerate(
                zip(chunks.texts, chunks.embeddings, strict=True)
            )
        ],
    )
