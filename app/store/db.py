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
        # 记录已应用的迁移，非幂等脚本（如 ALTER TABLE）只执行一次
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY)")
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(name) VALUES(?)", (path.name,))
    finally:
        conn.close()
