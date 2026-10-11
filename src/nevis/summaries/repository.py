import datetime
import uuid

from nevis.db import DbConnection, DbPool
from nevis.db_models import Document
from nevis.documents.columns import DOCUMENT_COLUMNS

# LISTEN/NOTIFY channel on which POST /documents announces a new document to the worker.
SUMMARY_CHANNEL = "documents_to_summarize"

NOTIFY = "SELECT pg_notify($1, $2)"

# Takes a lease instead of holding a row lock: the claim commits at once, so no transaction stays
# open during the (slow) model call, and a crashed worker's document becomes claimable again when
# the lease expires. SKIP LOCKED keeps concurrent claims from picking the same document.
CLAIM_DOCUMENT = f"""
    UPDATE documents
    SET summary_locked_until = now() + $1::interval
    WHERE id = (
        SELECT id FROM documents
        WHERE summary IS NULL
          AND (summary_locked_until IS NULL OR summary_locked_until < now())
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING {DOCUMENT_COLUMNS}
"""  # noqa: S608 - constant column list

# Never overwrites: a summary, once written, is the summary. Zero rows: it was written by someone
# else (a run after an expired lease) or the document is gone.
SET_SUMMARY = """
    UPDATE documents SET summary = $2, summary_locked_until = NULL
    WHERE id = $1 AND summary IS NULL
"""

# Counts the failure and keeps the document unavailable until the next attempt is due. $3 is the
# lease this worker claimed with: if the lease expired and another worker re-claimed the document,
# the lease differs and this stale failure must not overwrite the newer one.
RECORD_FAILURE = """
    UPDATE documents
    SET summary_attempts = summary_attempts + 1,
        summary_locked_until = now() + $2::interval
    WHERE id = $1 AND summary IS NULL AND summary_locked_until = $3
"""


async def notify_document(connection: DbConnection, document_id: uuid.UUID) -> None:
    """Announce a document to the worker. Delivered when the caller's transaction commits."""
    await connection.execute(NOTIFY, SUMMARY_CHANNEL, str(document_id))


async def claim_document(pool: DbPool, lease: datetime.timedelta) -> Document | None:
    row = await pool.fetchrow(CLAIM_DOCUMENT, lease)
    return None if row is None else Document(**row)


async def set_summary(pool: DbPool, document_id: uuid.UUID, summary: str) -> bool:
    """Store the summary unless the document has one already; False if nothing was written."""
    status: str = await pool.execute(SET_SUMMARY, document_id, summary)
    return status == "UPDATE 1"


async def record_failure(
    pool: DbPool,
    document_id: uuid.UUID,
    delay: datetime.timedelta,
    claimed_lease: datetime.datetime | None,
) -> bool:
    """Count a failure of the attempt that claimed the document with `claimed_lease`.

    False if nothing was updated: the document was re-claimed, summarized or deleted meanwhile.
    """
    status: str = await pool.execute(RECORD_FAILURE, document_id, delay, claimed_lease)
    return status == "UPDATE 1"
