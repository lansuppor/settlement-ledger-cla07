CREATE TABLE IF NOT EXISTS payment_records(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  record_type TEXT NOT NULL CHECK(record_type IN ('payment','reversal')),
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  originator TEXT NOT NULL,
  related_record_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, record_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_records_order
  ON payment_records(tenant, order_id, created_at, record_id);

-- 同一笔收款至多被冲正一次：reversal 行的 related_record_id 在该租户内唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_reversal_once
  ON payment_records(tenant, related_record_id)
  WHERE record_type = 'reversal';
