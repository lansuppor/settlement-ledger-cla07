import sqlite3
from app.store.db import connect

# 状态口径：已收金额达到订单金额为已结清，否则为未收清（含未登记收款）。
# 注意 SQLite 的 UPDATE 中对列的引用取更新前的值，故增量需显式代入。
_STATUS_ON_ADD = "CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
_STATUS_ON_REVERSE = "CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END"

def _settled(paid_cents: int, amount_cents: int) -> str:
    return "settled" if paid_cents >= amount_cents else "accepted"

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

def _next_flow_seq(conn: sqlite3.Connection, tenant: str, order_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM order_flow WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    return int(row["next_seq"])

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
        # 收款生效后在同一事务内追加流水；失败路径已回滚，不会留下流水。
        paid_after = row["paid_cents"] + amount_cents
        seq = _next_flow_seq(conn, tenant, order_id)
        conn.execute(
            "INSERT INTO order_flow(tenant, order_id, seq, entry_id, entry_type, amount_cents,"
            " paid_cents, outstanding_cents, status, reversal_id) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (
                tenant, order_id, seq, f"pay-{seq}", "payment", amount_cents,
                paid_after, row["amount_cents"] - paid_after, _settled(paid_after, row["amount_cents"]),
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
    所有路径在单个事务内完成，冲突时不改变任何账务；仅 applied 在末尾追加一条冲正流水，
    重复提交与被拒绝的请求不产生新流水。
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
        # 冲正生效后在同一事务内追加流水，并以 reversal_id 关联当次冲正。
        paid_after = row["paid_cents"] - amount_cents
        seq = _next_flow_seq(conn, tenant, order_id)
        conn.execute(
            "INSERT INTO order_flow(tenant, order_id, seq, entry_id, entry_type, amount_cents,"
            " paid_cents, outstanding_cents, status, reversal_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                tenant, order_id, seq, f"rev-{seq}", "reversal", amount_cents,
                paid_after, row["amount_cents"] - paid_after, _settled(paid_after, row["amount_cents"]),
                reversal_id,
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id), "applied"

def list_flow(tenant: str, order_id: str) -> list[dict] | None:
    """按生效先后返回该订单在本租户下的收款流水。

    订单不存在或属于其他租户时返回 None（调用方按 404 处理，不泄漏对象是否存在）。
    流水只追加、不重排不改写；本查询仅命中 (tenant, order_id) 主键维度，无法越界读取其他租户。
    """
    conn = connect()
    try:
        owned = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if owned is None:
            return None
        rows = conn.execute(
            "SELECT seq, entry_id, entry_type, amount_cents, paid_cents, outstanding_cents, status, reversal_id"
            " FROM order_flow WHERE tenant=? AND order_id=? ORDER BY seq ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
