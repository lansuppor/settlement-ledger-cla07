CREATE TABLE IF NOT EXISTS import_batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  originator TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed')),
  total_lines INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY(tenant, batch_id)
);

-- 同一租户下，同一文件路径与发起方标识的重复提交视为同一批次：
-- 重复提交命中该唯一索引后返回首次导入的结论，不重复记账、不重复留痕
CREATE UNIQUE INDEX IF NOT EXISTS idx_import_batch_dedup
  ON import_batches(tenant, file_path, originator);

-- 逐行受理结论：每行一条，成功行记录受理后账面快照与收款流水标识，
-- 失败行记录与单笔收款一致的可区分拒绝原因；批次中断后可凭批次标识查询已完成行
CREATE TABLE IF NOT EXISTS import_rows(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  order_id TEXT,
  installment_id TEXT,
  outcome TEXT NOT NULL CHECK(outcome IN ('accepted','rejected')),
  reason TEXT,
  record_id TEXT,
  paid_cents INTEGER,
  outstanding_cents INTEGER,
  order_status TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, batch_id, line_no)
);
