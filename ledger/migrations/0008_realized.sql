-- Migration 8: the broker's realized gain/loss rows (EVIDENCE), and what lots learn from them.
--
-- Schwab closes mutual funds at average cost and everything else FIFO (lots.py), so what remains
-- of a lot's cost is no longer always its pro-rata share: lot.remaining_cost holds it.
-- lot.cost_source says where a cost came from when it wasn't simply the cash of the opening event:
-- 'realized' = solved from the broker's realized rows (the opening lot); 'wash' = includes a loss
-- disallowed on an earlier sale.
CREATE TABLE realized_close (
  source_sha    TEXT NOT NULL REFERENCES source_file(sha256),
  locator       TEXT NOT NULL,
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  closed_date   TEXT NOT NULL,
  quantity      TEXT NOT NULL,
  closing_price TEXT,
  method        TEXT,                        -- as the page labels it; see lots.py before trusting it
  proceeds      TEXT NOT NULL,
  cost          TEXT NOT NULL,
  gain          TEXT NOT NULL,               -- as printed: includes the disallowed loss
  long_term     TEXT,
  short_term    TEXT,
  disallowed    TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY (source_sha, locator)
);
CREATE INDEX realized_close_asset ON realized_close(account_id, asset_id, closed_date);

ALTER TABLE lot ADD COLUMN remaining_cost TEXT;
ALTER TABLE lot ADD COLUMN cost_source TEXT;

DROP VIEW holding;
CREATE VIEW holding AS
SELECT l.account_id, l.asset_id,
       sum(CAST(l.remaining AS REAL)) AS quantity_f,
       sum(CAST(l.remaining_cost AS REAL)) AS cost_f,
       sum(l.remaining_cost IS NULL AND CAST(l.remaining AS REAL) > 0) AS lots_without_cost,
       (SELECT p.day FROM price p WHERE p.asset_id = l.asset_id ORDER BY p.day DESC, p.source LIMIT 1) AS price_day,
       (SELECT CAST(p.value AS REAL) FROM price p WHERE p.asset_id = l.asset_id ORDER BY p.day DESC, p.source LIMIT 1)
         * sum(CAST(l.remaining AS REAL)) AS value_f
FROM lot l
WHERE CAST(l.remaining AS REAL) != 0
GROUP BY l.account_id, l.asset_id;
