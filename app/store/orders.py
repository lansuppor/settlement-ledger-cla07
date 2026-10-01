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
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        paid_after = row["paid_cents"] + amount_cents
        status = "settled" if paid_after >= row["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (paid_after, status, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)

def _snapshot(tenant: str, reversal: sqlite3.Row) -> dict:
    """按冲正记录回放首次成功时的订单结果，不随后续账务变化。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT amount_cents, currency FROM orders WHERE tenant=? AND order_id=?",
            (tenant, reversal["order_id"]),
        ).fetchone()
    finally:
        conn.close()
    paid = reversal["paid_cents_after"]
    return {
        "tenant": tenant,
        "order_id": reversal["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": paid,
        "currency": row["currency"],
        "status": reversal["status_after"],
        "outstanding_cents": row["amount_cents"] - paid,
    }

def reverse_payment(
    tenant: str, order_id: str, reversal_id: str, amount_cents: int
) -> tuple[str, dict | None]:
    """Reverse a previously registered payment.

    返回 (结果类型, 订单)：
    - ("ok", order)：本次冲正成功并已落账
    - ("duplicate", order)：冲正标识曾成功提交，返回与首次一致的结果
    - ("mismatch", None)：冲正标识已存在，但订单或金额与首次不一致
    - ("exceeds", None)：冲正金额大于当前已收金额
    - ("not_found", None)：该租户名下订单不存在（含跨租户）
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT order_id, amount_cents, paid_cents_after, status_after FROM reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if existing is not None:
            if existing["order_id"] != order_id or existing["amount_cents"] != amount_cents:
                conn.execute("ROLLBACK")
                return ("mismatch", None)
            conn.execute("COMMIT")
            return ("duplicate", _snapshot(tenant, existing))

        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return ("not_found", None)
        if amount_cents <= 0 or amount_cents > row["paid_cents"]:
            conn.execute("ROLLBACK")
            return ("exceeds", None)

        paid_after = row["paid_cents"] - amount_cents
        status = "settled" if paid_after >= row["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (paid_after, status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO reversals(tenant, reversal_id, order_id, amount_cents, paid_cents_after, status_after) VALUES(?,?,?,?,?,?)",
            (tenant, reversal_id, order_id, amount_cents, paid_after, status),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return ("ok", get(tenant, order_id))
