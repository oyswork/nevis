"""Copy rows from Postgres into the search indexes. Run by outbox jobs, so idempotent."""

import uuid

from meilisearch_python_sdk import AsyncClient

from nevis.db import DbPool
from nevis.db_models import Client
from nevis.search.indexes import CLIENT_TEXT_FIELDS, CLIENTS_INDEX

SELECT_CLIENT = "SELECT * FROM clients WHERE id = $1"
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
    await search_client.wait_for_task(
        task.task_uid, timeout_in_ms=INDEX_TASK_TIMEOUT_MS, raise_for_status=True
    )
