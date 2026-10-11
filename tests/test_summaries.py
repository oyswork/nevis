import json

import httpx2
import pytest
from httpx2 import AsyncClient
from pydantic import HttpUrl, PostgresDsn, ValidationError

from nevis.config import Settings, SummarySettings
from nevis.summaries.client import CONTENT_MAX_CHARS, LocalSummarizer, create_summarizer

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
def test_summarizer_is_off_unless_configured() -> None:
    assert create_summarizer(SummarySettings()) is None


@pytest.mark.unit
async def test_summarizer_is_created_from_the_summary_settings() -> None:
    settings = SummarySettings(summary_api_url=HttpUrl("http://ollama:11434/v1"), summary_model="m")

    summarizer = create_summarizer(settings)

    assert summarizer is not None
    await summarizer.aclose()


@pytest.mark.unit
def test_summary_settings_treat_empty_values_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker-compose passes unset variables through as empty strings.
    monkeypatch.setenv("NEVIS_SUMMARY_API_URL", "")
    monkeypatch.setenv("NEVIS_SUMMARY_MODEL", "")

    settings = SummarySettings()

    assert (settings.summary_api_url, settings.summary_model) == (None, None)


@pytest.mark.unit
def test_api_settings_ignore_a_half_set_summary_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the worker uses the summary settings; they must not be able to crash the api.
    monkeypatch.setenv("NEVIS_SUMMARY_MODEL", "llama3.2")
    monkeypatch.delenv("NEVIS_SUMMARY_API_URL", raising=False)

    settings = Settings(
        database_url=PostgresDsn("postgresql://u@db/nevis"),
    )

    assert not hasattr(settings, "summary_model")


@pytest.mark.unit
@pytest.mark.parametrize(
    "update",
    [{"summary_api_url": "http://ollama:11434/v1"}, {"summary_model": "llama3.2"}],
)
def test_summary_settings_need_both_url_and_model(update: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="NEVIS_SUMMARY_API_URL and NEVIS_SUMMARY_MODEL"):
        SummarySettings.model_validate(update)
