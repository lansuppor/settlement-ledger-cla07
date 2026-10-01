-- 订单收款分期能力：逐笔收款流水 + 逐笔冲正 + 幂等登记键
-- paid_cents 始终等于该订单有效（未冲正）流水金额之和，由应用在同一事务内维护
CREATE TABLE IF NOT EXISTS order_payments(
  tenant TEXT NOT NULL,
  seq INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,  -- 登记顺序（仅与同租户内比较）
  payment_id TEXT NOT NULL,                         -- 成功登记后暴露的收款流水标识
  idempotency_key TEXT NOT NULL,                    -- 登记请求的客户幂等键
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,                    -- 该笔收款金额（>0）
  status TEXT NOT NULL DEFAULT 'accepted',          -- accepted（未冲正）/ reversed（已冲正）
  created_at TEXT NOT NULL,                         -- 登记时刻
  reversed_at TEXT,
  UNIQUE(tenant, payment_id),
  UNIQUE(tenant, idempotency_key)
);

-- 按订单读取流水（订单查询结果内的 payments 走此索引）
CREATE INDEX IF NOT EXISTS idx_order_payments_order
  ON order_payments(tenant, order_id, seq);

-- 条件检索的主过滤索引：租户隔离 + 登记顺序稳定排列
CREATE INDEX IF NOT EXISTS idx_order_payments_search
  ON order_payments(tenant, seq);
