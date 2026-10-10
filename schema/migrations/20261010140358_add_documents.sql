-- Modify "outbox" table
ALTER TABLE "outbox" DROP CONSTRAINT "outbox_kind_check", ADD CONSTRAINT "outbox_kind_check" CHECK (kind = ANY (ARRAY['index_client'::text, 'index_document'::text]));
-- Create "documents" table
CREATE TABLE "documents" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "client_id" uuid NOT NULL,
  "title" character varying(200) NOT NULL,
  "content" text NOT NULL,
  "summary" text NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY ("id"),
  CONSTRAINT "fk_documents_client" FOREIGN KEY ("client_id") REFERENCES "clients" ("id") ON UPDATE NO ACTION ON DELETE CASCADE
);
-- Create index "ix_documents_client_id" to table: "documents"
CREATE INDEX "ix_documents_client_id" ON "documents" ("client_id");
