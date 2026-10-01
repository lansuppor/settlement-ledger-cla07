-- 订单收款分期能力：逐笔收款流水（可冲正）+ 订单“曾结清”标记
-- 累计已收仍以 orders.paid_cents 为准；每笔有效（未冲正）流水金额之和恒等于 paid_cents。

CREATE TABLE IF NOT EXISTS order_payments(
  tenant TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,           -- 该笔收款金额（最小货币单位整数，> 0）
  status TEXT NOT NULL,                    -- accepted（有效）/ reversed（已冲正）
  seq INTEGER NOT NULL,                    -- 租户内单调递增的登记序号，决定登记顺序
  created_at TEXT NOT NULL,                -- 登记时刻
  reversed_at TEXT,                        -- 冲正时刻
  idempotency_key TEXT,                    -- 登记幂等键（可空），超时重试命中同一次结论
  PRIMARY KEY(tenant, payment_id)
);

-- 登记幂等：同一（租户，幂等键）只产生一条流水
CREATE UNIQUE INDEX IF NOT EXISTS idx_order_payments_idem
  ON order_payments(tenant, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- 按订单列出流水 / 按登记顺序稳定分页
CREATE INDEX IF NOT EXISTS idx_order_payments_order
  ON order_payments(tenant, order_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_order_payments_seq
  ON order_payments(tenant, seq);

-- 该订单是否曾结清（未收曾为 0）。冲正使未收重新大于 0 时保持为 1，
-- 用于把“冲正导致的重新未结清”与“从未结清”区分开。
ALTER TABLE orders ADD COLUMN ever_settled INTEGER NOT NULL DEFAULT 0;

-- 回填：迁移前已收清的存量订单视为“曾结清”
UPDATE orders SET ever_settled=1 WHERE paid_cents >= amount_cents;
