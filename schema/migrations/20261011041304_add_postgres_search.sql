-- Modify "clients" table
ALTER TABLE "clients" ADD COLUMN "search_text" text NOT NULL DEFAULT '';
-- Create index "ix_clients_search_text_trgm" to table: "clients"
CREATE INDEX "ix_clients_search_text_trgm" ON "clients" USING GIN ("search_text" gin_trgm_ops);
-- Modify "documents" table
ALTER TABLE "documents" ADD COLUMN "tsv" tsvector NOT NULL GENERATED ALWAYS AS (to_tsvector('english'::regconfig, (((title)::text || ' '::text) || content))) STORED;
-- Create index "ix_documents_tsv" to table: "documents"
CREATE INDEX "ix_documents_tsv" ON "documents" USING GIN ("tsv");
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
