-- Migration 7: user annotations on accounts (USER layer, persisted in state.jsonl).
-- Feeds name accounts however they like (and ingest refreshes those names); what the user
-- knows about an account lives here and wins in the `accounts` view.
CREATE TABLE account_annotation (
  account_id    TEXT PRIMARY KEY,       -- may name an account a later file creates
  name          TEXT,
  institution   TEXT,
  closed        TEXT,                   -- day the account was closed, if it was
  note          TEXT,
  updated_at    TEXT NOT NULL
);

CREATE VIEW accounts AS
SELECT a.id, coalesce(n.name, a.name) AS name, coalesce(n.institution, a.institution) AS institution,
       a.kind, a.owned, n.closed, n.note
FROM account a LEFT JOIN account_annotation n ON n.account_id = a.id;
