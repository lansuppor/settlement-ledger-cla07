import sqlite3

from app.store.db import connect


def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, refunded_cents, settled_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return order_view(row)

def order_view(row: sqlite3.Row) -> dict:
    amount = row["amount_cents"]
    outstanding = amount - row["paid_cents"]
    refundable = amount - row["refunded_cents"]
    settleable = amount - row["settled_cents"]
    return {
        **dict(row),
        "outstanding_cents": outstanding,
        "refundable_cents": refundable,
        "settleable_cents": settleable,
    }
