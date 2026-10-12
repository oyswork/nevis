"""Measure search relevance: load a labelled corpus into an empty api and report the ranks.

Usually run by ``scripts/relevance.sh`` (``just relevance``) on a throwaway stack. The corpus,
scripts/relevance_corpus.json (all fictional), has 48 documents in five domains (finance,
medical, legal, software support, German) and 58 queries, each labelled with the documents it
should find. Kinds: "sem" shares no distinctive word with them, "xl" is in the other language,
"lex" does share a word. Ranks count among all documents, so the database must start empty.

It reports and doesn't judge: relevance depends on the models, so there is no pass mark. Compare
the numbers before and after a change to search (decisions.md quotes them).
"""

import argparse
import sys
from pathlib import Path
from typing import Literal

from httpx2 import BasicAuth, Client
from pydantic import BaseModel

CORPUS_PATH = Path(__file__).with_name("relevance_corpus.json")
REQUEST_TIMEOUT_SECONDS = 120  # storing a document embeds it
DOCUMENTS_LIMIT = 20  # the api's maximum: a rank beyond it counts as not found
CLIENTS_LIMIT = 10
TOP = 3  # hit@3
ABOVE_SHOWN = 3  # titles listed above a missed document


class CorpusDocument(BaseModel):
    title: str
    content: str


class CorpusQuery(BaseModel):
    q: str
    kind: Literal["sem", "xl", "lex"]
    expected: list[str]  # titles; any of them counts


class Domain(BaseModel):
    name: str
    client: dict[str, str]
    documents: list[CorpusDocument]
    queries: list[CorpusQuery]


class ClientQuery(BaseModel):
    q: str
    expected_email: str


class Corpus(BaseModel):
    domains: list[Domain]
    clients: list[dict[str, str]]
    client_queries: list[ClientQuery]


class Ranked(BaseModel):
    domain: str
    query: CorpusQuery
    rank: int | None  # 1-based; None: not among the first DOCUMENTS_LIMIT
    above: list[str]


def say(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def load(http: Client, corpus: Corpus) -> None:
    for domain in corpus.domains:
        response = http.post("/clients", json=domain.client)
        if response.status_code == 409:
            sys.exit("The corpus is already loaded: run this against an empty database.")
        response.raise_for_status()
        url = f"/clients/{response.json()['id']}/documents"
        for document in domain.documents:
            http.post(url, json=document.model_dump()).raise_for_status()
    for client in corpus.clients:
        http.post("/clients", json=client).raise_for_status()


def rank_documents(http: Client, corpus: Corpus) -> list[Ranked]:
    results: list[Ranked] = []
    for domain in corpus.domains:
        for query in domain.queries:
            response = http.get(
                "/search",
                params={"q": query.q, "clients_limit": 0, "documents_limit": DOCUMENTS_LIMIT},
            )
            response.raise_for_status()
            titles = [hit["document"]["title"] for hit in response.json()]
            rank = next((i for i, t in enumerate(titles, 1) if t in query.expected), None)
            above = titles[: (rank - 1 if rank else ABOVE_SHOWN)][:ABOVE_SHOWN]
            results.append(Ranked(domain=domain.name, query=query, rank=rank, above=above))
    return results


def metrics(results: list[Ranked]) -> str:
    """hit@1, hit@TOP and mean reciprocal rank, formatted as table cells."""
    n = len(results)
    hit1 = sum(r.rank == 1 for r in results) / n
    hit_top = sum(r.rank is not None and r.rank <= TOP for r in results) / n
    mrr = sum(1 / r.rank for r in results if r.rank) / n
    return f"{n:>3}  {hit1:5.2f}  {hit_top:5.2f}  {mrr:5.2f}"


def report_documents(results: list[Ranked]) -> None:
    say(f"Documents (rank among the first {DOCUMENTS_LIMIT})")
    say(f"{'domain':<10} {'queries':>11}  hit@1  hit@{TOP}    MRR   | the same for sem and xl only")
    groups = {
        name: [r for r in results if r.domain == name]
        for name in dict.fromkeys(r.domain for r in results)
    }
    for name, rows in {**groups, "all": results}.items():
        semantic = [r for r in rows if r.query.kind != "lex"]
        say(f"{name:<10} {metrics(rows)}   | {metrics(semantic)}")
    say()
    say("Not ranked first:")
    for r in results:
        if r.rank != 1:
            rank = "-" if r.rank is None else str(r.rank)
            say(f"  {r.domain:<9} {rank:>2}  {r.query.kind:<3} {r.query.q!r}: above it {r.above}")


def report_clients(http: Client, corpus: Corpus) -> None:
    misses: list[str] = []
    for query in corpus.client_queries:
        response = http.get(
            "/search", params={"q": query.q, "clients_limit": CLIENTS_LIMIT, "documents_limit": 0}
        )
        response.raise_for_status()
        emails = [hit["client"]["email"] for hit in response.json()]
        if emails[:1] != [query.expected_email]:
            misses.append(f"  {query.q!r}: expected {query.expected_email}, got {emails[:3]}")
    found = len(corpus.client_queries) - len(misses)
    say()
    say(f"Clients: {found} of {len(corpus.client_queries)} queries find the expected client first")
    for line in misses:
        say(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--base-url", default="http://localhost:8000", help="the api's URL")
    parser.add_argument("--user", help="NAME:PASSWORD for Basic auth")
    args = parser.parse_args()
    corpus = Corpus.model_validate_json(CORPUS_PATH.read_text())
    auth = BasicAuth(*args.user.split(":", 1)) if args.user else None
    with Client(base_url=args.base_url, auth=auth, timeout=REQUEST_TIMEOUT_SECONDS) as http:
        load(http, corpus)
        report_documents(rank_documents(http, corpus))
        report_clients(http, corpus)


if __name__ == "__main__":
    main()
