from pathlib import Path
from typing import Self

from pydantic import HttpUrl, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ASYNCPG_SCHEMES = frozenset({"postgresql", "postgres"})


class _NevisSettings(BaseSettings):
    # env_ignore_empty: docker-compose passes variables that aren't set in .env through as empty
    # strings; those mean "unset", so the field's default applies.
    model_config = SettingsConfigDict(
        env_prefix="NEVIS_", env_ignore_empty=True, frozen=True, extra="ignore"
    )


class EmbeddingSettings(_NevisSettings):
    """Local embedding and reranking models, read from ``NEVIS_*`` environment variables.

    On its own for ``python -m nevis.embeddings`` (the image build), which needs no database.
    """

    # Where fastembed keeps the models. Unset: its default (a temp directory).
    model_cache_dir: Path | None = None
    # The cross-encoder reranker is optional; its model is loaded only when this is on.
    search_rerank: bool = False


class DatabaseSettings(_NevisSettings):
    """The Postgres connection, read from ``NEVIS_*`` environment variables.

    On its own for the summary worker, which needs no search or embedding settings.
    """

    database_url: PostgresDsn

    @field_validator("database_url")
    @classmethod
    def _require_plain_scheme(cls, url: PostgresDsn) -> PostgresDsn:
        if url.scheme not in ASYNCPG_SCHEMES:
            msg = f"use a plain postgresql:// URL for asyncpg, not {url.scheme}://"
            raise ValueError(msg)
        return url


class SummarySettings(_NevisSettings):
    """The optional local summarization model, read from ``NEVIS_*`` environment variables."""

    # A locally running model with an OpenAI-compatible API (e.g. Ollama's
    # http://localhost:11434/v1) for document summaries. Unset: summaries stay empty.
    summary_api_url: HttpUrl | None = None
    summary_model: str | None = None

    @model_validator(mode="after")
    def _require_both_summary_settings(self) -> Self:
        if (self.summary_api_url is None) != (self.summary_model is None):
            msg = "set both NEVIS_SUMMARY_API_URL and NEVIS_SUMMARY_MODEL, or neither"
            raise ValueError(msg)
        return self


class Settings(EmbeddingSettings, DatabaseSettings):
    """Runtime configuration of the api, read from ``NEVIS_*`` environment variables.

    No summary settings: only the worker summarizes, so a half-set ``NEVIS_SUMMARY_*`` pair is
    ignored here instead of stopping the api.
    """


class WorkerSettings(DatabaseSettings, SummarySettings):
    """Configuration of the summary worker: no embedding models."""
