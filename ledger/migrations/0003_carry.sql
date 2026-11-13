-- Migration 3: carry txn identity across fingerprint changes.
--
-- When a re-parse changes an observation's fingerprint (a parser fix), its link is dropped so
-- it can be matched afresh. The txn it used to belong to is parked here; after linking, a txn
-- left with no observations is merged into wherever its observation went, so annotations and
-- pins follow it instead of being orphaned.
CREATE TABLE txn_carry (
  obs_id        TEXT PRIMARY KEY,
  old_txn       TEXT NOT NULL REFERENCES txn(id)
);
