-- Create "clients" table
CREATE TABLE "clients" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "first_name" character varying(100) NOT NULL,
  "last_name" character varying(100) NOT NULL,
  "email" character varying(320) NOT NULL,
  "description" text NULL,
  "social_links" text[] NOT NULL DEFAULT '{}',
  "search_text" text NOT NULL DEFAULT '',
  "created_at" timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY ("id")
);
-- Create index "ix_clients_search_text_trgm" to table: "clients"
CREATE INDEX "ix_clients_search_text_trgm" ON "clients" USING GIN ("search_text" gin_trgm_ops);
-- Create index "uq_clients_email_lower" to table: "clients"
CREATE UNIQUE INDEX "uq_clients_email_lower" ON "clients" ((lower((email)::text)));
-- Create "embedding_model" table
CREATE TABLE "embedding_model" (
  "singleton" boolean NOT NULL DEFAULT true,
  "name" text NOT NULL,
  PRIMARY KEY ("singleton"),
  CONSTRAINT "ck_embedding_model_singleton" CHECK (singleton)
);
-- Create "documents" table
CREATE TABLE "documents" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "client_id" uuid NOT NULL,
  "title" character varying(200) NOT NULL,
  "content" text NOT NULL,
  "summary" text NULL,
  "summary_attempts" integer NOT NULL DEFAULT 0,
  "summary_locked_until" timestamptz NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  "tsv" tsvector NOT NULL GENERATED ALWAYS AS (to_tsvector('english'::regconfig, (((title)::text || ' '::text) || content))) STORED,
  PRIMARY KEY ("id"),
  CONSTRAINT "fk_documents_client" FOREIGN KEY ("client_id") REFERENCES "clients" ("id") ON UPDATE NO ACTION ON DELETE CASCADE
);
-- Create index "ix_documents_client_id" to table: "documents"
CREATE INDEX "ix_documents_client_id" ON "documents" ("client_id");
-- Create index "ix_documents_tsv" to table: "documents"
CREATE INDEX "ix_documents_tsv" ON "documents" USING GIN ("tsv");
-- Create index "ix_documents_unsummarized" to table: "documents"
CREATE INDEX "ix_documents_unsummarized" ON "documents" ("created_at") WHERE (summary IS NULL);
-- Create "document_chunks" table
CREATE TABLE "document_chunks" (
  "document_id" uuid NOT NULL,
  "ordinal" integer NOT NULL,
  "text" text NOT NULL,
  "embedding" vector(384) NOT NULL,
  PRIMARY KEY ("document_id", "ordinal"),
  CONSTRAINT "fk_document_chunks_document" FOREIGN KEY ("document_id") REFERENCES "documents" ("id") ON UPDATE NO ACTION ON DELETE CASCADE
);
-- Create index "ix_document_chunks_embedding" to table: "document_chunks"
CREATE INDEX "ix_document_chunks_embedding" ON "document_chunks" USING HNSW ("embedding" vector_cosine_ops);
