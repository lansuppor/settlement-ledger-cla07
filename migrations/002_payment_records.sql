CREATE TABLE IF NOT EXISTS payment_records(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  record_type TEXT NOT NULL CHECK(record_type IN ('payment','reversal','refund')),
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  originator TEXT NOT NULL,
  related_record_id TEXT,
  created_at TEXT NOT NULL,
  installment_id TEXT,
  PRIMARY KEY(tenant, record_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_records_order
  ON payment_records(tenant, order_id, created_at, record_id);

-- 同一笔收款至多被冲正一次：reversal 行的 related_record_id 在该租户内唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_reversal_once
  ON payment_records(tenant, related_record_id)
  WHERE record_type = 'reversal';

-- 退款请求重放只生效一次：同一订单下，针对同一原收款、同一金额、同一期次的退款
-- 视为同一请求（整单退款期次以 '' 占位），唯一索引在并发下也保证不重复扣减、不重复留痕
CREATE UNIQUE INDEX IF NOT EXISTS idx_refund_once
  ON payment_records(tenant, order_id, related_record_id, amount_cents, COALESCE(installment_id, ''))
  WHERE record_type = 'refund';
