-- Migration 2: txns become events that can span accounts (double entry).
--
-- A txn is one real-world event. Each observation attests to the postings of the account it
-- was seen from; an own-account transfer is one txn with a posting in each account, and a
-- Privacy.com card charge is a second observation of the checking debit it caused.
-- Resolve balances every event: whatever no owned account absorbs goes to an external
-- account ('ext:uncategorized', or 'ext:<category>' once annotated), so a category is just
-- the counterparty of the event.
--
-- The RESOLVED tables are dropped and rebuilt (they are derived); identity gains merged_into.

ALTER TABLE txn ADD COLUMN merged_into TEXT REFERENCES txn(id);  -- set when absorbed by another txn

DROP VIEW ledger;
DROP TABLE posting;
DROP TABLE txn_fact;

-- One row per event.
CREATE TABLE txn_fact (
  txn_id        TEXT PRIMARY KEY REFERENCES txn(id),
  date          TEXT NOT NULL,          -- local calendar day of the earliest entry
  t_value       TEXT NOT NULL,
  t_precision   TEXT NOT NULL,
  t_lo          INTEGER NOT NULL,
  t_hi          INTEGER NOT NULL,
  kind          TEXT,
  description   TEXT,
  flow          TEXT NOT NULL,          -- 'transfer': >1 owned account, nothing leaves
                                        -- 'internal': one account, nothing leaves (trades, reinvests)
                                        -- 'external': something crosses the ownership boundary
  accounts      TEXT NOT NULL,          -- comma list of owned accounts touched
  pending       INTEGER NOT NULL,
  n_obs         INTEGER NOT NULL,
  feeds         TEXT NOT NULL
);
CREATE INDEX txn_fact_date ON txn_fact(date);

-- One row per (event, owned account): the account's own view, like a statement line.
CREATE TABLE entry (
  txn_id        TEXT NOT NULL REFERENCES txn(id),
  account_id    TEXT NOT NULL REFERENCES account(id),
  date          TEXT NOT NULL,          -- local calendar day this account saw it
  t_precision   TEXT NOT NULL,
  kind          TEXT,
  description   TEXT,
  payee         TEXT,
  amount        TEXT NOT NULL,          -- net change in the account's base asset
  amount_f      REAL NOT NULL,
  counterparty  TEXT NOT NULL,          -- comma list of the event's other accounts (owned or ext:)
  pending       INTEGER NOT NULL,
  n_obs         INTEGER NOT NULL,
  feeds         TEXT NOT NULL,
  PRIMARY KEY (txn_id, account_id)
);
CREATE INDEX entry_date ON entry(account_id, date);

-- Balanced postings. Per event, sum(weight) = 0 for every asset, where weight is
-- quantity * unit_price (in price_asset) for priced legs and quantity otherwise.
CREATE TABLE posting (
  txn_id        TEXT NOT NULL REFERENCES txn(id),
  n             INTEGER NOT NULL,
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  quantity      TEXT NOT NULL,
  quantity_f    REAL NOT NULL,
  unit_price    TEXT,
  price_asset   TEXT,
  role          TEXT NOT NULL,          -- leg role, or 'counter' for the balancing posting
  date          TEXT NOT NULL,          -- local day of this posting (a transfer can take days)
  obs_id        TEXT,                   -- the observation it came from; NULL for 'counter'
  PRIMARY KEY (txn_id, n)
);
CREATE INDEX posting_acct ON posting(account_id, asset_id);

-- The default "transactions table": one row per owned-account entry, annotations applied.
CREATE VIEW ledger AS
SELECT e.txn_id,
       e.account_id,
       coalesce(json_extract(a.overrides, '$.date'), e.date)               AS date,
       e.t_precision,
       coalesce(json_extract(a.overrides, '$.description'), e.description) AS description,
       e.kind,
       e.amount,
       e.amount_f,
       f.flow,
       e.counterparty,
       a.category,
       a.tags,
       a.note,
       e.pending,
       e.feeds,
       e.n_obs
FROM entry e
JOIN txn_fact f ON f.txn_id = e.txn_id
LEFT JOIN txn_annotation a ON a.txn_id = e.txn_id
WHERE coalesce(json_extract(a.overrides, '$.hidden'), 0) = 0;
