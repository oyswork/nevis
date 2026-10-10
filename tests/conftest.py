import asyncio
from collections.abc import Iterator
from urllib.parse import urlsplit

import asyncpg
import pytest
from fastapi.testclient import TestClient
from pydantic import PostgresDsn

from nevis.config import Settings
from nevis.main import create_app

# Served by docker-compose.test.yml; integration tests never start infrastructure themselves.
TEST_DATABASE_URL = "postgresql://nevis:nevis@localhost:55432/nevis_test"
START_TEST_STACK = (
    "docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --wait migrate"
)
CONNECT_TIMEOUT_SECONDS = 3


def pytest_collection_finish(session: pytest.Session) -> None:
    """Abort before running anything if integration tests are selected but infra isn't up."""
    if not any(item.get_closest_marker("integration") for item in session.items):
        return
    problem = asyncio.run(_test_database_problem(TEST_DATABASE_URL))
    if problem is not None:
        pytest.exit(
            f"Integration test infrastructure is not ready: {problem}\n"
            f"Start it with:  {START_TEST_STACK}\n"
            "Or run only unit tests:  uv run pytest -m unit",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )


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


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=PostgresDsn("postgresql://test:test@localhost:5432/test"))


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    return Settings(database_url=PostgresDsn(TEST_DATABASE_URL))


@pytest.fixture
def db_client(db_settings: Settings) -> Iterator[TestClient]:
    asyncio.run(_execute(str(db_settings.database_url), "TRUNCATE clients CASCADE"))
    with TestClient(create_app(db_settings)) as test_client:
        yield test_client


async def _execute(url: str, statement: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()
