import uuid

import asyncpg

from nevis.db import DbPool
from nevis.db_models import Document
from nevis.documents.schemas import DocumentCreate
from nevis.outbox import repository as outbox
from nevis.outbox.repository import OutboxKind

CLIENT_FOREIGN_KEY = "fk_documents_client"

INSERT_DOCUMENT = """
    INSERT INTO documents (client_id, title, content)
    VALUES ($1, $2, $3)
    RETURNING *
"""
SELECT_DOCUMENT = "SELECT * FROM documents WHERE id = $1"


class ClientNotFoundError(Exception):
    """The document's client doesn't exist."""


async def create_document(
    pool: DbPool, client_id: uuid.UUID, data: DocumentCreate, *, summarize: bool
) -> Document:
    """Insert the document and, in the same transaction, the jobs that index it for search
    and, if `summarize`, summarize it."""
    try:
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(INSERT_DOCUMENT, client_id, data.title, data.content)
            if row is None:  # INSERT ... RETURNING always yields the inserted row
                msg = "INSERT INTO documents returned no row"
                raise RuntimeError(msg)
            document = Document(**row)
            await outbox.enqueue(connection, OutboxKind.INDEX_DOCUMENT, document.id)
            if summarize:
                await outbox.enqueue(connection, OutboxKind.SUMMARIZE_DOCUMENT, document.id)
    except asyncpg.ForeignKeyViolationError as exc:
        if exc.constraint_name == CLIENT_FOREIGN_KEY:
            raise ClientNotFoundError from exc
        raise
    return document


async def get_document(pool: DbPool, document_id: uuid.UUID) -> Document | None:
    row = await pool.fetchrow(SELECT_DOCUMENT, document_id)
    return None if row is None else Document(**row)
