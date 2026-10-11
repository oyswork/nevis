import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import NonCallableMagicMock, create_autospec
from urllib.parse import urlsplit

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from pydantic import PostgresDsn

from nevis.config import EmbeddingSettings, Settings
from nevis.db import DbPool, create_pool
from nevis.embeddings import Embedder, create_embedder
from nevis.main import create_app

# Served by docker-compose.test.yml; integration tests never start infrastructure themselves.
# The default is the port the test stack publishes on the host; the `tests` container, which
# reaches the database over the compose network, overrides it.
TEST_DATABASE_URL_ENV = "NEVIS_TEST_DATABASE_URL"
DEFAULT_TEST_DATABASE_URL = "postgresql://nevis:nevis@localhost:55432/nevis_test"
CONNECT_TIMEOUT_SECONDS = 3


def pytest_collection_finish(session: pytest.Session) -> None:
    """Abort before running anything if integration tests are selected but infra isn't up."""
    if not any(item.get_closest_marker("integration") for item in session.items):
        return
    # Collection hooks are synchronous and run before any test event loop exists.
    problem = asyncio.run(_test_database_problem(_test_database_url()))
    if problem is not None:
        pytest.exit(
            f"Integration test infrastructure is not ready: {problem}\n"
            "Start it with:  just test-infra  (or run everything in Docker:  just test)\n"
            "Or run only unit tests:  uv run pytest -m unit",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )


def _test_database_url() -> str:
    return os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL)


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
    # Never connected to: the `client` fixture replaces the pool.
    return Settings(
        database_url=PostgresDsn("postgresql://unused@unused.invalid/unused"),
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
def fake_embedder() -> NonCallableMagicMock:
    """The embedder behind `client`; configure its methods per test."""
    embedder: NonCallableMagicMock = create_autospec(Embedder, instance=True)
    return embedder


@pytest.fixture
async def client(
    settings: Settings,
    fake_pool: NonCallableMagicMock,
    fake_embedder: NonCallableMagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient]:
    """App client for unit tests: no infrastructure, the lifespan gets the fakes instead."""

    async def create_fake_pool(_: Settings) -> NonCallableMagicMock:
        return fake_pool

    async def create_fake_embedder(_: Settings) -> NonCallableMagicMock:
        return fake_embedder

    async def skip_embedding_model_check(_: NonCallableMagicMock) -> None:
        return None

    monkeypatch.setattr("nevis.main.create_pool", create_fake_pool)
    monkeypatch.setattr("nevis.main.create_embedder", create_fake_embedder)
    monkeypatch.setattr("nevis.main.check_embedding_model", skip_embedding_model_check)
    _disable_reranker(monkeypatch)
    async with _serve(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    return Settings(
        database_url=PostgresDsn(_test_database_url()),
    )


# One real model for the whole session: loading it takes seconds. It binds to no event loop.
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def real_embedder() -> Embedder:
    return await create_embedder(EmbeddingSettings())


@pytest.fixture
async def db_pool(db_settings: Settings) -> AsyncGenerator[DbPool]:
    """A pool on the test database, with the app's connection setup (the vector codec)."""
    pool = await create_pool(db_settings)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_client(
    db_settings: Settings,
    real_embedder: Embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient]:
    """App client on the real test infrastructure, emptied first."""
    await _execute(str(db_settings.database_url), "TRUNCATE clients CASCADE")

    async def create_session_embedder(_: Settings) -> Embedder:
        return real_embedder

    monkeypatch.setattr("nevis.main.create_embedder", create_session_embedder)
    _disable_reranker(monkeypatch)
    async with _serve(create_app(db_settings)) as test_client:
        yield test_client


def _disable_reranker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def create_no_reranker(_: Settings) -> None:
        return None

    monkeypatch.setattr("nevis.main.create_reranker", create_no_reranker)


async def _execute(url: str, statement: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()
