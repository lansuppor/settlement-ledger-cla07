import sqlite3
import uuid
from app.store.db import connect


class ReversalError(Exception):
    """冲正被拒绝；reason 给出可区分的拒绝原因。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


def add_payment(tenant: str, order_id: str, amount_cents: int, actor_id: str = "") -> dict | None:
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
        # 已收金额、订单状态与收款流水在同一事务内落库，任一失败整体回滚。
        next_seq = _next_seq(conn, tenant, order_id)
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO payment_flows(tenant, order_id, flow_id, type, amount_cents, origin_flow_id, actor_id, seq)"
            " VALUES(?,?,?,'payment',?,NULL,?,?)",
            (tenant, order_id, _new_flow_id(conn, tenant), amount_cents, actor_id, next_seq),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)


def list_flows(tenant: str, order_id: str) -> list[dict] | None:
    """按发生顺序返回订单的全部收款与冲正；订单不存在（含跨租户）返回 None。"""
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if exists is None:
            return None
        rows = conn.execute(
            "SELECT flow_id, type, amount_cents, origin_flow_id, actor_id, created_at"
            " FROM payment_flows WHERE tenant=? AND order_id=? ORDER BY seq",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_flow_dict(row) for row in rows]


def reverse_payment(tenant: str, order_id: str, flow_id: str, actor_id: str = "") -> dict:
    """冲正一笔已登记收款。成功返回冲正后的订单；失败抛 ReversalError，账面与流水均不变。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            raise ReversalError("order not found")
        flow = conn.execute(
            "SELECT type, amount_cents FROM payment_flows WHERE tenant=? AND flow_id=?",
            (tenant, flow_id),
        ).fetchone()
        # 先按不存在处理：流水缺失、属于其他订单、类型不是收款，对外同为 flow_not_found，
        # 不泄漏流水是否存在以及归属。
        if flow is None or flow["type"] != "payment" or not _flow_belongs_order(conn, tenant, flow_id, order_id):
            conn.execute("ROLLBACK")
            raise ReversalError("payment flow not found")
        already = conn.execute(
            "SELECT 1 FROM payment_flows WHERE tenant=? AND origin_flow_id=? AND type='reversal'",
            (tenant, flow_id),
        ).fetchone()
        if already is not None:
            conn.execute("ROLLBACK")
            raise ReversalError("payment already reversed")
        amount = flow["amount_cents"]
        new_paid = order["paid_cents"] - amount
        if new_paid < 0 or new_paid > order["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ReversalError("reversal would produce illegal balance")
        # 冲正后状态等价于该笔收款从未发生：仍有余款即未结清，恰好为 0 才是已结清。
        conn.execute(
            "UPDATE orders SET paid_cents = ?, status = CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
            " WHERE tenant=? AND order_id=?",
            (new_paid, new_paid, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO payment_flows(tenant, order_id, flow_id, type, amount_cents, origin_flow_id, actor_id, seq)"
            " VALUES(?,?,?,'reversal',?,?,?,?)",
            (tenant, order_id, _new_flow_id(conn, tenant), amount, flow_id, actor_id, _next_seq(conn, tenant, order_id)),
        )
        conn.execute("COMMIT")
    except ReversalError:
        raise
    except sqlite3.IntegrityError:
        # 并发重放穿过前面的检查时，唯一索引兜底：视为重复冲正，整笔回滚。
        conn.execute("ROLLBACK")
        raise ReversalError("payment already reversed")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    result = get(tenant, order_id)
    assert result is not None
    return result


def _flow_belongs_order(conn: sqlite3.Connection, tenant: str, flow_id: str, order_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM payment_flows WHERE tenant=? AND flow_id=? AND order_id=?",
        (tenant, flow_id, order_id),
    ).fetchone()
    return row is not None


def _next_seq(conn: sqlite3.Connection, tenant: str, order_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM payment_flows WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    return int(row["next"])


def _new_flow_id(conn: sqlite3.Connection, tenant: str) -> str:
    # 极小概率撞键时重试，避免把内部错误暴露给调用方。
    while True:
        flow_id = uuid.uuid4().hex
        clash = conn.execute(
            "SELECT 1 FROM payment_flows WHERE tenant=? AND flow_id=?",
            (tenant, flow_id),
        ).fetchone()
        if clash is None:
            return flow_id


def _flow_dict(row: sqlite3.Row) -> dict:
    return {
        "flow_id": row["flow_id"],
        "type": row["type"],
        "amount_cents": row["amount_cents"],
        "origin_flow_id": row["origin_flow_id"],
        "actor_id": row["actor_id"],
        "created_at": row["created_at"],
    }
