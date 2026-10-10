-- Create "clients" table
CREATE TABLE "clients" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "first_name" character varying(100) NOT NULL,
  "last_name" character varying(100) NOT NULL,
  "email" character varying(320) NOT NULL,
  "description" text NULL,
  "social_links" text[] NOT NULL DEFAULT '{}',
  "created_at" timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY ("id")
);
-- Create index "uq_clients_email_lower" to table: "clients"
CREATE UNIQUE INDEX "uq_clients_email_lower" ON "clients" ((lower((email)::text)));
