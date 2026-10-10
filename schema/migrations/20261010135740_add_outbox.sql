-- Create "outbox" table
CREATE TABLE "outbox" (
  "id" bigint NOT NULL GENERATED ALWAYS AS IDENTITY,
  "kind" text NOT NULL,
  "entity_id" uuid NOT NULL,
  "attempts" integer NOT NULL DEFAULT 0,
  "available_at" timestamptz NOT NULL DEFAULT now(),
  "locked_until" timestamptz NULL,
  "last_error" text NULL,
  "failed_at" timestamptz NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY ("id"),
  CONSTRAINT "outbox_kind_check" CHECK (kind = 'index_client'::text)
);
-- Create index "ix_outbox_pending" to table: "outbox"
CREATE INDEX "ix_outbox_pending" ON "outbox" ("available_at") WHERE (failed_at IS NULL);
