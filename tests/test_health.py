import pytest
from httpx2 import AsyncClient
from pydantic import PostgresDsn, ValidationError

from nevis.config import Settings


@pytest.mark.unit
async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.unit
async def test_health_does_not_depend_on_database(unreachable_db_client: AsyncClient) -> None:
    # Liveness: a database outage must not make the orchestrator restart healthy app instances.
    response = await unreachable_db_client.get("/health")

    assert response.status_code == 200


@pytest.mark.unit
async def test_readiness_returns_503_when_database_is_unreachable(
    unreachable_db_client: AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


@pytest.mark.integration
async def test_readiness_returns_ok_when_database_is_reachable(db_client: AsyncClient) -> None:
    response = await db_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.unit
def test_settings_read_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEVIS_DATABASE_URL", "postgresql://u:p@db:5432/nevis")

    settings = Settings()

    assert str(settings.database_url) == "postgresql://u:p@db:5432/nevis"


@pytest.mark.unit
@pytest.mark.parametrize("scheme", ["postgresql+asyncpg", "postgresql+psycopg"])
def test_settings_reject_driver_qualified_database_url(scheme: str) -> None:
    # asyncpg only understands plain postgresql:// / postgres:// DSNs.
    with pytest.raises(ValidationError, match="plain postgresql://"):
        Settings(database_url=PostgresDsn(f"{scheme}://u:p@db:5432/nevis"))
