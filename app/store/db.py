import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# payment_records 重建后的索引定义（与 migrations/002 保持一致）
_PAYMENT_RECORD_INDEXES = [
    (
        "CREATE INDEX IF NOT EXISTS idx_payment_records_order "
        "ON payment_records(tenant, order_id, created_at, record_id)"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reversal_once "
        "ON payment_records(tenant, related_record_id) WHERE record_type = 'reversal'"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_refund_once "
        "ON payment_records(tenant, order_id, related_record_id, amount_cents, COALESCE(installment_id, '')) "
        "WHERE record_type = 'refund'"
    ),
]


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _evolve_payment_records(conn: sqlite3.Connection) -> None:
    """对存量库幂等演进 payment_records：补 installment_id 列、扩展 record_type CHECK 容纳 refund。"""
    if not _table_exists(conn, "payment_records"):
        return
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(payment_records)")]
    if "installment_id" not in columns:
        conn.execute("ALTER TABLE payment_records ADD COLUMN installment_id TEXT")

    # 旧库的 CHECK 只允许 ('payment','reversal')，SQLite 无法直接改约束，需整表重建（幂等）
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='payment_records'"
    ).fetchone()
    if schema is None or "'refund'" in (schema["sql"] or ""):
        return
    conn.execute("BEGIN")
    conn.execute(
        """CREATE TABLE payment_records_new(
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
           )"""
    )
    conn.execute(
        """INSERT INTO payment_records_new
             (tenant, order_id, record_id, record_type, amount_cents, originator,
              related_record_id, created_at, installment_id)
           SELECT tenant, order_id, record_id, record_type, amount_cents, originator,
                  related_record_id, created_at, installment_id
           FROM payment_records"""
    )
    # 旧表上的索引随 DROP 一并删除，重命名后按最新定义重建
    conn.execute("DROP TABLE payment_records")
    conn.execute("ALTER TABLE payment_records_new RENAME TO payment_records")
    for statement in _PAYMENT_RECORD_INDEXES:
        conn.execute(statement)
    conn.execute("COMMIT")


def _ensure_payment_record_columns(conn: sqlite3.Connection) -> None:
    """存量库在执行迁移脚本前先补齐 installment_id，避免脚本内退款唯一索引引用不到列。"""
    if not _table_exists(conn, "payment_records"):
        return
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(payment_records)")]
    if "installment_id" not in columns:
        conn.execute("ALTER TABLE payment_records ADD COLUMN installment_id TEXT")


def migrate() -> None:
    conn = connect()
    try:
        _ensure_payment_record_columns(conn)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        _evolve_payment_records(conn)
    finally:
        conn.close()
