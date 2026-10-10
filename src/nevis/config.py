from pydantic import HttpUrl, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ASYNCPG_SCHEMES = frozenset({"postgresql", "postgres"})


class SearchSettings(BaseSettings):
    """Meilisearch connection, read from ``NEVIS_*`` environment variables.

    On its own for ``nevis.search.setup``, which needs no database.
    """

    model_config = SettingsConfigDict(env_prefix="NEVIS_", frozen=True, extra="ignore")

    meilisearch_url: HttpUrl
    meilisearch_api_key: SecretStr


class Settings(SearchSettings):
    """Runtime configuration of the app, read from ``NEVIS_*`` environment variables."""

    database_url: PostgresDsn

    @field_validator("database_url")
    @classmethod
    def _require_plain_scheme(cls, url: PostgresDsn) -> PostgresDsn:
        if url.scheme not in ASYNCPG_SCHEMES:
            msg = f"use a plain postgresql:// URL for asyncpg, not {url.scheme}://"
            raise ValueError(msg)
        return url
