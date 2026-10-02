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
    finally:
        conn.close()
