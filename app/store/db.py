import sqlite3
from pathlib import Path
from app.config import db_path

SCHEMA = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"

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
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        # 兼容升级前已存在的库：为旧的 order_flow 表补上业务发生时间列。
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(order_flow)").fetchall()]
        if "occurred_at" not in columns:
            conn.execute("ALTER TABLE order_flow ADD COLUMN occurred_at TEXT NOT NULL DEFAULT ''")
    finally:
        conn.close()
