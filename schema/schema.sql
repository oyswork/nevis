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
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Emails are unique regardless of case: John.Doe@x.com and john.doe@x.com are the same person.
CREATE UNIQUE INDEX uq_clients_email_lower ON clients (lower(email));
