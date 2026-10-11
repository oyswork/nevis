-- Modify "documents" table
ALTER TABLE "documents" ADD COLUMN "summary_attempts" integer NOT NULL DEFAULT 0, ADD COLUMN "summary_locked_until" timestamptz NULL;
-- Create index "ix_documents_unsummarized" to table: "documents"
CREATE INDEX "ix_documents_unsummarized" ON "documents" ("created_at") WHERE (summary IS NULL);
-- Summaries move to the worker, which finds documents itself; drop the pending jobs first, or
-- the narrower check below rejects them (and they would never run).
DELETE FROM "outbox" WHERE kind = 'summarize_document';
-- Modify "outbox" table
ALTER TABLE "outbox" DROP CONSTRAINT "outbox_kind_check", ADD CONSTRAINT "outbox_kind_check" CHECK (kind = ANY (ARRAY['index_client'::text, 'index_document'::text]));
