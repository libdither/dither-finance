-- Migration 4: balance assertions and opening balances (RESOLVED layer, rebuilt by reconcile).
--
-- Every snapshot a source gives is an assertion: replaying postings up to that day must give
-- that quantity. History starts after an account already held things, so each (account, asset)
-- gets an opening balance: its earliest assertion minus what the postings replay to by then.
-- Every later assertion then checks the identity layer (missing, duplicated or mis-merged rows).

CREATE TABLE opening_balance (
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  quantity      TEXT NOT NULL,          -- held before the first posting; counterparty equity:opening
  as_of         TEXT NOT NULL,          -- day before the earliest posting or assertion
  basis         TEXT NOT NULL,          -- which assertion kind fixed it
  PRIMARY KEY (account_id, asset_id)
);

CREATE TABLE balance_check (
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  day           TEXT NOT NULL,          -- end of this local day
  kind          TEXT NOT NULL,          -- 'running' | 'balance' | 'position' | 'absent' | 'cash'
  source_sha    TEXT NOT NULL,
  locator       TEXT NOT NULL,
  expected      TEXT NOT NULL,          -- what the source says
  computed      TEXT NOT NULL,          -- opening + postings through `day`
  ok            INTEGER NOT NULL,
  lag           INTEGER NOT NULL DEFAULT 0,  -- 1 if it only matched the previous day (posting lag)
  PRIMARY KEY (account_id, asset_id, day, kind, source_sha, locator)
);

-- Quantity held per account and asset after each day that has postings.
CREATE VIEW balance AS
SELECT account_id, asset_id, date,
       (SELECT coalesce(sum(CAST(o.quantity AS REAL)), 0) FROM opening_balance o
         WHERE o.account_id = d.account_id AND o.asset_id = d.asset_id)
       + sum(sum(quantity_f)) OVER (PARTITION BY account_id, asset_id ORDER BY date) AS quantity_f
FROM posting d
JOIN account a ON a.id = d.account_id AND a.owned
GROUP BY account_id, asset_id, date;
