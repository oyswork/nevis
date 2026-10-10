-- Modify "outbox" table
ALTER TABLE "outbox" DROP CONSTRAINT "outbox_kind_check", ADD CONSTRAINT "outbox_kind_check" CHECK (kind = ANY (ARRAY['index_client'::text, 'index_document'::text, 'summarize_document'::text]));
