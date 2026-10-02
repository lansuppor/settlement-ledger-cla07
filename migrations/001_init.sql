CREATE TABLE IF NOT EXISTS orders(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id)
);

-- 收款与冲正流水：一笔订单的每笔收款（payment）与冲正（reversal）各占一行，只追加不修改。
CREATE TABLE IF NOT EXISTS payment_flows(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  flow_id TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('payment', 'reversal')),
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  -- 冲正行通过它指向被冲正的原收款流水；收款行为 NULL。
  origin_flow_id TEXT,
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  seq INTEGER NOT NULL,
  PRIMARY KEY(tenant, flow_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_flows_order ON payment_flows(tenant, order_id, seq);

-- 同一笔收款最多被冲正一次（仅冲正行参与约束）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_flows_origin
  ON payment_flows(tenant, origin_flow_id)
  WHERE type = 'reversal';
