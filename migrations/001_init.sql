CREATE TABLE IF NOT EXISTS orders(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id)
);

CREATE TABLE IF NOT EXISTS payment_reversals(
  tenant TEXT NOT NULL,
  reversal_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  PRIMARY KEY(tenant, reversal_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 收款流水：每次成功生效的收款登记或冲正追加且仅追加一条，按 seq 升序即生效先后。
-- 同类型多次操作以各自的 entry_id 区分；冲正条目的 reversal_id 关联当次冲正标识。
-- paid/outstanding/status 为该次操作生效后的账务快照，写入后不再随后续操作改写。
-- occurred_at 为该次业务的业务发生时间（带时区偏移、精确到秒），写入后不再改写。
CREATE TABLE IF NOT EXISTS order_flow(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  entry_id TEXT NOT NULL,
  entry_type TEXT NOT NULL CHECK(entry_type IN ('payment','reversal')),
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL,
  outstanding_cents INTEGER NOT NULL,
  status TEXT NOT NULL,
  reversal_id TEXT,
  occurred_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, seq),
  UNIQUE(tenant, order_id, entry_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_order_flow_order ON order_flow(tenant, order_id, seq);
