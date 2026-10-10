"""Create the search indexes and apply their settings: ``python -m nevis.search.setup``.

Runs as the one-shot compose service ``search-setup``, before the api starts. Safe to re-run:
settings updates are partial and Meilisearch skips the ones that change nothing.
"""

import asyncio
import logging

from meilisearch_python_sdk import AsyncClient

from nevis.config import SearchSettings
from nevis.search.client import create_search_client
from nevis.search.indexes import EXPERIMENTAL_FEATURES, INDEX_SETTINGS, PRIMARY_KEY

logger = logging.getLogger(__name__)

# The first run downloads the embedding model and embeds whatever is already indexed.
SETTINGS_TASK_TIMEOUT_MS = 10 * 60 * 1000


async def apply_index_settings(search_client: AsyncClient) -> None:
    await search_client.update_experimental_features(EXPERIMENTAL_FEATURES)
    for uid, settings in INDEX_SETTINGS.items():
        index = await search_client.get_or_create_index(uid, PRIMARY_KEY)
        task = await index.update_settings(settings)
        await search_client.wait_for_task(
            task.task_uid, timeout_in_ms=SETTINGS_TASK_TIMEOUT_MS, raise_for_status=True
        )
        logger.info("Applied settings to search index %r", uid)


async def _main() -> None:
    async with create_search_client(SearchSettings()) as search_client:
        await apply_index_settings(search_client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per task poll otherwise
    asyncio.run(_main())
