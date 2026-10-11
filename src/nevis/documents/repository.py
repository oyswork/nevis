import uuid

import asyncpg

from nevis.db import DbPool
from nevis.db_models import Document
from nevis.documents.chunks import embed_document, insert_chunks
from nevis.documents.columns import DOCUMENT_COLUMNS
from nevis.documents.schemas import DocumentCreate
from nevis.embeddings import Embedder
from nevis.summaries import repository as summaries

CLIENT_FOREIGN_KEY = "fk_documents_client"

INSERT_DOCUMENT = f"""
    INSERT INTO documents (client_id, title, content)
    VALUES ($1, $2, $3)
    RETURNING {DOCUMENT_COLUMNS}
"""  # noqa: S608 - constant column list
CLIENT_EXISTS = "SELECT EXISTS (SELECT 1 FROM clients WHERE id = $1)"
SELECT_DOCUMENT = f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE id = $1"  # noqa: S608 - constant column list


class ClientNotFoundError(Exception):
    """The document's client doesn't exist."""


async def create_document(
    pool: DbPool,
    embedder: Embedder,
    client_id: uuid.UUID,
    data: DocumentCreate,
) -> Document:
    """Insert the document and, in the same transaction, its embedded chunks and the
    notification that wakes the summary worker (delivered on commit; without a worker or a model
    the document just stays unsummarized).

    Embedding takes a while (CPU-bound), so it happens first: no connection or transaction is
    held open meanwhile. The client is checked first, so a request for an unknown client doesn't
    pay for embedding; the foreign key still decides when it is deleted in between.
    """
    if not await pool.fetchval(CLIENT_EXISTS, client_id):
        raise ClientNotFoundError
    chunks = await embed_document(embedder, data.title, data.content)
    try:
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(INSERT_DOCUMENT, client_id, data.title, data.content)
            if row is None:  # INSERT ... RETURNING always yields the inserted row
                msg = "INSERT INTO documents returned no row"
                raise RuntimeError(msg)
            document = Document(**row)
            await insert_chunks(connection, document.id, chunks)
            await summaries.notify_document(connection, document.id)
    except asyncpg.ForeignKeyViolationError as exc:
        if exc.constraint_name == CLIENT_FOREIGN_KEY:
            raise ClientNotFoundError from exc
        raise
    return document


async def get_document(pool: DbPool, document_id: uuid.UUID) -> Document | None:
    row = await pool.fetchrow(SELECT_DOCUMENT, document_id)
    return None if row is None else Document(**row)
