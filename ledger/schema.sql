-- FinanceTracker ledger schema (sqlite). PRAGMA user_version tracks migrations (see db.py).
-- This is migration 1. Later changes live in migrations/: 0002 makes txns multi-account events
-- (txn_fact per event, entry per account, balanced postings), 0003 carries txn identity across
-- fingerprint changes, 0004 adds balance assertions and opening balances, 0005 lots and
-- evidence prices, 0006 account alias pins.
--
-- Three layers:
--   EVIDENCE   source_file, observation, obs_leg, obs_time, snapshot
--              What some source said, verbatim plus normalized. Re-derivable by re-parsing
--              the raw file, but kept even if the file is gone so the db can stand alone.
--   IDENTITY   account, account_alias, asset, txn, txn_link, link_pin
--              Which observations are the same real-world event. Minted once, never
--              recomputed from scratch on normal ingests: this is what makes merges stable.
--   RESOLVED   txn_fact, posting
--              Best-guess view of each txn computed from its linked observations +
--              annotations. Always safe to drop and recompute (`ledger resolve`).
--   USER       txn_annotation: categories, notes, field overrides. Never touched by ingest.
--
-- Conventions
--   * Quantities are exact decimal TEXT ('-12.34', '0.000000000000000001'); *_f REAL columns
--     are lossy shadows for fast aggregation only.
--   * Times carry precision. value is ISO-8601 truncated to its precision
--     ('2026', '2026-10', '2026-10-23', '2026-10-23T14:03:12Z'); tz is an IANA zone for
--     local values or 'UTC'; [lo, hi) is the unix-seconds interval the value could denote.
--   * Assets: 'fiat:USD', 'sec:ACME', later 'crypto:btc', 'crypto:eth', 'crypto:eth/erc20:0x…'.

------------------------------------------------------------------ EVIDENCE
CREATE TABLE source_file (
  sha256        TEXT PRIMARY KEY,
  path          TEXT NOT NULL,          -- last known path, relative to project root
  size          INTEGER NOT NULL,
  parser        TEXT,                   -- e.g. 'schwab_web_pdf'
  parser_version INTEGER,
  coverage_lo   TEXT,                   -- date range the source claims to be complete for
  coverage_hi   TEXT,
  meta          TEXT,                   -- JSON
  first_seen    TEXT NOT NULL,
  parsed_at     TEXT,
  present       INTEGER NOT NULL DEFAULT 1  -- 0 once the raw file disappears
);

CREATE TABLE observation (
  id            TEXT PRIMARY KEY,       -- hash(source sha256, locator): stable across parser versions
  source_sha    TEXT NOT NULL REFERENCES source_file(sha256),
  locator       TEXT NOT NULL,          -- where in the source: 'page=3;row=12', 'acct=…;txn=…'
  feed          TEXT NOT NULL,          -- dedup family: same feed => same fingerprint scheme
  account_id    TEXT NOT NULL REFERENCES account(id),
  fingerprint   TEXT NOT NULL,          -- within-feed identity; equal => same real txn
  ext_id        TEXT,                   -- source's own id, if any
  kind          TEXT,                   -- source's transaction type ('ACH', 'Buy', 'Reinvest Dividend')
  description   TEXT,
  payee         TEXT,
  memo          TEXT,
  pending       INTEGER NOT NULL DEFAULT 0,
  extra         TEXT,                   -- JSON: anything else (running balance, fees, mcc, …)
  raw           TEXT NOT NULL,          -- JSON: the source record as extracted
  UNIQUE (source_sha, locator)
);
CREATE INDEX observation_fp ON observation(fingerprint);
CREATE INDEX observation_acct ON observation(account_id);

CREATE TABLE obs_time (
  obs_id        TEXT NOT NULL REFERENCES observation(id) ON DELETE CASCADE,
  role          TEXT NOT NULL,          -- 'posted' | 'transacted' | 'settled' | 'block'
  value         TEXT NOT NULL,
  precision     TEXT NOT NULL CHECK (precision IN ('year','month','day','hour','minute','second','ms','us')),
  tz            TEXT,
  lo            INTEGER NOT NULL,
  hi            INTEGER NOT NULL,
  PRIMARY KEY (obs_id, role)
);

CREATE TABLE obs_leg (
  obs_id        TEXT NOT NULL REFERENCES observation(id) ON DELETE CASCADE,
  n             INTEGER NOT NULL,
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  quantity      TEXT NOT NULL,
  unit_price    TEXT,                   -- per unit, in price_asset
  price_asset   TEXT REFERENCES asset(id),
  role          TEXT NOT NULL DEFAULT 'main',  -- 'cash' | 'position' | 'fee' | 'main'
  PRIMARY KEY (obs_id, n)
);

-- Point-in-time balances and holdings (SimpleFIN balance, running balance, positions).
CREATE TABLE snapshot (
  source_sha    TEXT NOT NULL REFERENCES source_file(sha256),
  locator       TEXT NOT NULL,
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  kind          TEXT NOT NULL,          -- 'balance' | 'available' | 'position' | 'running'
  as_of         TEXT NOT NULL,
  as_of_prec    TEXT NOT NULL,
  as_of_lo      INTEGER NOT NULL,
  quantity      TEXT,
  value         TEXT,                   -- market value / cost basis in value_asset
  value_asset   TEXT REFERENCES asset(id),
  extra         TEXT,
  PRIMARY KEY (source_sha, locator)
);

