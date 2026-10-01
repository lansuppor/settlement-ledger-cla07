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
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            number = int(path.name.split("_", 1)[0])
            if number <= version:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version = {number}")
    finally:
        conn.close()
