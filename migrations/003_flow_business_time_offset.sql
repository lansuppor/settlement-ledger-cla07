-- 登记时的时区偏移（分钟），用于在读回时保留登记时的偏移写法。
-- 与 business_time（UTC 基准时刻）成对写入：按基准时刻比较与去重，按本列渲染回写时的写法，
-- 同一时刻的不同写法不会重复记账，也不影响闭区间取数。
-- 历史流水在迁移前只存有 UTC 文本，偏移按 +00:00（0 分钟）处理，写法保持不变。
ALTER TABLE order_flow ADD COLUMN business_time_offset INTEGER NOT NULL DEFAULT 0;
