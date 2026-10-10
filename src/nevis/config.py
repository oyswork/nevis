from pydantic import PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ASYNCPG_SCHEMES = frozenset({"postgresql", "postgres"})


class Settings(BaseSettings):
    """Runtime configuration, read from ``NEVIS_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="NEVIS_", frozen=True, extra="ignore")

    database_url: PostgresDsn

    @field_validator("database_url")
    @classmethod
    def _require_plain_scheme(cls, url: PostgresDsn) -> PostgresDsn:
        if url.scheme not in ASYNCPG_SCHEMES:
            msg = f"use a plain postgresql:// URL for asyncpg, not {url.scheme}://"
            raise ValueError(msg)
        return url
