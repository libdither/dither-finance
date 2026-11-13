-- Migration 6: manual account identity ("this feed's account is that account").
--
-- Some feeds can't name an account the way others do (Privacy.com knows a funding account
-- only by its own id and a nickname). A pin maps that feed key to an existing account. Pins
-- are user decisions: exported to state.jsonl and imported before parsing, so a scratch build
-- files observations under the right account from the start.
CREATE TABLE account_alias_pin (
  feed          TEXT NOT NULL,
  ext_key       TEXT NOT NULL,
  account_id    TEXT NOT NULL,          -- may name an account a later file creates
  created_at    TEXT NOT NULL,
  PRIMARY KEY (feed, ext_key)
);
