import asyncio
import uuid
from unittest.mock import NonCallableMagicMock, create_autospec

import pytest
from httpx2 import AsyncClient

from nevis.db import DbPool
from nevis.embeddings import Embedder
from nevis.search.reembed import main, reembed

DOCUMENT = {"title": "Utility bill", "content": "Electricity bill from Vattenfall, Zurich."}


async def create_client_and_document(db_client: AsyncClient) -> tuple[str, str]:
    client_id = (
        await db_client.post(
            "/clients", json={"first_name": "José", "last_name": "Doe", "email": "j@x.com"}
        )
    ).json()["id"]
    document_id = (await db_client.post(f"/clients/{client_id}/documents", json=DOCUMENT)).json()[
        "id"
    ]
    return client_id, document_id


@pytest.mark.integration
async def test_reembed_fills_missing_chunks_and_blank_search_text(
    db_client: AsyncClient, db_pool: DbPool, real_embedder: Embedder
) -> None:
    client_id, document_id = await create_client_and_document(db_client)
    await db_pool.execute("DELETE FROM document_chunks")
    await db_pool.execute("UPDATE clients SET search_text = ''")

    counts = await reembed(db_pool, real_embedder)

    assert (counts.documents, counts.chunks, counts.clients) == (1, 1, 1)
    chunk = await db_pool.fetchrow(
        "SELECT ordinal, text FROM document_chunks WHERE document_id = $1", uuid.UUID(document_id)
    )
    assert chunk is not None
    assert (chunk["ordinal"], chunk["text"]) == (0, DOCUMENT["content"])
    search_text = await db_pool.fetchval(
        "SELECT search_text FROM clients WHERE id = $1", uuid.UUID(client_id)
    )
    assert search_text == "jose doe j@x.com"


@pytest.mark.integration
async def test_reembed_is_idempotent(
    db_client: AsyncClient, db_pool: DbPool, real_embedder: Embedder
) -> None:
    await create_client_and_document(db_client)
    await db_pool.execute("DELETE FROM document_chunks")

    await reembed(db_pool, real_embedder)
    second = await reembed(db_pool, real_embedder)

    assert (second.documents, second.chunks) == (0, 0)
    assert await db_pool.fetchval("SELECT count(*) FROM document_chunks") == 1


@pytest.mark.integration
async def test_reembed_all_replaces_existing_chunks(
    db_client: AsyncClient, db_pool: DbPool, real_embedder: Embedder
) -> None:
    _, document_id = await create_client_and_document(db_client)
    await db_pool.execute("UPDATE document_chunks SET text = 'stale', ordinal = 7")

    counts = await reembed(db_pool, real_embedder, all_documents=True)

    assert (counts.documents, counts.chunks) == (1, 1)
    rows = await db_pool.fetch(
        "SELECT ordinal, text FROM document_chunks WHERE document_id = $1", uuid.UUID(document_id)
    )
    assert [(row["ordinal"], row["text"]) for row in rows] == [(0, DOCUMENT["content"])]


@pytest.mark.integration
async def test_reembed_skips_a_document_deleted_while_it_is_embedded(
    db_client: AsyncClient, db_pool: DbPool, real_embedder: Embedder
) -> None:
    _, document_id = await create_client_and_document(db_client)
    await db_pool.execute("DELETE FROM document_chunks")

    async def delete_then_embed(texts: list[str]) -> list[list[float]]:
        await db_pool.execute("DELETE FROM documents WHERE id = $1", uuid.UUID(document_id))
        return await real_embedder.embed_passages(texts)

    embedder: NonCallableMagicMock = create_autospec(Embedder, instance=True)
    embedder.embed_passages.side_effect = delete_then_embed

    counts = await reembed(db_pool, embedder)

    assert (counts.documents, counts.chunks) == (0, 0)
    assert await db_pool.fetchval("SELECT count(*) FROM document_chunks") == 0


@pytest.mark.integration
async def test_concurrent_reembed_runs_write_each_document_once(
    db_client: AsyncClient, db_pool: DbPool, real_embedder: Embedder
) -> None:
    client_id, _ = await create_client_and_document(db_client)
    await db_client.post(
        f"/clients/{client_id}/documents", json={**DOCUMENT, "title": "Second document"}
    )
    await db_pool.execute("DELETE FROM document_chunks")

    first, second = await asyncio.gather(
        reembed(db_pool, real_embedder), reembed(db_pool, real_embedder)
    )

    assert first.documents + second.documents == 2
    assert await db_pool.fetchval("SELECT count(*) FROM document_chunks") == 2


@pytest.mark.integration
async def test_reembed_pages_through_documents_and_clients(
    db_client: AsyncClient,
    db_pool: DbPool,
    real_embedder: Embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nevis.search.reembed.PAGE_SIZE", 2)
    client_id, _ = await create_client_and_document(db_client)
    for number in range(4):
        await db_client.post(
            f"/clients/{client_id}/documents", json={**DOCUMENT, "title": f"Doc {number}"}
        )
        await db_client.post(
            "/clients",
            json={"first_name": "Ana", "last_name": str(number), "email": f"a{number}@x.com"},
        )
    await db_pool.execute("DELETE FROM document_chunks")
    await db_pool.execute("UPDATE clients SET search_text = ''")

    counts = await reembed(db_pool, real_embedder)

    assert (counts.documents, counts.chunks, counts.clients) == (5, 5, 5)
    assert await db_pool.fetchval("SELECT count(*) FROM clients WHERE search_text = ''") == 0


@pytest.mark.unit
def test_main_passes_the_all_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[bool] = []

    async def fake_main(all_documents: bool) -> None:
        received.append(all_documents)

    monkeypatch.setattr("nevis.search.reembed._main", fake_main)

    main([])
    main(["--all"])

    assert received == [False, True]
