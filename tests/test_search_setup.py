from collections.abc import AsyncGenerator

import pytest
from meilisearch_python_sdk import AsyncClient as SearchClient

from nevis.config import Settings
from nevis.search.client import create_search_client
from nevis.search.indexes import (
    CLIENTS_INDEX,
    DOCUMENT_CHUNKS_INDEX,
    DOCUMENT_EMBEDDER,
    EMBEDDING_MODEL,
    INDEX_SETTINGS,
)
from nevis.search.setup import apply_index_settings


@pytest.fixture
async def search_client(db_settings: Settings) -> AsyncGenerator[SearchClient]:
    async with create_search_client(db_settings) as client:
        yield client


@pytest.mark.integration
async def test_setup_applies_index_settings_and_can_run_again(search_client: SearchClient) -> None:
    # The test stack's search-setup already ran once; running it again must change nothing.
    await apply_index_settings(search_client)

    features = await search_client.get_experimental_features()
    clients = await (await search_client.get_index(CLIENTS_INDEX)).get_settings()
    chunks = await (await search_client.get_index(DOCUMENT_CHUNKS_INDEX)).get_settings()

    assert features["containsFilter"] is True
    assert clients.searchable_attributes == INDEX_SETTINGS[CLIENTS_INDEX].searchable_attributes
    assert clients.filterable_attributes == INDEX_SETTINGS[CLIENTS_INDEX].filterable_attributes
    assert chunks.distinct_attribute == "document_id"
    assert chunks.embedders is not None
    assert chunks.embedders[DOCUMENT_EMBEDDER].model_dump()["model"] == EMBEDDING_MODEL
