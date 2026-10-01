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
    """按编号顺序应用 migrations 目录下的 SQL，已应用的编号记录在 schema_migrations 中。

    每个编号只应用一次：SQLite 的 executescript 在执行前自行提交，DDL 按语句即时落库，
    随后登记编号；编号只增不改，已落库的流水结构与数据不被重写。
    """
    conn = connect()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)")
        applied = {int(r["version"]) for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in applied:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
            applied.add(version)
    finally:
        conn.close()
