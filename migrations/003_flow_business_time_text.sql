-- 业务发生时间的登记写法：读回时保留登记时的时区偏移写法（如 2026-10-02T08:30:00+08:00）。
-- business_time 列仍为统一 UTC 文本，供区间比较与排序；business_time_text 为读回用文本。
ALTER TABLE order_flow ADD COLUMN business_time_text TEXT NOT NULL DEFAULT '';

-- 既有流水登记于本列引入之前、统一以 UTC 写法记账，其读回写法与 UTC 文本一致。
UPDATE order_flow SET business_time_text = business_time WHERE business_time_text = '';
