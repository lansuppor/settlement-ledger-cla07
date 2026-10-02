import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        # 分期收款：流水需记录对应期次；ALTER 无 IF NOT EXISTS，按现状幂等补齐
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(payment_records)")]
        if "installment_id" not in columns:
            conn.execute("ALTER TABLE payment_records ADD COLUMN installment_id TEXT")
        # 退款流水复用同一张表：旧库的 record_type CHECK 不含 'refund'，
        # SQLite 无法直接修改约束，按官方推荐的建-拷-删-改名方式一次性重建（幂等）
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='payment_records'"
        ).fetchone()["sql"]
        if "'refund'" not in schema:
            conn.executescript(
                """
                CREATE TABLE payment_records_new(
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
                INSERT INTO payment_records_new(tenant, order_id, record_id, record_type, amount_cents,
                                                originator, related_record_id, created_at, installment_id)
                SELECT tenant, order_id, record_id, record_type, amount_cents,
                       originator, related_record_id, created_at, installment_id
                FROM payment_records;
                DROP TABLE payment_records;
                ALTER TABLE payment_records_new RENAME TO payment_records;
                CREATE INDEX IF NOT EXISTS idx_payment_records_order
                  ON payment_records(tenant, order_id, created_at, record_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reversal_once
                  ON payment_records(tenant, related_record_id)
                  WHERE record_type = 'reversal';
                """
            )
    finally:
        conn.close()
