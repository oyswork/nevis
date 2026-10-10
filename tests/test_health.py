import pytest
from fastapi.testclient import TestClient
from pydantic import PostgresDsn, ValidationError

from nevis.config import Settings


@pytest.mark.unit
def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

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
