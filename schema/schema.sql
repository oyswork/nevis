-- Desired database state. Atlas diffs it against the migration history to generate versioned
-- migrations in schema/migrations/ (`just migration <name>`); omymodels generates
-- src/nevis/db_models.py from it.
-- Spell aware timestamps "timestamptz" (generated as pydantic.AwareDatetime): the DDL parser
-- reduces "timestamp with time zone" to plain "timestamp", i.e. a naive datetime.

CREATE TABLE clients (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    first_name varchar(100) NOT NULL,
    last_name varchar(100) NOT NULL,
    email varchar(320) NOT NULL,
    description text,
    social_links text[] NOT NULL DEFAULT '{}',
    -- Name, email and description, lowercased and stripped of accents (nevis.text), for trigram
    -- search. Normalized in Python: unaccent isn't IMMUTABLE, so an index or generated column
    -- would need a wrapper function, which is an Atlas Pro feature. '' for rows that predate it
    -- (`just reembed` fills them in).
    search_text text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Emails are unique regardless of case: John.Doe@x.com and john.doe@x.com are the same person.
CREATE UNIQUE INDEX uq_clients_email_lower ON clients (lower(email));

-- Substring (LIKE) and typo-tolerant (word_similarity) client search.
CREATE INDEX ix_clients_search_text_trgm ON clients USING gin (search_text gin_trgm_ops);

CREATE TABLE documents (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL,
    title varchar(200) NOT NULL,
    content text NOT NULL,
    -- Filled in later, and only if a local summarization model is configured.
    summary text,
    -- Summary worker bookkeeping (nevis.summaries). summary_locked_until is the lease of the
    -- worker that claimed the document, or the time of the next attempt after a failure; NULL
    -- when nobody holds it. summary_attempts counts failed attempts, for inspection.
    summary_attempts integer NOT NULL DEFAULT 0,
    summary_locked_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Full-text search over title and content (not the summary, which may arrive later).
    tsv tsvector NOT NULL GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || content)) STORED,
    PRIMARY KEY (id),
    CONSTRAINT fk_documents_client FOREIGN KEY (client_id) REFERENCES clients (id) ON DELETE CASCADE
);

CREATE INDEX ix_documents_client_id ON documents (client_id);
CREATE INDEX ix_documents_tsv ON documents USING gin (tsv);

-- The summary worker's claim query: unsummarized documents, oldest first. Ordered by created_at
-- rather than indexed on summary_locked_until: the lease test is a cheap filter on a small set,
-- and (IS NULL OR < now()) can't use an index range anyway.
CREATE INDEX ix_documents_unsummarized ON documents (created_at) WHERE summary IS NULL;

-- Overlapping chunks of a document's content (nevis.documents.chunking), each embedded with its
-- document's title. Written in the same transaction as the document. 384 is the output size of
-- the embedding model (nevis.embeddings.EMBEDDING_DIMENSIONS).
CREATE TABLE document_chunks (
    document_id uuid NOT NULL,
    ordinal integer NOT NULL,
    text text NOT NULL,
    embedding vector(384) NOT NULL,
    PRIMARY KEY (document_id, ordinal),
    CONSTRAINT fk_document_chunks_document FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
);

-- Nearest-neighbour search by cosine distance.
CREATE INDEX ix_document_chunks_embedding ON document_chunks USING hnsw (embedding vector_cosine_ops);

-- Which embedding model produced the vectors in document_chunks (nevis.embeddings.EMBEDDING_MODEL):
-- vectors from two models can't be compared. One row, recorded by the api's first start and
-- replaced by `just reembed --all`; the api refuses to start while it names another model.
CREATE TABLE embedding_model (
    singleton boolean NOT NULL DEFAULT true,
    name text NOT NULL,
    PRIMARY KEY (singleton),
    CONSTRAINT ck_embedding_model_singleton CHECK (singleton)
);
