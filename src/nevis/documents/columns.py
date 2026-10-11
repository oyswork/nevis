"""The columns of the Document row model, for queries that would otherwise `SELECT *`.

documents.tsv (generated, for full-text search) is left out: the app never reads it, and it
would travel with every row. tests/test_documents.py checks this list against the model.
"""

DOCUMENT_COLUMNS = "id, client_id, title, content, summary, created_at"
