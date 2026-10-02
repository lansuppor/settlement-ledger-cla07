CREATE TABLE IF NOT EXISTS installments(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  installment_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  due_at TEXT NOT NULL,
  paid INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, installment_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 收款流水永久记录其收讫的期次，冲正后期次回到未收但该因果链不丢失
ALTER TABLE payment_records ADD COLUMN installment_id TEXT;
