-- 业务发生时间：每次成功生效的收款登记或冲正都记录一个，最小精确到秒、带时区偏移。
-- 写入前在应用层统一换算为 UTC 后以 ISO-8601 字符串存储（如 2026-10-02T08:30:00+00:00），
-- 同一订单内按 business_time 比较即对应业务发生先后；带偏移的完整时间可直接字典序比较。
ALTER TABLE order_flow ADD COLUMN business_time TEXT NOT NULL DEFAULT '';

-- 范围查询以 business_time 为键，先按租户/订单限定，再按业务时间过滤并按 seq 排列。
CREATE INDEX IF NOT EXISTS idx_order_flow_time ON order_flow(tenant, order_id, business_time, seq);
