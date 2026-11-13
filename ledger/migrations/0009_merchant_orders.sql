-- Migration 9: orders as merchants report them (EVIDENCE), e.g. Amazon order history.
--
-- Not money movements: what a charge on a card was for. A read-time matcher (merchants.py)
-- links each order to the card rows that paid for it, by amount and date. The newest report
-- of an order wins (several pulls restate it).
CREATE TABLE merchant_order (
  source_sha    TEXT NOT NULL REFERENCES source_file(sha256),
  merchant      TEXT NOT NULL,               -- 'amazon'
  order_id      TEXT NOT NULL,
  placed        TEXT NOT NULL,               -- day the order was placed
  total         TEXT,                        -- grand total, as printed
  status        TEXT,
  ship_to       TEXT,                        -- recipient name (no street address)
  payment       TEXT,                        -- 'Visa 1234': brand and last digits
  items         TEXT NOT NULL,               -- JSON [{title, asin, price, qty, seller}]
  detail        TEXT,                        -- JSON {subtotal, shipping, tax, ...} when the details page was read
  pulled_at     TEXT NOT NULL,
  PRIMARY KEY (source_sha, merchant, order_id)
);
CREATE INDEX merchant_order_id ON merchant_order(merchant, order_id, pulled_at);
