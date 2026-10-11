"""What the write paths store for search: client search_text, document tsv and chunks."""

import datetime
import uuid
from unittest.mock import DEFAULT, NonCallableMagicMock

import pytest
from httpx2 import AsyncClient

from nevis.db import DbPool
from nevis.db_models import Document
from nevis.documents.columns import DOCUMENT_COLUMNS
from nevis.embeddings import EMBEDDING_DIMENSIONS, Embedder
from nevis.search.chunking import CHUNK_TEXT_MAX_BYTES, chunk_text

CLIENT = {"first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com"}
DOCUMENT = {
    "title": "Utility bill March",
    "content": "Electricity bill from Vattenfall for 12 Lake Road, Zurich. Amount due CHF 84.20.",
}
LONG_DOCUMENT = {
    "title": "Long statement",
    "content": " ".join(f"word{number}" for number in range(1000)),
}
SOME_ID = "8a0d5ff5-8f43-4a43-9d5d-3b3a5c4f5e11"


@pytest.fixture
async def client_id(db_client: AsyncClient) -> str:
    client_id: str = (await db_client.post("/clients", json=CLIENT)).json()["id"]
    return client_id


async def post_document(db_client: AsyncClient, client_id: str, payload: dict[str, str]) -> str:
    response = await db_client.post(f"/clients/{client_id}/documents", json=payload)
    assert response.status_code == 201
    document_id: str = response.json()["id"]
    return document_id


def created_row(client_id: str) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "client_id": uuid.UUID(client_id),
        **LONG_DOCUMENT,
        "summary": None,
        "summary_attempts": 0,
        "summary_locked_until": None,
        "created_at": datetime.datetime.now(datetime.UTC),
    }


@pytest.mark.unit
async def test_create_document_checks_the_client_then_embeds_title_prefixed_chunks_then_writes(
    client: AsyncClient,
    fake_pool: NonCallableMagicMock,
    fake_connection: NonCallableMagicMock,
    fake_embedder: NonCallableMagicMock,
) -> None:
    calls: list[str] = []

    async def client_exists(*_: object) -> bool:
        calls.append("exists")
        return True

    async def embed(texts: list[str]) -> list[list[float]]:
        calls.append("embed")
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    def acquire(*_: object) -> object:
        calls.append("acquire")
        return DEFAULT

    fake_pool.fetchval.side_effect = client_exists
    fake_embedder.embed_passages.side_effect = embed
    fake_pool.acquire.side_effect = acquire
    fake_connection.fetchrow.return_value = created_row(SOME_ID)

    response = await client.post(f"/clients/{SOME_ID}/documents", json=LONG_DOCUMENT)

    chunks = chunk_text(LONG_DOCUMENT["content"])
    assert response.status_code == 201
    assert len(chunks) > 1
    assert calls == ["exists", "embed", "acquire"]
    fake_embedder.embed_passages.assert_awaited_once_with(
        [f"{LONG_DOCUMENT['title']}: {chunk}" for chunk in chunks]
    )
    fake_connection.executemany.assert_awaited_once()


@pytest.mark.unit
async def test_create_document_notifies_the_summary_worker_in_its_transaction(
    client: AsyncClient,
    fake_pool: NonCallableMagicMock,
    fake_connection: NonCallableMagicMock,
    fake_embedder: NonCallableMagicMock,
) -> None:
    fake_pool.fetchval.return_value = True
    fake_embedder.embed_passages.side_effect = _vectors
    row = created_row(SOME_ID)
    fake_connection.fetchrow.return_value = row

    await client.post(f"/clients/{SOME_ID}/documents", json=LONG_DOCUMENT)

    fake_connection.execute.assert_any_await(
        "SELECT pg_notify($1, $2)", "documents_to_summarize", str(row["id"])
    )
    fake_connection.transaction.assert_called_once()


async def _vectors(texts: list[str]) -> list[list[float]]:
    return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]


@pytest.mark.unit
async def test_create_document_for_unknown_client_returns_404_without_embedding(
    client: AsyncClient, fake_pool: NonCallableMagicMock, fake_embedder: NonCallableMagicMock
) -> None:
    fake_pool.fetchval.return_value = False

    response = await client.post(f"/clients/{SOME_ID}/documents", json=LONG_DOCUMENT)

    assert response.status_code == 404
    fake_embedder.embed_passages.assert_not_awaited()
    fake_pool.acquire.assert_not_called()


