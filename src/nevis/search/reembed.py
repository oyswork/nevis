"""Rebuild the Postgres search data: ``python -m nevis.search.reembed [--all]`` (``just reembed``).

- Documents without chunks (rows from before the search tables existed) are chunked and
  embedded; with ``--all`` every document is, replacing its chunks (after changing the model).
- ``clients.search_text`` is recomputed for every client (cheap).

Idempotent: a document's chunks are replaced in one transaction, so search never sees it half done.
"""

import argparse
import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import asyncpg

from nevis.config import Settings
from nevis.db import DbPool, create_pool
from nevis.embeddings import Embedder, create_embedder
from nevis.search.chunks import embed_document, insert_chunks
from nevis.text import client_search_text

logger = logging.getLogger(__name__)

NIL_UUID = uuid.UUID(int=0)

PAGE_SIZE = 100
# Keyset pagination over ids: no page holds the whole table, and rows added or removed while
# running don't shift the pages.
SELECT_DOCUMENT_IDS = """
    SELECT id FROM documents
    WHERE id > $1
      AND ($2 OR NOT EXISTS (SELECT 1 FROM document_chunks WHERE document_id = documents.id))
    ORDER BY id
    LIMIT $3
"""
SELECT_DOCUMENTS = "SELECT id, title, content FROM documents WHERE id = ANY($1::uuid[])"
# Serializes with other runs (and deletes) on the same document for the rest of the transaction.
LOCK_DOCUMENT = "SELECT 1 FROM documents WHERE id = $1 FOR UPDATE"
HAS_CHUNKS = "SELECT EXISTS (SELECT 1 FROM document_chunks WHERE document_id = $1)"
DELETE_CHUNKS = "DELETE FROM document_chunks WHERE document_id = $1"
SELECT_CLIENTS = """
    SELECT id, first_name, last_name, email, description, search_text FROM clients
    WHERE id > $1 ORDER BY id LIMIT $2
"""
SET_SEARCH_TEXT = "UPDATE clients SET search_text = $2 WHERE id = $1"


@dataclass(frozen=True)
class ReembedCounts:
    documents: int  # documents whose chunks were written
    chunks: int
    clients: int  # clients whose search_text changed


async def reembed(
    pool: DbPool, embedder: Embedder, *, all_documents: bool = False
) -> ReembedCounts:
    documents = 0
    chunk_count = 0
    last_id = NIL_UUID
    while page := await pool.fetch(SELECT_DOCUMENT_IDS, last_id, all_documents, PAGE_SIZE):
        last_id = page[-1]["id"]
        rows = await pool.fetch(SELECT_DOCUMENTS, [row["id"] for row in page])
        for row in rows:
            written = await _reembed_document(
                pool, embedder, row["id"], row["title"], row["content"], all_documents
            )
            if written is not None:
                documents += 1
                chunk_count += written
        logger.info("Documents: %d written so far (page ending at %s)", documents, last_id)
    return ReembedCounts(documents, chunk_count, await _refresh_search_text(pool))


async def _reembed_document(
    pool: DbPool,
    embedder: Embedder,
    document_id: uuid.UUID,
    title: str,
    content: str,
    all_documents: bool,
) -> int | None:
    """Replace the document's chunks; the number written, or None if it was skipped."""
    # Embed before the transaction, so none is held open meanwhile. The cost: when another run
    # got there first, this embedding is thrown away.
    chunks = await embed_document(embedder, title, content)
    try:
        async with pool.acquire() as connection, connection.transaction():
            if await connection.fetchval(LOCK_DOCUMENT, document_id) is None:
                logger.debug("Document %s is gone, skipping", document_id)
                return None
            if not all_documents and await connection.fetchval(HAS_CHUNKS, document_id):
                logger.debug("Document %s got chunks meanwhile, skipping", document_id)
                return None
            await connection.execute(DELETE_CHUNKS, document_id)
            await insert_chunks(connection, document_id, chunks)
    except asyncpg.ForeignKeyViolationError, asyncpg.UniqueViolationError:
        logger.warning("Could not store the chunks of document %s", document_id, exc_info=True)
        return None
    return len(chunks.texts)


async def _refresh_search_text(pool: DbPool) -> int:
    changed = 0
    last_id = NIL_UUID
    while page := await pool.fetch(SELECT_CLIENTS, last_id, PAGE_SIZE):
        last_id = page[-1]["id"]
        updates = [
            (row["id"], search_text)
            for row in page
            if (
                search_text := client_search_text(
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    email=row["email"],
                    description=row["description"],
                )
            )
            != row["search_text"]
        ]
        if updates:
            await pool.executemany(SET_SEARCH_TEXT, updates)
        changed += len(updates)
        logger.info("Clients: %d updated so far (page ending at %s)", changed, last_id)
    return changed


async def _main(all_documents: bool) -> None:
    settings = Settings()
    pool = await create_pool(settings)
    try:
        embedder = await create_embedder(settings)
        counts = await reembed(pool, embedder, all_documents=all_documents)
    finally:
        await pool.close()
    logger.info(
        "Embedded %d documents (%d chunks); updated search_text of %d clients",
        counts.documents,
        counts.chunks,
        counts.clients,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--all", action="store_true", help="re-embed every document, not just those without chunks"
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_main(arguments.all))


if __name__ == "__main__":
    main()
