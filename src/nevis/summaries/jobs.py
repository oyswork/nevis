import uuid

from nevis.db import DbPool
from nevis.db_models import Document
from nevis.summaries.client import LocalSummarizer

SELECT_DOCUMENT = "SELECT * FROM documents WHERE id = $1"
# Never overwrites: a summary, once written, is the summary.
SET_SUMMARY = "UPDATE documents SET summary = $2 WHERE id = $1 AND summary IS NULL"


async def summarize_document(
    pool: DbPool, summarizer: LocalSummarizer, document_id: uuid.UUID
) -> None:
    """Outbox job: summarize the document unless it's gone or already summarized (idempotent)."""
    row = await pool.fetchrow(SELECT_DOCUMENT, document_id)
    if row is None:
        return
    document = Document(**row)
    if document.summary is not None:
        return
    summary = await summarizer.summarize(document.title, document.content)
    await pool.execute(SET_SUMMARY, document.id, summary)
