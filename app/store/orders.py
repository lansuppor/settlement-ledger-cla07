import sqlite3

from app.store import payments as payment_store
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
        if row is None:
            return None
        # 收款流水随订单一起暴露，按登记顺序排列
        flows = payment_store.list_for_order(tenant, order_id, conn=conn)
    finally:
        conn.close()
    return order_view(row, flows)

def order_view(row: sqlite3.Row, payment_flows: list[dict] | None = None) -> dict:
    amount = row["amount_cents"]
    outstanding = amount - row["paid_cents"]
    refundable = amount - row["refunded_cents"]
    settleable = amount - row["settled_cents"]
    return {
        **dict(row),
        "outstanding_cents": outstanding,
        "refundable_cents": refundable,
        "settleable_cents": settleable,
        "payments": payment_flows or [],
    }
