-- Migration 5: lots and evidence prices (RESOLVED layer, rebuilt by lots.build).
--
-- A lot is a quantity of one asset acquired by one event: the UTXO view of a position. Postings
-- that add to a position open a lot; postings that reduce it close lots by the account's lot
-- method (FIFO for now). Movements that net to zero inside one event (share journals) touch no
-- lots, so acquisition dates survive them. Shares held before history begins form an
-- 'opening' lot with unknown cost.

CREATE TABLE lot (
  id            TEXT PRIMARY KEY,
  account_id    TEXT NOT NULL REFERENCES account(id),
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  opened_txn    TEXT REFERENCES txn(id),     -- NULL for the opening lot
  opened_date   TEXT NOT NULL,               -- for the opening lot: the day before history
  quantity      TEXT NOT NULL,
  cost          TEXT,                        -- total, in cost_asset, fees included; NULL if unknown
  cost_asset    TEXT REFERENCES asset(id),
  remaining     TEXT NOT NULL
);
CREATE INDEX lot_acct ON lot(account_id, asset_id);

CREATE TABLE lot_close (
  lot_id        TEXT NOT NULL REFERENCES lot(id),
  txn_id        TEXT NOT NULL REFERENCES txn(id),
  date          TEXT NOT NULL,
  quantity      TEXT NOT NULL,               -- positive: units taken from the lot
  proceeds      TEXT,                        -- this lot's share of the event's cash, fees deducted
  cost          TEXT,                        -- this lot's share of its cost; NULL if unknown
  PRIMARY KEY (lot_id, txn_id)
);

-- Prices seen in the evidence. Market prices from outside sources go here too, later.
CREATE TABLE price (
  asset_id      TEXT NOT NULL REFERENCES asset(id),
  day           TEXT NOT NULL,
  price_asset   TEXT NOT NULL REFERENCES asset(id),
  value         TEXT NOT NULL,
  source        TEXT NOT NULL,               -- 'trade' | 'holding'
  ref           TEXT NOT NULL,               -- txn id or snapshot locator
  PRIMARY KEY (asset_id, day, source, ref)
);

-- Open position per account and asset, valued at the latest price on or before today.
CREATE VIEW holding AS
SELECT l.account_id, l.asset_id,
       sum(CAST(l.remaining AS REAL)) AS quantity_f,
       sum(CASE WHEN l.cost IS NULL THEN NULL
                ELSE CAST(l.cost AS REAL) * CAST(l.remaining AS REAL) / CAST(l.quantity AS REAL) END) AS cost_f,
       sum(l.cost IS NULL AND CAST(l.remaining AS REAL) > 0) AS lots_without_cost,
       (SELECT p.day FROM price p WHERE p.asset_id = l.asset_id ORDER BY p.day DESC, p.source LIMIT 1) AS price_day,
       (SELECT CAST(p.value AS REAL) FROM price p WHERE p.asset_id = l.asset_id ORDER BY p.day DESC, p.source LIMIT 1)
         * sum(CAST(l.remaining AS REAL)) AS value_f
FROM lot l
WHERE CAST(l.remaining AS REAL) != 0
GROUP BY l.account_id, l.asset_id;
