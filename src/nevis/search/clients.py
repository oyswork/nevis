"""Client search in Postgres: trigram substring and typo matches over `clients.search_text`.

`search_text` and the query are normalized the same way (nevis.text: case, accents, script,
whitespace).
"""

import re

from nevis.clients.schemas import ClientRead
from nevis.db import DbPool
from nevis.db_models import Client
from nevis.search.schemas import ClientHit
from nevis.text import normalize

# Shorter substrings match nearly every client, so a short query only matches whole words.
SUBSTRING_MIN_LENGTH = 3
# $1: the query as a LIKE-escaped literal, $2: the same as an escaped regular expression, $3: the
# query itself, $4: the limit. Whole words: bounded by the text's ends or by non-alphanumerics (a
# `\m ... \M` pair would fail for queries that start or end with punctuation). Tiers: whole word,
# part of a word, typo (`<%`, at the pool's word_similarity_threshold, see nevis.db); each
# ordered by similarity, then oldest first. The `LIKE` and `<%`
# conditions are what the trigram index serves; the regex only orders what they return.
SELECT_CLIENTS = """
    SELECT *
    FROM (
        SELECT clients.*,
               CASE
                   WHEN search_text ~ ('(^|[^[:alnum:]])' || $2 || '([^[:alnum:]]|$)') THEN 0
                   WHEN search_text LIKE '%' || $1 || '%' ESCAPE '\\' THEN 1
                   ELSE 2
               END AS tier,
               word_similarity($3, search_text) AS similarity
        FROM clients
        WHERE search_text LIKE '%' || $1 || '%' ESCAPE '\\' OR $3 <% search_text
    ) AS matches
    ORDER BY tier, similarity DESC, created_at, id
    LIMIT $4
"""
# Short queries: whole words only, no index help and no typo matching.
SELECT_CLIENTS_BY_WORD = """
    SELECT *
    FROM clients
    WHERE search_text ~ ('(^|[^[:alnum:]])' || $1 || '([^[:alnum:]]|$)')
    ORDER BY created_at, id
    LIMIT $2
"""
# Computed columns of SELECT_CLIENTS that aren't part of the row model.
EXTRA_COLUMNS = frozenset({"tier", "similarity"})


def like_escape(text: str) -> str:
    """`text` as a LIKE pattern matching itself: \\, % and _ lose their meaning."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def regex_escape(text: str) -> str:
    """`text` as a Postgres regular expression matching itself (re.escape's output is valid)."""
    return re.escape(text)


async def search_clients(pool: DbPool, q: str, limit: int) -> list[ClientHit]:
    """Clients matching `q`: whole words, then parts of words, then typos; at most `limit`."""
    text = normalize(q)
    if limit == 0 or not any(character.isalnum() for character in text):
        return []  # without a word it would match every client
    if len(text) < SUBSTRING_MIN_LENGTH:
        rows = await pool.fetch(SELECT_CLIENTS_BY_WORD, regex_escape(text), limit)
    else:
        rows = await pool.fetch(SELECT_CLIENTS, like_escape(text), regex_escape(text), text, limit)
    return [
        ClientHit(
            client=ClientRead.model_validate(
                Client(**{key: value for key, value in row.items() if key not in EXTRA_COLUMNS})
            )
        )
        for row in rows
    ]
