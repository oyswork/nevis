"""Copy rows from Postgres into the search indexes. Run by outbox jobs, so idempotent."""

import uuid

from meilisearch_python_sdk import AsyncClient

from nevis.db import DbPool
from nevis.db_models import Client, Document
from nevis.search.chunking import chunk_text
from nevis.search.indexes import CLIENT_TEXT_FIELDS, CLIENTS_INDEX, DOCUMENT_CHUNKS_INDEX

SELECT_CLIENT = "SELECT * FROM clients WHERE id = $1"
SELECT_DOCUMENT = "SELECT * FROM documents WHERE id = $1"
# Indexing one record is quick; this bounds a job when Meilisearch's task queue is backed up.
INDEX_TASK_TIMEOUT_MS = 60_000


def client_document(client: Client) -> dict[str, str | None]:
    return {"id": str(client.id)} | {field: getattr(client, field) for field in CLIENT_TEXT_FIELDS}


async def index_client(pool: DbPool, search_client: AsyncClient, client_id: uuid.UUID) -> None:
    """Make the index match Postgres: upsert the client, or drop it if it no longer exists."""
    row = await pool.fetchrow(SELECT_CLIENT, client_id)
    index = search_client.index(CLIENTS_INDEX)
    if row is None:
        task = await index.delete_document(str(client_id))
    else:
        task = await index.add_documents([client_document(Client(**row))])
    await _wait(search_client, task.task_uid)


def chunk_documents(document: Document) -> list[dict[str, str | int]]:
    """The document's chunks as index entries; ids are stable, so re-indexing overwrites them."""
    return [
        {
            "id": f"{document.id}-{ordinal}",
            "document_id": str(document.id),
            "client_id": str(document.client_id),
            "ordinal": ordinal,
            "title": document.title,
            "text": text,
        }
        for ordinal, text in enumerate(chunk_text(document.content))
    ]


async def index_document(pool: DbPool, search_client: AsyncClient, document_id: uuid.UUID) -> None:
    """Make the index match Postgres: upsert the chunks, or drop them if it no longer exists."""
    row = await pool.fetchrow(SELECT_DOCUMENT, document_id)
    index = search_client.index(DOCUMENT_CHUNKS_INDEX)
    of_document = f'document_id = "{document_id}"'  # a UUID: nothing to escape
    if row is None:
        task = await index.delete_documents_by_filter(of_document)
        await _wait(search_client, task.task_uid)
        return
    chunks = chunk_documents(Document(**row))
    await _wait(search_client, (await index.add_documents(chunks)).task_uid)
    # Content that got shorter leaves chunks past the new last one; drop them after the upsert,
    # so the document stays searchable throughout.
    stale = f"{of_document} AND ordinal >= {len(chunks)}"
    await _wait(search_client, (await index.delete_documents_by_filter(stale)).task_uid)


async def _wait(search_client: AsyncClient, task_uid: int) -> None:
    await search_client.wait_for_task(
        task_uid, timeout_in_ms=INDEX_TASK_TIMEOUT_MS, raise_for_status=True
    )
