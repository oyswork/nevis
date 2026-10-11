"""The columns of the Document row model, for queries that would otherwise `SELECT *`.

documents.tsv (generated, for full-text search) is left out: the app never reads it, and it
would travel with every row. The summary lease columns are read by the summary worker; the api's
DocumentRead leaves them out of responses. tests/test_search_data.py checks this list against
the model.
"""

DOCUMENT_COLUMNS = (
    "id, client_id, title, content, summary, summary_attempts, summary_locked_until, created_at"
)
