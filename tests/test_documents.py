import datetime
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import NonCallableMagicMock

import pytest
from httpx2 import AsyncClient
from meilisearch_python_sdk import AsyncClient as SearchClient

from nevis.config import Settings
from nevis.db import DbPool, create_pool
from nevis.outbox.handlers import create_outbox_worker
from nevis.search.chunking import CHUNK_TEXT_MAX_BYTES
from nevis.search.client import create_search_client
from nevis.search.indexes import DOCUMENT_CHUNKS_INDEX
from nevis.search.indexing import index_document

CLIENT = {"first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com"}
DOCUMENT = {
    "title": "Utility bill March",
    "content": "Electricity bill from Vattenfall for 12 Lake Road, Zurich. Amount due CHF 84.20.",
}
SOME_ID = "8a0d5ff5-8f43-4a43-9d5d-3b3a5c4f5e11"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "   "),
        ("title", "x" * 201),
        ("title", "bill\x00"),
        ("content", ""),
        ("content", " \n "),
        ("content", "x" * 100_001),
        ("content", "a\x00b"),
        ("title", 42),
    ],
)
async def test_create_document_rejects_invalid_field(
    client: AsyncClient, field: str, value: object
) -> None:
    response = await client.post(f"/clients/{SOME_ID}/documents", json={**DOCUMENT, field: value})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", field]


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["title", "content"])
async def test_create_document_rejects_missing_field(client: AsyncClient, missing: str) -> None:
    payload = {key: value for key, value in DOCUMENT.items() if key != missing}

    response = await client.post(f"/clients/{SOME_ID}/documents", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", missing]


@pytest.mark.unit
async def test_create_document_rejects_unknown_field(client: AsyncClient) -> None:
    payload = {**DOCUMENT, "client_id": SOME_ID}

    response = await client.post(f"/clients/{SOME_ID}/documents", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "client_id"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/clients/not-a-uuid/documents"),
        ("GET", "/documents/not-a-uuid"),
        ("GET", "/clients/not-a-uuid"),
    ],
)
async def test_malformed_id_in_path_is_rejected(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path, json=DOCUMENT)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "path"


@pytest.mark.unit
async def test_create_document_returns_503_when_database_is_unreachable(
    client: AsyncClient, fake_pool: NonCallableMagicMock
) -> None:
    fake_pool.acquire.side_effect = ConnectionRefusedError

    response = await client.post(f"/clients/{SOME_ID}/documents", json=DOCUMENT)

    assert response.status_code == 503


@pytest.fixture
async def db_pool(db_settings: Settings) -> AsyncGenerator[DbPool]:
    pool = await create_pool(db_settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def search_client(db_settings: Settings) -> AsyncGenerator[SearchClient]:
    async with create_search_client(db_settings) as client:
        yield client


@pytest.fixture
async def client_id(db_client: AsyncClient) -> str:
    client_id: str = (await db_client.post("/clients", json=CLIENT)).json()["id"]
    return client_id


async def chunk_ordinals(search_client: SearchClient, document_id: str) -> list[int]:
    result = await search_client.index(DOCUMENT_CHUNKS_INDEX).get_documents(
        filter=f'document_id = "{document_id}"', limit=1000
    )
    return sorted(chunk["ordinal"] for chunk in result.results)


@pytest.mark.integration
async def test_create_document_returns_201_with_the_document(
    db_client: AsyncClient, client_id: str
) -> None:
    response = await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)

    assert response.status_code == 201
    body = response.json()
    uuid.UUID(body.pop("id"))
    assert datetime.datetime.fromisoformat(body.pop("created_at")).tzinfo is not None
    assert body == {**DOCUMENT, "client_id": client_id, "summary": None}


@pytest.mark.integration
async def test_create_document_for_unknown_client_returns_404_and_enqueues_nothing(
    db_client: AsyncClient, db_pool: DbPool
) -> None:
    response = await db_client.post(f"/clients/{SOME_ID}/documents", json=DOCUMENT)

    assert response.status_code == 404
    assert response.json() == {"detail": "Client not found"}
    assert await db_pool.fetchval("SELECT count(*) FROM outbox WHERE kind = 'index_document'") == 0


@pytest.mark.integration
async def test_create_document_enqueues_an_index_job(
    db_client: AsyncClient, db_pool: DbPool, client_id: str
) -> None:
    document = (await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)).json()

    entity_ids = await db_pool.fetch("SELECT entity_id FROM outbox WHERE kind = 'index_document'")

    assert [str(row["entity_id"]) for row in entity_ids] == [document["id"]]


@pytest.mark.integration
async def test_get_document_returns_it_or_404(db_client: AsyncClient, client_id: str) -> None:
    created = (await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)).json()

    found = await db_client.get(f"/documents/{created['id']}")
    missing = await db_client.get(f"/documents/{SOME_ID}")

    assert (found.status_code, found.json()) == (200, created)
    assert (missing.status_code, missing.json()) == (404, {"detail": "Document not found"})


@pytest.mark.integration
async def test_get_client_returns_it_or_404(db_client: AsyncClient) -> None:
    created = (await db_client.post("/clients", json=CLIENT)).json()

    found = await db_client.get(f"/clients/{created['id']}")
    missing = await db_client.get(f"/clients/{SOME_ID}")

    assert (found.status_code, found.json()) == (200, created)
    assert (missing.status_code, missing.json()) == (404, {"detail": "Client not found"})


@pytest.mark.integration
async def test_worker_indexes_document_chunks_and_drops_stale_ones(
    db_client: AsyncClient, db_pool: DbPool, search_client: SearchClient, client_id: str
) -> None:
    long_document = {**DOCUMENT, "content": "Vattenfall " * (CHUNK_TEXT_MAX_BYTES // 4)}
    document_id = (
        await db_client.post(f"/clients/{client_id}/documents", json=long_document)
    ).json()["id"]

    await create_outbox_worker(db_pool, search_client).run_once()
    ordinals = await chunk_ordinals(search_client, document_id)
    assert len(ordinals) > 1
    assert ordinals == list(range(len(ordinals)))

    # Shorter content: re-indexing must not leave the old trailing chunks behind.
    await db_pool.execute(
        "UPDATE documents SET content = 'Vattenfall' WHERE id = $1", uuid.UUID(document_id)
    )
    await index_document(db_pool, search_client, uuid.UUID(document_id))
    assert await chunk_ordinals(search_client, document_id) == [0]

    await db_pool.execute("DELETE FROM documents WHERE id = $1", uuid.UUID(document_id))
    await index_document(db_pool, search_client, uuid.UUID(document_id))
    assert await chunk_ordinals(search_client, document_id) == []


@pytest.mark.integration
async def test_deleting_a_client_deletes_its_documents(
    db_client: AsyncClient, db_pool: DbPool, client_id: str
) -> None:
    await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)

    await db_pool.execute("DELETE FROM clients WHERE id = $1", uuid.UUID(client_id))

    assert await db_pool.fetchval("SELECT count(*) FROM documents") == 0
