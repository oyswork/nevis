"""Chunks of a document and their embeddings: written with the document, rebuilt by reembed."""

import uuid
from dataclasses import dataclass

from nevis.db import DbConnection, DbPool
from nevis.documents.chunking import chunk_text
from nevis.embeddings import EMBEDDING_MODEL, Embedder

INSERT_CHUNKS = """
    INSERT INTO document_chunks (document_id, ordinal, text, embedding)
    VALUES ($1, $2, $3, $4)
"""
# A new database has no row: the api's first start records the model it embeds with.
RECORD_EMBEDDING_MODEL = "INSERT INTO embedding_model (name) VALUES ($1) ON CONFLICT DO NOTHING"
SELECT_EMBEDDING_MODEL = "SELECT name FROM embedding_model"
SET_EMBEDDING_MODEL = """
    INSERT INTO embedding_model (name) VALUES ($1)
    ON CONFLICT (singleton) DO UPDATE SET name = excluded.name
"""


class EmbeddingModelMismatchError(Exception):
    """The stored chunk vectors come from another embedding model than the one running."""


async def check_embedding_model(pool: DbPool) -> None:
    """Raise unless the stored vectors come from EMBEDDING_MODEL (recorded on first use)."""
    await pool.execute(RECORD_EMBEDDING_MODEL, EMBEDDING_MODEL)
    stored = await pool.fetchval(SELECT_EMBEDDING_MODEL)
    if stored != EMBEDDING_MODEL:
        msg = (
            f"the stored vectors come from {stored}, but this code embeds with {EMBEDDING_MODEL}: "
            "re-embed every document first (just reembed --all)"
        )
        raise EmbeddingModelMismatchError(msg)


async def set_embedding_model(pool: DbPool) -> None:
    """Record EMBEDDING_MODEL as the model of the stored vectors (after re-embedding them all)."""
    await pool.execute(SET_EMBEDDING_MODEL, EMBEDDING_MODEL)


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
