CREATE TABLE IF NOT EXISTS installments(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  installment_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unpaid' CHECK(status IN ('unpaid','paid')),
  paid_record_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, installment_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);
