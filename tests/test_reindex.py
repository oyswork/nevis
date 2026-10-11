from collections.abc import AsyncGenerator

import pytest
from httpx2 import AsyncClient
from meilisearch_python_sdk import AsyncClient as SearchClient

from nevis.config import Settings
from nevis.db import DbPool
from nevis.outbox.handlers import create_outbox_worker
from nevis.search.client import create_search_client
from nevis.search.indexes import CLIENTS_INDEX, DOCUMENT_CHUNKS_INDEX
from nevis.search.reindex import reindex

CLIENT = {"first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com"}
DOCUMENT = {"title": "Utility bill", "content": "Electricity bill from Vattenfall."}
ORPHAN = {"id": "orphan", "first_name": "Gone", "last_name": "Away", "email": "gone@example.com"}


@pytest.fixture
async def search_client(db_settings: Settings) -> AsyncGenerator[SearchClient]:
    async with create_search_client(db_settings) as client:
        yield client


async def indexed_ids(search_client: SearchClient, uid: str) -> set[str]:
    result = await search_client.index(uid).get_documents(limit=1000)
    return {document["id"] for document in result.results}


@pytest.mark.integration
async def test_reindex_rebuilds_the_indexes_from_postgres(
    db_client: AsyncClient, db_pool: DbPool, search_client: SearchClient
) -> None:
    client_id = (await db_client.post("/clients", json=CLIENT)).json()["id"]
    document_id = (await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)).json()[
        "id"
    ]
    await db_pool.execute("DELETE FROM outbox")  # as if the jobs were lost
    task = await search_client.index(CLIENTS_INDEX).add_documents([ORPHAN])
    await search_client.wait_for_task(task.task_uid, raise_for_status=True)

    enqueued = await reindex(db_pool, search_client)
    await create_outbox_worker(db_pool, search_client).run_once()

    assert enqueued == 2
    assert await indexed_ids(search_client, CLIENTS_INDEX) == {client_id}  # orphan dropped
    assert await indexed_ids(search_client, DOCUMENT_CHUNKS_INDEX) == {f"{document_id}-0"}
