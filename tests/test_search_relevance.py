"""Document search on a realistic corpus, with the real embedding model.

The documents and queries come from scripts/relevance_corpus.json (`just relevance` measures the
whole corpus). These queries share no word with their document and ranked it first in every
measured run, on the corpus alone and among 1,102 documents: a regression in the models, the
fusion or the ranking shows up as a miss here.
"""

import json
from pathlib import Path

import pytest
from httpx2 import AsyncClient

CORPUS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "relevance_corpus.json"
DOMAINS = ("finance", "medical", "legal", "software")
QUERIES = {
    "heart attack": "Cardiology discharge summary",
    "secrecy pact": "Mutual non-disclosure agreement",
    "login broken": "Ticket #4812 \N{EN DASH} Authentication failure after password reset",
    "home loan": "Mortgage offer Zürcher Kantonalbank",
}
TOP = 3


@pytest.mark.integration
async def test_queries_find_their_document_by_meaning_among_forty(db_client: AsyncClient) -> None:
    corpus = json.loads(CORPUS_PATH.read_text())
    for domain in corpus["domains"]:
        if domain["name"] not in DOMAINS:
            continue
        client_id = (await db_client.post("/clients", json=domain["client"])).json()["id"]
        for document in domain["documents"]:
            response = await db_client.post(f"/clients/{client_id}/documents", json=document)
            assert response.status_code == 201

    misses: dict[str, list[str]] = {}
    for q, title in QUERIES.items():
        params: dict[str, str | int] = {"q": q, "clients_limit": 0, "documents_limit": TOP}
        titles = [
            hit["document"]["title"]
            for hit in (await db_client.get("/search", params=params)).json()
        ]
        if title not in titles:
            misses[q] = titles

    assert misses == {}
