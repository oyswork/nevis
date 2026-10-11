"""The model behind the stored vectors: recorded on first use, checked by the api and reembed."""

from collections.abc import AsyncGenerator

import pytest
from httpx2 import AsyncClient

from nevis.config import Settings
from nevis.db import DbPool
from nevis.documents.chunks import (
    EmbeddingModelMismatchError,
    check_embedding_model,
    set_embedding_model,
)
from nevis.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, Embedder
from nevis.main import create_app
from nevis.search.reembed import reembed

SELECT_MODEL = "SELECT name FROM embedding_model"


@pytest.fixture
async def other_model(db_client: AsyncClient, db_pool: DbPool) -> AsyncGenerator[None]:
    """Stored vectors that claim another model; restored afterwards (the database is shared).

    Depends on `db_client` for its side effect: an emptied database, so reembed has no work.
    """
    await db_pool.execute(
        "INSERT INTO embedding_model (name) VALUES ('other/model') "
        "ON CONFLICT (singleton) DO UPDATE SET name = excluded.name"
    )
    try:
        yield
    finally:
        await set_embedding_model(db_pool)


@pytest.mark.integration
async def test_the_first_check_records_the_running_model(db_pool: DbPool) -> None:
    await db_pool.execute("DELETE FROM embedding_model")

    await check_embedding_model(db_pool)

    assert await db_pool.fetchval(SELECT_MODEL) == EMBEDDING_MODEL


@pytest.mark.integration
@pytest.mark.usefixtures("other_model")
async def test_the_api_refuses_to_start_on_vectors_from_another_model(
    db_settings: Settings, real_embedder: Embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create_session_embedder(_: Settings) -> Embedder:
        return real_embedder

    monkeypatch.setattr("nevis.main.create_embedder", create_session_embedder)
    app = create_app(db_settings)

    with pytest.raises(EmbeddingModelMismatchError, match="other/model"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.integration
@pytest.mark.usefixtures("other_model")
async def test_reembed_without_all_refuses_vectors_from_another_model(
    db_pool: DbPool, real_embedder: Embedder
) -> None:
    with pytest.raises(EmbeddingModelMismatchError):
        await reembed(db_pool, real_embedder)


@pytest.mark.integration
@pytest.mark.usefixtures("other_model")
async def test_reembed_all_records_the_running_model(
    db_pool: DbPool, real_embedder: Embedder
) -> None:
    await reembed(db_pool, real_embedder, all_documents=True)

    assert await db_pool.fetchval(SELECT_MODEL) == EMBEDDING_MODEL


@pytest.mark.integration
async def test_the_vector_column_has_the_model_dimensions(db_pool: DbPool) -> None:
    # For pgvector's `vector(n)`, the column's type modifier is n.
    typmod = await db_pool.fetchval(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
    )

    assert typmod == EMBEDDING_DIMENSIONS
