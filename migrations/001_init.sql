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
