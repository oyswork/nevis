import json
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import NonCallableMagicMock, create_autospec

import httpx2
import pytest
from httpx2 import AsyncClient
from pydantic import HttpUrl, PostgresDsn, SecretStr, ValidationError

from nevis.config import Settings
from nevis.db import DbPool, create_pool
from nevis.summaries.client import CONTENT_MAX_CHARS, LocalSummarizer, create_summarizer
from nevis.summaries.jobs import summarize_document

CLIENT = {"first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com"}
DOCUMENT = {"title": "Utility bill", "content": "Electricity bill from Vattenfall, CHF 84.20."}


def completion(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.mark.unit
async def test_summarizer_sends_the_document_to_the_local_model_and_returns_its_reply() -> None:
    requests: list[httpx2.Request] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=completion("  An electricity bill.  "))

    http_client = AsyncClient(
        base_url="http://ollama:11434/v1/", transport=httpx2.MockTransport(reply)
    )
    summarizer = LocalSummarizer(http_client, model="llama3.2")

    summary = await summarizer.summarize("Utility bill", "x" * (CONTENT_MAX_CHARS + 100))

    [request] = requests
    body = json.loads(request.content)
    assert request.url == "http://ollama:11434/v1/chat/completions"
    assert body["model"] == "llama3.2"
    assert "Utility bill" in body["messages"][-1]["content"]
    assert body["messages"][-1]["content"].count("x") == CONTENT_MAX_CHARS  # long input is cut
    assert summary == "An electricity bill."


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(500, text="model crashed"),
        httpx2.Response(200, json={"choices": []}),
        httpx2.Response(200, json=completion("   ")),
    ],
)
async def test_summarizer_raises_when_the_model_gives_no_summary(response: httpx2.Response) -> None:
    http_client = AsyncClient(
        base_url="http://ollama/v1/", transport=httpx2.MockTransport(lambda _: response)
    )

    with pytest.raises((httpx2.HTTPStatusError, ValueError)):
        await LocalSummarizer(http_client, model="m").summarize("t", "c")


@pytest.mark.unit
def test_summarizer_is_off_unless_configured(settings: Settings) -> None:
    assert create_summarizer(settings) is None


@pytest.mark.unit
def test_summary_settings_treat_empty_values_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker-compose passes unset variables through as empty strings.
    monkeypatch.setenv("NEVIS_SUMMARY_API_URL", "")
    monkeypatch.setenv("NEVIS_SUMMARY_MODEL", "")

    settings = Settings(
        database_url=PostgresDsn("postgresql://u@db/nevis"),
        meilisearch_url=HttpUrl("http://meilisearch:7700"),
        meilisearch_api_key=SecretStr("a-key-of-at-least-16-bytes"),
    )

    assert (settings.summary_api_url, settings.summary_model) == (None, None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "update",
    [{"summary_api_url": "http://ollama:11434/v1"}, {"summary_model": "llama3.2"}],
)
def test_summary_settings_need_both_url_and_model(
    settings: Settings, update: dict[str, str]
) -> None:
    with pytest.raises(ValidationError, match="NEVIS_SUMMARY_API_URL and NEVIS_SUMMARY_MODEL"):
        Settings.model_validate(settings.model_dump() | update)


@pytest.fixture
async def db_pool(db_settings: Settings) -> AsyncGenerator[DbPool]:
    pool = await create_pool(db_settings)
    try:
        yield pool
    finally:
        await pool.close()


async def summarize_jobs(pool: DbPool) -> int:
    count: int = await pool.fetchval(
        "SELECT count(*) FROM outbox WHERE kind = 'summarize_document'"
    )
    return count


async def create_document(client: AsyncClient) -> str:
    client_id = (await client.post("/clients", json=CLIENT)).json()["id"]
    response = await client.post(f"/clients/{client_id}/documents", json=DOCUMENT)
    document_id: str = response.json()["id"]
    return document_id


@pytest.mark.integration
async def test_no_summary_job_without_a_configured_model(
    db_client: AsyncClient, db_pool: DbPool
) -> None:
    await create_document(db_client)

    assert await summarize_jobs(db_pool) == 0


@pytest.mark.integration
async def test_summary_job_is_enqueued_when_a_model_is_configured(
    summarizing_client: AsyncClient, db_pool: DbPool
) -> None:
    await create_document(summarizing_client)

    assert await summarize_jobs(db_pool) == 1


@pytest.mark.integration
async def test_summary_job_stores_the_summary_once(db_client: AsyncClient, db_pool: DbPool) -> None:
    document_id = await create_document(db_client)
    summarizer: NonCallableMagicMock = create_autospec(LocalSummarizer, instance=True)
    summarizer.summarize.return_value = "An electricity bill."

    await summarize_document(db_pool, summarizer, uuid.UUID(document_id))
    summarizer.summarize.return_value = "A different summary."
    await summarize_document(db_pool, summarizer, uuid.UUID(document_id))  # the job ran twice
    await summarize_document(db_pool, summarizer, uuid.uuid4())  # document gone: nothing to do

    summarizer.summarize.assert_called_once_with(DOCUMENT["title"], DOCUMENT["content"])
    document = (await db_client.get(f"/documents/{document_id}")).json()
    assert document["summary"] == "An electricity bill."
