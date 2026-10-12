import functools
import json

import httpx2
import pytest
from httpx2 import AsyncClient
from pydantic import HttpUrl, PostgresDsn, SecretStr, ValidationError

from nevis.config import Settings, SummarySettings
from nevis.summaries.client import CONTENT_MAX_CHARS, ChatSummarizer, create_summarizer

CLIENT = {"first_name": "John", "last_name": "Doe", "email": "john.doe@neviswealth.com"}
DOCUMENT = {"title": "Utility bill", "content": "Electricity bill from Vattenfall, CHF 84.20."}


def completion(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.mark.unit
async def test_summarizer_sends_the_document_to_the_model_and_returns_its_reply() -> None:
    requests: list[httpx2.Request] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=completion("  An electricity bill.  "))

    http_client = AsyncClient(
        base_url="http://ollama:11434/v1/", transport=httpx2.MockTransport(reply)
    )
    summarizer = ChatSummarizer(http_client, model="llama3.2")

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
        await ChatSummarizer(http_client, model="m").summarize("t", "c")


@pytest.mark.unit
def test_summarizer_is_off_unless_configured() -> None:
    assert create_summarizer(SummarySettings()) is None


@pytest.mark.unit
async def test_summarizer_is_created_from_the_summary_settings() -> None:
    settings = SummarySettings(summary_api_url=HttpUrl("http://ollama:11434/v1"), summary_model="m")

    summarizer = create_summarizer(settings)

    assert summarizer is not None
    await summarizer.aclose()


def capture_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    """Make create_summarizer's HTTP client answer locally and record what it sends."""
    requests: list[httpx2.Request] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=completion("A bill."))

    transport = httpx2.MockTransport(reply)
    monkeypatch.setattr(
        "nevis.summaries.client.AsyncClient", functools.partial(AsyncClient, transport=transport)
    )
    return requests


@pytest.mark.unit
@pytest.mark.parametrize(
    ("api_key", "authorization"), [(SecretStr("sk-test"), "Bearer sk-test"), (None, None)]
)
async def test_summarizer_sends_the_api_key_as_a_bearer_token_only_when_set(
    monkeypatch: pytest.MonkeyPatch, api_key: SecretStr | None, authorization: str | None
) -> None:
    requests = capture_requests(monkeypatch)
    summarizer = create_summarizer(
        SummarySettings(
            summary_api_url=HttpUrl("https://llm.example/v1"),
            summary_model="m",
            summary_api_key=api_key,
        )
    )
    assert summarizer is not None

    await summarizer.summarize("t", "c")
    await summarizer.aclose()

    [request] = requests
    assert request.headers.get("authorization") == authorization


@pytest.mark.unit
def test_summary_api_key_needs_the_url_and_model() -> None:
    with pytest.raises(ValidationError, match="NEVIS_SUMMARY_API_KEY"):
        SummarySettings(summary_api_key=SecretStr("sk-test"))


@pytest.mark.unit
def test_summary_api_key_stays_out_of_the_settings_repr() -> None:
    settings = SummarySettings(
        summary_api_url=HttpUrl("https://llm.example/v1"),
        summary_model="m",
        summary_api_key=SecretStr("sk-test"),
    )

    assert "sk-test" not in repr(settings)
    assert "sk-test" not in str(settings)


@pytest.mark.unit
def test_summary_settings_treat_empty_values_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker-compose passes unset variables through as empty strings.
    monkeypatch.setenv("NEVIS_SUMMARY_API_URL", "")
    monkeypatch.setenv("NEVIS_SUMMARY_MODEL", "")
    monkeypatch.setenv("NEVIS_SUMMARY_API_KEY", "")

    settings = SummarySettings()

    assert (settings.summary_api_url, settings.summary_model) == (None, None)
    assert settings.summary_api_key is None


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
