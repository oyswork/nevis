-- Desired database state. Atlas diffs the live database against this file and applies the
-- difference (`atlas schema apply`); omymodels generates src/nevis/db_models.py from it.
-- Spell timestamps "timestamp with time zone": omymodels doesn't map the "timestamptz" alias.

CREATE TABLE clients (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    first_name varchar(100) NOT NULL,
    last_name varchar(100) NOT NULL,
    email varchar(320) NOT NULL,
    description text,
    social_links text[] NOT NULL DEFAULT '{}',
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Emails are unique regardless of case: John.Doe@x.com and john.doe@x.com are the same person.
CREATE UNIQUE INDEX uq_clients_email_lower ON clients (lower(email));
