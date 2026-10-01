import sqlite3
from app.store.db import connect

# 状态口径：已收金额达到订单金额为已结清，否则为未收清（含未登记收款）。
# 注意 SQLite 的 UPDATE 中对列的引用取更新前的值，故增量需显式代入。
_STATUS_ON_ADD = "CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
_STATUS_ON_REVERSE = "CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END"

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
        conn.execute(
            f"UPDATE orders SET paid_cents = paid_cents + ?, status = {_STATUS_ON_ADD} WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        paid_after = row["paid_cents"] + amount_cents
        conn.execute(
            "INSERT INTO payment_ledger(tenant, order_id, kind, amount_cents, paid_cents, outstanding_cents, status, reversal_id)"
            " VALUES(?,?,?,?,?,?,?,NULL)",
            (
                tenant,
                order_id,
                "payment",
                amount_cents,
                paid_after,
                row["amount_cents"] - paid_after,
                "settled" if paid_after >= row["amount_cents"] else "accepted",
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)

def reverse_payment(
    tenant: str, order_id: str, reversal_id: str, amount_cents: int
) -> tuple[dict | None, str]:
    """登记一次收款冲正。

    返回 (订单, 结果)：
    - (order, "applied")：本次冲正已生效；
    - (order, "duplicate")：冲正标识此前已成功提交，业务内容一致，按首次成功结果返回；
    - (None, "not_found")：订单在该租户下不存在（含属于其他租户）；
    - (None, "conflict")：冲正金额超过当前已收金额，或同一冲正标识的业务内容与首次不一致。
    所有路径在单个事务内完成，冲突时不改变任何账务。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT order_id, amount_cents FROM payment_reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if prior is not None:
            conn.execute("ROLLBACK")
            if prior["order_id"] == order_id and prior["amount_cents"] == amount_cents:
                return get(tenant, order_id), "duplicate"
            return None, "conflict"
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None, "not_found"
        if amount_cents <= 0 or amount_cents > row["paid_cents"]:
            conn.execute("ROLLBACK")
            return None, "conflict"
        conn.execute(
            "INSERT INTO payment_reversals(tenant, reversal_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, reversal_id, order_id, amount_cents),
        )
        conn.execute(
            f"UPDATE orders SET paid_cents = paid_cents - ?, status = {_STATUS_ON_REVERSE} WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        paid_after = row["paid_cents"] - amount_cents
        conn.execute(
            "INSERT INTO payment_ledger(tenant, order_id, kind, amount_cents, paid_cents, outstanding_cents, status, reversal_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                tenant,
                order_id,
                "reversal",
                amount_cents,
                paid_after,
                row["amount_cents"] - paid_after,
                "settled" if paid_after >= row["amount_cents"] else "accepted",
                reversal_id,
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id), "applied"

def list_ledger(tenant: str, order_id: str) -> list[dict] | None:
    """按生效先后返回订单的收款/冲正流水；订单不存在（含跨租户）时返回 None。"""
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if exists is None:
            return None
        rows = conn.execute(
            "SELECT entry_id, kind, amount_cents, paid_cents, outstanding_cents, status, reversal_id"
            " FROM payment_ledger WHERE tenant=? AND order_id=? ORDER BY entry_id",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
