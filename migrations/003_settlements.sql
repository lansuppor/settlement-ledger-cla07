-- 结算能力：订单累计已结算金额 + 独立结算单（支持分期收款与冲正）
ALTER TABLE orders ADD COLUMN settled_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS settlements(
  tenant TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,       -- 结算金额（受理时占用的订单剩余结算金额）
  received_cents INTEGER NOT NULL DEFAULT 0,  -- 累计已收（分期收款累加）
  currency TEXT NOT NULL,
  status TEXT NOT NULL,                -- accepted（已受理）/ reversed（已冲正）
  created_at TEXT NOT NULL,
  reversed_at TEXT,
  PRIMARY KEY(tenant, settlement_id)
);

-- 按订单汇总在途结算单，冲正与核对时定位
CREATE INDEX IF NOT EXISTS idx_settlements_order ON settlements(tenant, order_id);
