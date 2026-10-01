-- 退款能力：订单累计已退金额 + 独立退款单
ALTER TABLE orders ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,            -- accepted（已受理）/ reversed（已冲正）
  created_at TEXT NOT NULL,
  reversed_at TEXT,
  PRIMARY KEY(tenant, refund_id)
);

-- 同一退款单只允许一次成功的受理/冲正状态推进
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(tenant, order_id);
