"""Exercise a running Nevis API end to end, the way a client of it would.

Usually run by ``scripts/exercise.sh`` (``just exercise``), which starts a throwaway stack whose
summary worker uses a local model. Against any other running api:

    uv run python scripts/exercise.py --base-url https://203.0.113.7 --user name:password

Covers every endpoint: health and docs, clients, documents, search (the task's examples, typos,
accents, limits, ordering, hostile input, validation) and summaries written by the worker. It
only adds data, and every email carries a per-run tag, so it can run again on the same database;
search checks look only at what this run created. Exits 1 if any check failed.
"""

import argparse
import sys
import time
import uuid
from typing import Any

from httpx2 import BasicAuth, Client, Response

# Any: arbitrary JSON as the api returns it; the checks below are what validates it.
type JsonObject = dict[str, Any]

REQUEST_TIMEOUT_SECONDS = 60  # storing a long document embeds it before responding
SUMMARY_POLL_SECONDS = 2
SUMMARY_PREVIEW_CHARS = 160
CLIENTS_MAX_LIMIT = 50  # nevis.search.router
DOCUMENTS_MAX_LIMIT = 20
QUERY_MAX_LENGTH = 200

JOHN, ANNA, ZOE, MIA = "john", "anna", "zoe", "mia"
UTILITY_BILL = "Utility bill March"
PASSPORT = "Passport scan"
TAX_RETURN = "Tax return 2025"
INVESTMENT_POLICY = "Investment policy statement"
BANK_STATEMENT = "Bank statement September"
PORTFOLIO_REVIEW = "Annual portfolio review 2026"


