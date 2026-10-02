-- 收款登记请求级幂等：幂等键由调用方提供，在租户内唯一标识一次收款意图。
-- 仅在收款入账成功后随账面快照与流水标识一并落库；被拒绝的请求不写入本表，不占用幂等键。
-- 主键含租户：跨租户提交相同幂等键互不影响，任一方都读不到另一方的请求身份。
CREATE TABLE IF NOT EXISTS payment_idempotency(
  tenant TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  order_id TEXT NOT NULL,
  installment_id TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL,
  record_id TEXT NOT NULL,
  paid_cents INTEGER NOT NULL,
  outstanding_cents INTEGER NOT NULL,
  order_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, idempotency_key)
);
