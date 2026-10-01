-- 结算单能力：订单累计已结算金额 + 独立结算单与分期收款记录
ALTER TABLE orders ADD COLUMN settled_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS settlements(
  tenant TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  received_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,            -- accepted（已受理，占用订单剩余可结算金额）/ reversed（已冲正，整体释放）
  created_at TEXT NOT NULL,
  reversed_at TEXT,
  PRIMARY KEY(tenant, settlement_id)
);

CREATE INDEX IF NOT EXISTS idx_settlements_order ON settlements(tenant, order_id);

-- 结算单的分期收款明细；冲正只允许在累计已收为 0 时进行，因此已冲正单据不会有收款行
CREATE TABLE IF NOT EXISTS settlement_payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant, settlement_id) REFERENCES settlements(tenant, settlement_id)
);

CREATE INDEX IF NOT EXISTS idx_settlement_payments_one ON settlement_payments(tenant, settlement_id);