@pytest.mark.unit
async def test_create_document_writes_nothing_when_embedding_fails(
    client: AsyncClient,
    fake_pool: NonCallableMagicMock,
    fake_connection: NonCallableMagicMock,
    fake_embedder: NonCallableMagicMock,
) -> None:
    fake_pool.fetchval.return_value = True
    fake_embedder.embed_passages.side_effect = RuntimeError("model failed")

    with pytest.raises(RuntimeError, match="model failed"):
        await client.post(f"/clients/{SOME_ID}/documents", json=LONG_DOCUMENT)

    fake_pool.acquire.assert_not_called()
    fake_connection.execute.assert_not_called()
    fake_connection.executemany.assert_not_called()


@pytest.mark.unit
def test_document_columns_match_the_document_model() -> None:
    assert DOCUMENT_COLUMNS.split(", ") == list(Document.model_fields)


@pytest.mark.integration
async def test_create_document_stores_chunks_in_order_with_embeddings(
    db_client: AsyncClient, db_pool: DbPool, client_id: str
) -> None:
    document_id = await post_document(db_client, client_id, LONG_DOCUMENT)

    rows = await db_pool.fetch(
        "SELECT ordinal, text, embedding FROM document_chunks WHERE document_id = $1 "
        "ORDER BY ordinal",
        uuid.UUID(document_id),
    )

    expected = chunk_text(LONG_DOCUMENT["content"])
    assert len(expected) > 1
    assert [row["ordinal"] for row in rows] == list(range(len(expected)))
    assert [row["text"] for row in rows] == expected  # plain text, without the title
    assert all(len(row["embedding"]) == EMBEDDING_DIMENSIONS for row in rows)
    assert all(len(row["text"].encode()) <= CHUNK_TEXT_MAX_BYTES for row in rows)


@pytest.mark.integration
async def test_chunk_embeddings_are_those_of_title_and_text(
    db_client: AsyncClient, db_pool: DbPool, client_id: str, real_embedder: Embedder
) -> None:
    document_id = await post_document(db_client, client_id, DOCUMENT)

    stored = await db_pool.fetchval(
        "SELECT embedding FROM document_chunks WHERE document_id = $1", uuid.UUID(document_id)
    )

    [expected] = await real_embedder.embed_passages([f"{DOCUMENT['title']}: {DOCUMENT['content']}"])
    assert stored == pytest.approx(expected, abs=1e-5)


@pytest.mark.integration
async def test_create_document_populates_the_full_text_vector(
    db_client: AsyncClient, db_pool: DbPool, client_id: str
) -> None:
    document_id = await post_document(db_client, client_id, DOCUMENT)

    matches = await db_pool.fetchval(
        "SELECT count(*) FROM documents WHERE id = $1 "
        "AND tsv @@ websearch_to_tsquery('english', 'vattenfall utility')",
        uuid.UUID(document_id),
    )

    assert matches == 1


@pytest.mark.integration
async def test_create_document_for_unknown_client_stores_no_document_and_no_chunks(
    db_client: AsyncClient, db_pool: DbPool
) -> None:
    response = await db_client.post(f"/clients/{SOME_ID}/documents", json=DOCUMENT)

    assert response.status_code == 404
    assert await db_pool.fetchval("SELECT count(*) FROM documents") == 0
    assert await db_pool.fetchval("SELECT count(*) FROM document_chunks") == 0


@pytest.mark.integration
async def test_deleting_a_client_deletes_the_chunks_of_its_documents(
    db_client: AsyncClient, db_pool: DbPool, client_id: str
) -> None:
    await post_document(db_client, client_id, DOCUMENT)
    assert await db_pool.fetchval("SELECT count(*) FROM document_chunks") == 1

    await db_pool.execute("DELETE FROM clients WHERE id = $1", uuid.UUID(client_id))

    assert await db_pool.fetchval("SELECT count(*) FROM document_chunks") == 0


@pytest.mark.integration
async def test_create_client_stores_normalized_search_text(
    db_client: AsyncClient, db_pool: DbPool
) -> None:
    payload = {
        "first_name": "José",
        "last_name": "Ñúñez",
        "email": "Jose.Nunez@Example.com",
        "description": "Prefers  Zürich",
    }

    created = (await db_client.post("/clients", json=payload)).json()

    search_text = await db_pool.fetchval(
        "SELECT search_text FROM clients WHERE id = $1", uuid.UUID(created["id"])
    )
    assert search_text == "jose nunez jose.nunez@example.com prefers zurich"
    assert set(created) == {"id", *payload, "social_links"}  # API response unchanged