------------------------------------------------------------------ IDENTITY
CREATE TABLE account (
  id            TEXT PRIMARY KEY,       -- 'acme-123', 'eth-0xabc…'
  institution   TEXT NOT NULL,
  name          TEXT,
  kind          TEXT,                   -- 'checking' | 'brokerage' | 'wallet' | …
  base_asset    TEXT REFERENCES asset(id),
  owned         INTEGER NOT NULL DEFAULT 1,
  meta          TEXT
);

-- How each feed refers to an account. Durable, so renames upstream don't fork accounts.
CREATE TABLE account_alias (
  feed          TEXT NOT NULL,
  ext_key       TEXT NOT NULL,
  account_id    TEXT NOT NULL REFERENCES account(id),
  PRIMARY KEY (feed, ext_key)
);

CREATE TABLE asset (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,          -- 'fiat' | 'security' | 'crypto'
  symbol        TEXT,
  name          TEXT,
  decimals      INTEGER,                -- smallest unit, informational (USD 2, ETH 18)
  meta          TEXT
);

CREATE TABLE txn (
  id            TEXT PRIMARY KEY,       -- 'tx_' + hash of the fingerprint that minted it
  minted_from   TEXT NOT NULL,          -- that fingerprint
  minted_at     TEXT NOT NULL,
  orphaned      INTEGER NOT NULL DEFAULT 0  -- 1 if all its observations vanished
);

CREATE TABLE txn_link (
  obs_id        TEXT PRIMARY KEY REFERENCES observation(id) ON DELETE CASCADE,
  txn_id        TEXT NOT NULL REFERENCES txn(id),
  method        TEXT NOT NULL,          -- 'mint' | 'fingerprint' | 'fuzzy' | 'pin'
  score         REAL,
  linked_at     TEXT NOT NULL
);
CREATE INDEX txn_link_txn ON txn_link(txn_id);

-- Manual matching decisions, keyed by fingerprint so they survive re-parses and rebuilds.
-- txn_id NULL means "never merge this into anything else".
CREATE TABLE link_pin (
  fingerprint   TEXT PRIMARY KEY,
  txn_id        TEXT REFERENCES txn(id),
  reason        TEXT,
  created_at    TEXT NOT NULL
);

------------------------------------------------------------------ USER
CREATE TABLE txn_annotation (
  txn_id        TEXT PRIMARY KEY REFERENCES txn(id),
  category      TEXT,
  tags          TEXT,                   -- JSON array
  note          TEXT,
  overrides     TEXT,                   -- JSON: {"date": "...", "description": "...", "hidden": true}
  updated_at    TEXT NOT NULL
);

------------------------------------------------------------------ RESOLVED (derived)
CREATE TABLE txn_fact (
  txn_id        TEXT PRIMARY KEY REFERENCES txn(id),
  account_id    TEXT NOT NULL REFERENCES account(id),
  date          TEXT NOT NULL,          -- local calendar day, for budgeting
  t_value       TEXT NOT NULL,          -- best-known occurrence time
  t_precision   TEXT NOT NULL,
  t_lo          INTEGER NOT NULL,
  t_hi          INTEGER NOT NULL,
  posted        TEXT,
  kind          TEXT,
  description   TEXT,
  payee         TEXT,
  amount        TEXT,                   -- net cash change in account base asset
  amount_f      REAL,
  pending       INTEGER NOT NULL,
  n_obs         INTEGER NOT NULL,
  feeds         TEXT NOT NULL           -- comma list of feeds that saw it
);
CREATE INDEX txn_fact_date ON txn_fact(account_id, date);

CREATE TABLE posting (
  txn_id        TEXT NOT NULL REFERENCES txn(id),
  n             INTEGER NOT NULL,
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  quantity      TEXT NOT NULL,
  quantity_f    REAL NOT NULL,
  unit_price    TEXT,
  price_asset   TEXT,
  role          TEXT NOT NULL,
  PRIMARY KEY (txn_id, n)
);

------------------------------------------------------------------ VIEWS
-- The default "transactions table": resolved facts with user annotations applied.
CREATE VIEW ledger AS
SELECT f.txn_id,
       f.account_id,
       coalesce(json_extract(a.overrides, '$.date'), f.date)               AS date,
       f.t_precision,
       coalesce(json_extract(a.overrides, '$.description'), f.description) AS description,
       f.kind,
       f.amount,
       f.amount_f,
       a.category,
       a.tags,
       a.note,
       f.pending,
       f.feeds,
       f.n_obs
FROM txn_fact f
LEFT JOIN txn_annotation a ON a.txn_id = f.txn_id
WHERE coalesce(json_extract(a.overrides, '$.hidden'), 0) = 0;

-- Every observation behind every txn, with the file it came from.
CREATE VIEW provenance AS
SELECT l.txn_id, l.method, l.score, o.id AS obs_id, o.feed, o.fingerprint,
       s.path, o.locator, s.parser, s.parser_version, o.description, o.raw
FROM txn_link l
JOIN observation o ON o.id = l.obs_id
JOIN source_file s ON s.sha256 = o.source_sha;
