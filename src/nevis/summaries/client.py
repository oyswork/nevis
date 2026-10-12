"""Summaries from a model over the OpenAI-compatible chat API.

Local servers (Ollama, LM Studio, llama.cpp, vLLM) and most hosted providers serve it; hosted ones
take the API key as a Bearer token.
"""

from typing import Protocol

from httpx2 import AsyncClient
from pydantic import BaseModel

from nevis.config import SummarySettings

# Fits the context window of small local models and bounds what a hosted call costs; longer
# documents are summarized from the start.
CONTENT_MAX_CHARS = 12_000
# Models can be slow, local ones especially. Must stay under the summary worker's lease
# (nevis.summaries.worker), or a slow summary runs twice.
REQUEST_TIMEOUT_SECONDS = 90
SYSTEM_PROMPT = (
    "You summarize documents for a financial advisor. Reply with two or three plain sentences: "
    "what the document is and its key facts. No preamble, no formatting."
)


class _Message(BaseModel):
    content: str | None = None


class _Choice(BaseModel):
    message: _Message


class _Completion(BaseModel):
    choices: list[_Choice]


class Summarizer(Protocol):
    """What the summary worker needs from a summarization backend."""

    async def summarize(self, title: str, content: str) -> str:
        """A short summary of the document; raises if the backend gives none."""
        ...

    async def aclose(self) -> None: ...


class ChatSummarizer:
    """A Summarizer backed by an OpenAI-compatible chat API."""

    def __init__(self, http_client: AsyncClient, model: str) -> None:
        self._http_client = http_client
        self._model = model

    async def summarize(self, title: str, content: str) -> str:
        response = await self._http_client.post(
            "chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{title}\n\n{content[:CONTENT_MAX_CHARS]}"},
                ],
            },
        )
        response.raise_for_status()
        choices = _Completion.model_validate(response.json()).choices
        summary = (choices[0].message.content or "").strip() if choices else ""
        if not summary:
            msg = "the model returned no summary"
            raise ValueError(msg)
        return summary

    async def aclose(self) -> None:
        await self._http_client.aclose()


def create_summarizer(settings: SummarySettings) -> ChatSummarizer | None:
    """The configured summarizer, or None: then documents are never summarized."""
    if settings.summary_api_url is None or settings.summary_model is None:
        return None
    # Trailing slash: request paths are joined onto the URL's path (e.g. /v1/).
    base_url = str(settings.summary_api_url).rstrip("/") + "/"
    headers = (
        {}
        if settings.summary_api_key is None
        else {"Authorization": f"Bearer {settings.summary_api_key.get_secret_value()}"}
    )
    http_client = AsyncClient(base_url=base_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    return ChatSummarizer(http_client, settings.summary_model)
