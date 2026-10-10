import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import NonCallableMagicMock, create_autospec
from urllib.parse import urlsplit

import asyncpg
import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from meilisearch_python_sdk import AsyncClient as SearchClient
from meilisearch_python_sdk.errors import MeilisearchApiError, MeilisearchCommunicationError
from pydantic import HttpUrl, PostgresDsn, SecretStr

from nevis.config import Settings
from nevis.main import create_app
from nevis.outbox.worker import OutboxWorker
from nevis.search.client import create_search_client
from nevis.search.indexes import CLIENTS_INDEX, DOCUMENT_CHUNKS_INDEX

# Served by docker-compose.test.yml; integration tests never start infrastructure themselves.
# The default is the port the test stack publishes on the host; the `tests` container, which
# reaches the database over the compose network, overrides it.
TEST_DATABASE_URL_ENV = "NEVIS_TEST_DATABASE_URL"
DEFAULT_TEST_DATABASE_URL = "postgresql://nevis:nevis@localhost:55432/nevis_test"
TEST_MEILISEARCH_URL_ENV = "NEVIS_TEST_MEILISEARCH_URL"
DEFAULT_TEST_MEILISEARCH_URL = "http://localhost:57700"
# The test stack's master key (docker-compose.test.yml); it guards nothing but throwaway data.
TEST_MEILISEARCH_API_KEY = "nevis-test-master-key"
CONNECT_TIMEOUT_SECONDS = 3


def pytest_collection_finish(session: pytest.Session) -> None:
    """Abort before running anything if integration tests are selected but infra isn't up."""
    if not any(item.get_closest_marker("integration") for item in session.items):
        return
    # Collection hooks are synchronous and run before any test event loop exists.
    problem = asyncio.run(_test_database_problem(_test_database_url())) or asyncio.run(
        _test_search_problem(_test_meilisearch_url())
    )
    if problem is not None:
        pytest.exit(
            f"Integration test infrastructure is not ready: {problem}\n"
            "Start it with:  just test-infra  (or run everything in Docker:  just test)\n"
            "Or run only unit tests:  uv run pytest -m unit",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL)


def _test_meilisearch_url() -> str:
    return os.environ.get(TEST_MEILISEARCH_URL_ENV, DEFAULT_TEST_MEILISEARCH_URL)


async def _test_database_problem(url: str) -> str | None:
    parts = urlsplit(url)
    location = f"{parts.hostname}:{parts.port}{parts.path}"  # no credentials in messages
    try:
        connection = await asyncpg.connect(url, timeout=CONNECT_TIMEOUT_SECONDS)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        return f"cannot connect to {location} ({type(exc).__name__}: {exc})"
    try:
        database = await connection.fetchval("SELECT current_database()")
        if not str(database).endswith("_test"):
            # The tests truncate tables; never let them near a dev or prod database.
            return f"refusing to use database {database!r}: its name must end with _test"
        if await connection.fetchval("SELECT to_regclass('public.clients')") is None:
            return f"schema not applied at {location} (no clients table)"
    finally:
        await connection.close()
    return None


async def _test_search_problem(url: str) -> str | None:
    async with SearchClient(
        url, TEST_MEILISEARCH_API_KEY, timeout=CONNECT_TIMEOUT_SECONDS
    ) as client:
        try:
            await client.get_index(CLIENTS_INDEX)
        except MeilisearchCommunicationError as exc:
            return f"cannot connect to Meilisearch at {url} ({exc.message})"
        except MeilisearchApiError as exc:
            return f"search indexes not set up at {url} ({exc.code})"
    return None


@asynccontextmanager
async def _serve(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    # ASGITransport doesn't run the lifespan, so enter it here: it creates and closes the pool.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
def settings() -> Settings:
    # Never connected to: the `client` fixture replaces the pool and the search client.
    return Settings(
        database_url=PostgresDsn("postgresql://unused@unused.invalid/unused"),
        meilisearch_url=HttpUrl("http://unused.invalid"),
        meilisearch_api_key=SecretStr("unused-unused-unused"),
    )


@pytest.fixture
def fake_pool() -> NonCallableMagicMock:
    """The asyncpg pool behind `client`; configure its methods (e.g. side_effect) per test."""
    pool: NonCallableMagicMock = create_autospec(asyncpg.Pool, instance=True)
    return pool


@pytest.fixture
def fake_connection(fake_pool: NonCallableMagicMock) -> NonCallableMagicMock:
    """The connection `async with fake_pool.acquire()` yields, for code that runs transactions."""
    connection: NonCallableMagicMock = create_autospec(asyncpg.Connection, instance=True)
    fake_pool.acquire.return_value.__aenter__.return_value = connection
    return connection


@pytest.fixture
def fake_outbox_worker() -> NonCallableMagicMock:
    """Replaces the app's background worker: tests run jobs explicitly, never in a loop."""
    outbox_worker: NonCallableMagicMock = create_autospec(OutboxWorker, instance=True)
    return outbox_worker


@pytest.fixture
def fake_search_client() -> NonCallableMagicMock:
    """The Meilisearch client behind `client`; configure its methods per test."""
    search_client: NonCallableMagicMock = create_autospec(SearchClient, instance=True)
    return search_client


@pytest.fixture
async def client(
    settings: Settings,
    fake_pool: NonCallableMagicMock,
    fake_search_client: NonCallableMagicMock,
    fake_outbox_worker: NonCallableMagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient]:
    """App client for unit tests: no infrastructure, the lifespan gets the fakes instead."""

    async def create_fake_pool(_: Settings) -> NonCallableMagicMock:
        return fake_pool

    def create_fake_search_client(_: Settings) -> NonCallableMagicMock:
        return fake_search_client

    monkeypatch.setattr("nevis.main.create_pool", create_fake_pool)
    monkeypatch.setattr("nevis.main.create_search_client", create_fake_search_client)
    _replace_outbox_worker(monkeypatch, fake_outbox_worker)
    async with _serve(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    return Settings(
        database_url=PostgresDsn(_test_database_url()),
        meilisearch_url=HttpUrl(_test_meilisearch_url()),
        meilisearch_api_key=SecretStr(TEST_MEILISEARCH_API_KEY),
    )


@pytest.fixture
async def db_client(
    db_settings: Settings, fake_outbox_worker: NonCallableMagicMock, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient]:
    """App client on the real test infrastructure, emptied first. Jobs run only when a test
    runs them."""
    await _execute(str(db_settings.database_url), "TRUNCATE clients, outbox CASCADE")
    await _empty_search_indexes(db_settings)
    _replace_outbox_worker(monkeypatch, fake_outbox_worker)
    async with _serve(create_app(db_settings)) as test_client:
        yield test_client


def _replace_outbox_worker(monkeypatch: pytest.MonkeyPatch, worker: NonCallableMagicMock) -> None:
    def create_fake_outbox_worker(*_: object) -> NonCallableMagicMock:
        return worker

    monkeypatch.setattr("nevis.main.create_outbox_worker", create_fake_outbox_worker)


async def _empty_search_indexes(settings: Settings) -> None:
    async with create_search_client(settings) as search_client:
        for uid in (CLIENTS_INDEX, DOCUMENT_CHUNKS_INDEX):
            task = await search_client.index(uid).delete_all_documents()
            await search_client.wait_for_task(task.task_uid, raise_for_status=True)


async def _execute(url: str, statement: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()
