-- 批量收款导入：批次（一次文件提交一行）与逐行结论（可续跑、可重放）
CREATE TABLE IF NOT EXISTS import_batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  originator TEXT NOT NULL,
  total_lines INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'processing' CHECK(status IN ('processing','completed')),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  PRIMARY KEY(tenant, batch_id)
);

-- 同一文件路径与发起方标识重复提交视为同一批次：唯一索引保证并发下也只建一个批次
CREATE UNIQUE INDEX IF NOT EXISTS idx_import_batches_dedup
  ON import_batches(tenant, file_path, originator);

CREATE TABLE IF NOT EXISTS import_batch_lines(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER,
  installment_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
  reject_reason TEXT,
  paid_cents INTEGER,
  outstanding_cents INTEGER,
  order_status TEXT,
  record_id TEXT,
  processed_at TEXT,
  PRIMARY KEY(tenant, batch_id, line_no),
  FOREIGN KEY(tenant, batch_id) REFERENCES import_batches(tenant, batch_id)
);