def say(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class Run:
    """The HTTP client plus the checks that passed and failed so far."""

    def __init__(self, http: Client) -> None:
        self.http = http
        self.passed = 0
        self.failures: list[str] = []

    def section(self, title: str) -> None:
        say(f"\n== {title}")

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        if ok:
            self.passed += 1
            say(f"ok    {name}")
        else:
            self.failures.append(name)
            say(f"FAIL  {name}" + (f"\n      {detail}" if detail != "" else ""))
        return ok

    def expect(self, name: str, response: Response, status: int) -> bool:
        detail = f"got {response.status_code}: {response.text[:300]}"
        return self.check(f"{status}  {name}", response.status_code == status, detail)

    def search(self, q: str, **params: int) -> list[JsonObject]:
        response = self.http.get("/search", params={"q": q, **params})
        if response.status_code != 200:
            self.check(f"200  search {q!r}", False, f"got {response.status_code}: {response.text}")
            return []
        hits: list[JsonObject] = response.json()
        return hits


def client_hits(hits: list[JsonObject]) -> list[str]:
    return [hit["client"]["id"] for hit in hits if hit["type"] == "client"]


def document_hits(hits: list[JsonObject]) -> list[str]:
    return [hit["document"]["id"] for hit in hits if hit["type"] == "document"]


def check_health(run: Run) -> None:
    run.section("health and docs")
    run.expect("GET /health", run.http.get("/health"), 200)
    run.expect("GET /health/ready (database reachable)", run.http.get("/health/ready"), 200)
    docs = run.http.get("/docs")
    if run.expect("GET /docs", docs, 200):
        run.check("Swagger UI is HTML", "text/html" in docs.headers.get("content-type", ""))
    openapi = run.http.get("/openapi.json")
    if run.expect("GET /openapi.json", openapi, 200):
        paths = set(openapi.json()["paths"])
        wanted = {
            "/clients",
            "/clients/{client_id}",
            "/clients/{client_id}/documents",
            "/documents/{document_id}",
            "/search",
        }
        run.check("OpenAPI documents every endpoint", wanted <= paths, f"missing {wanted - paths}")


def create_clients(run: Run, tag: str) -> dict[str, JsonObject]:
    run.section("clients")
    john_email = f"john.doe.{tag}@neviswealth.com"
    payloads: dict[str, JsonObject] = {
        JOHN: {
            "first_name": "John",
            "last_name": "Doe",
            "email": john_email,
            "description": "Prefers sustainable funds; retiring in 2030",
            "social_links": ["https://www.linkedin.com/in/johndoe"],
        },
        ANNA: {
            "first_name": "Anna",
            "last_name": "Schmidt",
            "email": f"anna.{tag}@weberfamilyoffice.de",
            "description": "Referred by NevisWealth",
        },
        ZOE: {"first_name": "Zoë", "last_name": "McAllister", "email": f"zoe.{tag}@example.com"},
        # Whitespace is trimmed and a blank description is stored as null.
        MIA: {
            "first_name": "  Mia ",
            "last_name": " Rossi",
            "email": f"mia.{tag}@example.org",
            "description": "   ",
        },
    }
    clients: dict[str, JsonObject] = {}
    for key, payload in payloads.items():
        response = run.http.post("/clients", json=payload)
        if run.expect(f"POST /clients ({key})", response, 201):
            clients[key] = response.json()
    if JOHN in clients:
        john = clients[JOHN]
        run.check(
            "the created client echoes its fields",
            {k: john[k] for k in payloads[JOHN]} == payloads[JOHN],
            john,
        )
        fetched = run.http.get(f"/clients/{john['id']}")
        if run.expect("GET /clients/{id}", fetched, 200):
            run.check("GET returns the stored client", fetched.json() == john, fetched.json())
        duplicate = {**payloads[JOHN], "email": john_email.upper()}
        run.expect(
            "POST /clients with a known email in other case",
            run.http.post("/clients", json=duplicate),
            409,
        )
    if MIA in clients:
        mia = clients[MIA]
        run.check(
            "names are trimmed and a blank description becomes null",
            (mia["first_name"], mia["last_name"], mia["description"]) == ("Mia", "Rossi", None),
            mia,
        )
        run.check("social_links defaults to []", mia["social_links"] == [], mia)
    run.expect("GET /clients/{unknown id}", run.http.get(f"/clients/{uuid.uuid4()}"), 404)
    run.expect("GET /clients/not-a-uuid", run.http.get("/clients/not-a-uuid"), 422)
    return clients


def check_client_validation(run: Run, tag: str) -> None:
    valid = {"first_name": "Val", "last_name": "Id", "email": f"valid.{tag}@example.com"}
    invalid = {
        "missing email": {"first_name": "Val", "last_name": "Id"},
        "malformed email": {**valid, "email": "not-an-email"},
        "empty first_name": {**valid, "first_name": ""},
        "first_name over 100 characters": {**valid, "first_name": "x" * 101},
        "NUL in last_name": {**valid, "last_name": "I\x00d"},
        "unknown field": {**valid, "nickname": "Val"},
        "javascript: social link": {**valid, "social_links": ["javascript:alert(1)"]},
        "21 social links": {**valid, "social_links": ["https://example.com"] * 21},
    }
    for name, payload in invalid.items():
        run.expect(f"POST /clients rejects {name}", run.http.post("/clients", json=payload), 422)


def portfolio_review_content() -> str:
    """A document long enough to be split into several chunks (nevis.documents.chunking)."""
    paragraphs = [
        f"Quarter {quarter} of 2026: portfolio rebalancing brought equities back to the 60 percent "
        f"target after market moves; fixed income duration stayed near {quarter + 4} years and the "
        "cash buffer covered six months of planned withdrawals."
        for quarter in range(1, 5)
    ] * 6
    return "\n\n".join(paragraphs)


def create_documents(run: Run, clients: dict[str, JsonObject]) -> dict[str, JsonObject]:
    run.section("documents")
    planned = [
        (
            JOHN,
            UTILITY_BILL,
            "Electricity utility bill from Vattenfall for the apartment at 12 Lake Road, Zurich. "
            "Billing period March 2026, amount due CHF 84.20.",
        ),
        (
            JOHN,
            PASSPORT,
            "Scanned copy of the client's Swiss passport, issued 2021, valid until 2031.",
        ),
        (
            JOHN,
            TAX_RETURN,
            "Federal and cantonal tax return for 2025. Taxable income CHF 182,400; wealth tax on "
            "the securities portfolio; deductions for pillar 3a pension contributions.",
        ),
        (
            ANNA,
            INVESTMENT_POLICY,
            "Agreed investment policy: balanced risk profile, 60% equities and 40% bonds, ESG "
            "exclusions for tobacco and weapons, reviewed every year.",
        ),
        (
            ZOE,
            BANK_STATEMENT,
            "Monthly statement of the UBS current account for September 2026. Opening balance "
            "CHF 12,300, salary credit CHF 9,800, mortgage payment CHF 2,150.",
        ),
        (ANNA, PORTFOLIO_REVIEW, portfolio_review_content()),
    ]
    documents: dict[str, JsonObject] = {}
    for owner, title, content in planned:
        if owner not in clients:
            run.check(f"POST document {title!r}", False, f"client {owner} wasn't created")
            continue
        client_id = clients[owner]["id"]
        response = run.http.post(
            f"/clients/{client_id}/documents", json={"title": title, "content": content}
        )
        if run.expect(f"POST /clients/{{id}}/documents ({title})", response, 201):
            documents[title] = response.json()
            document = documents[title]
            run.check(
                f"{title!r} is stored for its client with no summary yet",
                (document["client_id"], document["title"], document["summary"])
                == (client_id, title, None),
                document,
            )
    if UTILITY_BILL in documents:
        bill = documents[UTILITY_BILL]
        fetched = run.http.get(f"/documents/{bill['id']}")
        if run.expect("GET /documents/{id}", fetched, 200):
            run.check("GET returns the stored document", fetched.json() == bill, fetched.json())
    run.expect("GET /documents/{unknown id}", run.http.get(f"/documents/{uuid.uuid4()}"), 404)
    run.expect("GET /documents/not-a-uuid", run.http.get("/documents/not-a-uuid"), 422)
    return documents


def check_document_validation(run: Run, clients: dict[str, JsonObject]) -> None:
    valid = {"title": "Note", "content": "Call back next week."}
    run.expect(
        "POST a document for an unknown client",
        run.http.post(f"/clients/{uuid.uuid4()}/documents", json=valid),
        404,
    )
    run.expect(
        "POST a document for a malformed client id",
        run.http.post("/clients/not-a-uuid/documents", json=valid),
        422,
    )
    if JOHN not in clients:
        return
    url = f"/clients/{clients[JOHN]['id']}/documents"
    invalid = {
        "missing content": {"title": "Note"},
        "empty title": {**valid, "title": ""},
        "blank content": {**valid, "content": "   "},
        "title over 200 characters": {**valid, "title": "x" * 201},
        "unknown field": {**valid, "author": "me"},
    }
    for name, payload in invalid.items():
        run.expect(f"POST a document rejects {name}", run.http.post(url, json=payload), 422)


def check_client_search(run: Run, tag: str, clients: dict[str, JsonObject]) -> None:
    run.section("search: clients")
    ids = {key: client["id"] for key, client in clients.items()}
    found = client_hits(run.search(tag, clients_limit=CLIENTS_MAX_LIMIT, documents_limit=0))
    run.check(
        "new clients are searchable right away (by the run tag in their emails)",
        set(ids.values()) <= set(found),
        found,
    )

    def finds(q: str, wanted: list[str], unwanted: tuple[str, ...] = (), why: str = "") -> None:
        found = client_hits(run.search(q, clients_limit=CLIENTS_MAX_LIMIT, documents_limit=0))
        names = [key for key, client_id in ids.items() if client_id in found]
        ok = all(ids.get(k) in found for k in wanted) and not any(k in names for k in unwanted)
        run.check(f"{q!r} finds {', '.join(wanted)}" + (f" ({why})" if why else ""), ok, names)

    finds("NevisWealth", [JOHN, ANNA], (ZOE, MIA), "task example: email and description")
    finds("neviswealth", [JOHN], why="any case")
    finds("wealth", [JOHN], why="inside a word")
    finds("neviswelth", [JOHN], why="a typo")
    finds("Zoe", [ZOE], why="without the accent")
    finds("MCALLISTER", [ZOE], why="upper case")
    finds("Rossi", [MIA])
    finds("sustainable", [JOHN], why="description")

    for q in ["qqqzzzxxx", 'x" OR id EXISTS OR email CONTAINS "y', "' OR 1=1 --", "%", "_"]:
        found = client_hits(run.search(q, clients_limit=CLIENTS_MAX_LIMIT))
        run.check(
            f"{q!r} is plain text and finds none of the clients",
            not set(found) & set(ids.values()),
            found,
        )


def our_ranking(hits: list[JsonObject], documents: dict[str, JsonObject]) -> list[str]:
    """Titles of this run's documents in the order search returned them."""
    titles = {document["id"]: title for title, document in documents.items()}
    return [titles[i] for i in document_hits(hits) if i in titles]


def check_document_search(run: Run, documents: dict[str, JsonObject]) -> None:
    run.section("search: documents")

    def ranks(q: str, title: str, within: int, why: str = "") -> None:
        ranking = our_ranking(run.search(q, documents_limit=DOCUMENTS_MAX_LIMIT), documents)
        ok = title in ranking[:within]
        place = "first" if within == 1 else f"in the top {within}"
        run.check(f"{q!r} ranks {title!r} {place}" + (f" ({why})" if why else ""), ok, ranking)

    # The bill has neither word; identity documents rank closer to "proof" (README, decisions.md).
    ranks("address proof", UTILITY_BILL, 2, "task example: by meaning")
    ranks("Vattenfall", UTILITY_BILL, 1, "a word in the content")
    ranks("electricity", UTILITY_BILL, 1)
    ranks("identity document", PASSPORT, 1)
    ranks("income taxes", TAX_RETURN, 1)
    ranks("salary and mortgage", BANK_STATEMENT, 1)
    ranks("asset allocation", INVESTMENT_POLICY, 2)
    ranks("portfolio rebalancing", PORTFOLIO_REVIEW, 1, "a long document")

    hits = run.search("portfolio rebalancing", documents_limit=DOCUMENTS_MAX_LIMIT)
    ranking = our_ranking(hits, documents)
    run.check("a long document is listed once", ranking.count(PORTFOLIO_REVIEW) == 1, ranking)

    hits = run.search("Vattenfall", documents_limit=DOCUMENTS_MAX_LIMIT)
    bill = next(
        (h for h in hits if h["type"] == "document" and h["document"]["title"] == UTILITY_BILL),
        None,
    )
    run.check(
        "a document hit carries the matched chunk and the document without its content",
        bill is not None
        and "Vattenfall" in bill["matched_text"]
        and "content" not in bill["document"],
        bill,
    )


def check_search_shape(run: Run) -> None:
    run.section("search: ordering, limits and validation")
    hits = run.search("NevisWealth", clients_limit=1, documents_limit=2)
    types = [hit["type"] for hit in hits]
    run.check(
        "clients come first, and both limits hold",
        types == ["client", "document", "document"],
        types,
    )
    types = [hit["type"] for hit in run.search("NevisWealth", clients_limit=0)]
    run.check("clients_limit=0 returns no clients", "client" not in types, types)
    types = [hit["type"] for hit in run.search("NevisWealth", documents_limit=0)]
    run.check("documents_limit=0 returns no documents", "document" not in types, types)
    run.check("the default documents_limit is 5", len(document_hits(run.search("bill"))) <= 5)

    invalid: dict[str, dict[str, str | int]] = {
        "a missing q": {},
        "an empty q": {"q": ""},
        "a blank q": {"q": "   "},
        f"a q over {QUERY_MAX_LENGTH} characters": {"q": "x" * (QUERY_MAX_LENGTH + 1)},
        "a NUL in q": {"q": "a\x00b"},
        f"clients_limit={CLIENTS_MAX_LIMIT + 1}": {
            "q": "a",
            "clients_limit": CLIENTS_MAX_LIMIT + 1,
        },
        f"documents_limit={DOCUMENTS_MAX_LIMIT + 1}": {
            "q": "a",
            "documents_limit": DOCUMENTS_MAX_LIMIT + 1,
        },
        "documents_limit=-1": {"q": "a", "documents_limit": -1},
        "a non-numeric limit": {"q": "a", "clients_limit": "many"},
    }
    for name, params in invalid.items():
        run.expect(f"GET /search rejects {name}", run.http.get("/search", params=params), 422)


def check_summaries(run: Run, documents: dict[str, JsonObject], timeout: float) -> None:
    run.section(f"summaries (the worker and the local model; waiting up to {timeout:.0f} s)")
    pending = {title: document["id"] for title, document in documents.items()}
    started = time.monotonic()
    while pending and time.monotonic() - started < timeout:
        for title, document_id in list(pending.items()):
            response = run.http.get(f"/documents/{document_id}")
            summary = response.json().get("summary") if response.status_code == 200 else None
            if summary:
                del pending[title]
                elapsed = time.monotonic() - started
                preview = " ".join(summary.split())
                if len(preview) > SUMMARY_PREVIEW_CHARS:
                    preview = preview[:SUMMARY_PREVIEW_CHARS] + "…"
                run.check(f"{title!r} summarized after {elapsed:.0f} s", True)
                say(f"      {preview}")
        if pending:
            time.sleep(SUMMARY_POLL_SECONDS)
    for title in pending:
        run.check(f"{title!r} summarized", False, "no summary yet: check the worker's logs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--base-url", default="http://localhost:8000", help="the api's URL")
    parser.add_argument("--user", help="NAME:PASSWORD for Basic auth (the deployed server)")
    parser.add_argument(
        "--summary-timeout",
        type=float,
        default=300,
        help="seconds to wait for the worker's summaries; 0 skips the summary checks",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    auth = BasicAuth(*args.user.split(":", 1)) if args.user else None
    tag = uuid.uuid4().hex[:10]
    say(f"Exercising {args.base_url} (run tag {tag})")
    with Client(base_url=args.base_url, auth=auth, timeout=REQUEST_TIMEOUT_SECONDS) as http:
        run = Run(http)
        check_health(run)
        clients = create_clients(run, tag)
        check_client_validation(run, tag)
        documents = create_documents(run, clients)
        check_document_validation(run, clients)
        check_client_search(run, tag, clients)
        check_document_search(run, documents)
        check_search_shape(run)
        if args.summary_timeout > 0:
            check_summaries(run, documents, args.summary_timeout)
    say(f"\n== {run.passed} passed, {len(run.failures)} failed")
    for name in run.failures:
        say(f"FAIL  {name}")
    return 1 if run.failures else 0


if __name__ == "__main__":
    sys.exit(main())
