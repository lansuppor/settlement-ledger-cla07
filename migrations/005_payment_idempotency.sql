-- 收款登记的请求级幂等留痕：仅在收款成功入账的同一事务内写入。
-- 幂等键在租户内唯一标识一次收款意图，与服务端生成的收款流水标识相互独立；
-- 失败的收款请求随事务回滚，不占用幂等键、不留任何痕迹。
CREATE TABLE IF NOT EXISTS payment_idempotency(
  tenant TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  installment_id TEXT,
  record_id TEXT NOT NULL,
  -- 首次成功受理后的订单账面快照：重放请求返回与首次一致的结果
  paid_cents INTEGER NOT NULL,
  outstanding_cents INTEGER NOT NULL,
  status TEXT NOT NULL,
  originator TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, idempotency_key),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id),
  FOREIGN KEY(tenant, record_id) REFERENCES payment_records(tenant, record_id)
);
