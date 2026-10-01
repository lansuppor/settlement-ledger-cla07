import sqlite3
from datetime import UTC, datetime

from app.store.db import connect


class PaymentExceedsOutstanding(Exception):
    """本次收款会使累计已收超过订单金额。"""


class PaymentAlreadyReversed(Exception):
    """收款流水已处于已冲正状态，不能重复冲正。"""


_PAYMENT_COLUMNS = (
    "tenant, payment_id, order_id, amount_cents, status, seq, "
    "created_at, reversed_at, idempotency_key"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _payment_view(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "payment_id": row["payment_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "status": row["status"],
        "reversed": row["status"] == "reversed",
        "seq": row["seq"],
        "created_at": row["created_at"],
        "reversed_at": row["reversed_at"],
    }


def get(tenant: str, payment_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_PAYMENT_COLUMNS} FROM order_payments WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _payment_view(row)


def _next_seq(conn: sqlite3.Connection, tenant: str) -> int:
    row = conn.execute(
        "SELECT seq FROM order_payments WHERE tenant=? ORDER BY seq DESC LIMIT 1",
        (tenant,),
    ).fetchone()
    return (row["seq"] + 1) if row is not None else 1


def register(
    tenant: str, order_id: str, amount_cents: int, idempotency_key: str | None = None
) -> tuple[str, dict | None]:
    """登记一笔订单分期收款。

    返回 (结论, 流水视图)：
    - accepted：新登记成功；
    - duplicate：命中同一幂等键的首次登记结论（不二次入账、不产生第二条流水）。
    订单不存在（含跨租户）返回 order_not_found；超限抛出 PaymentExceedsOutstanding。
    判重、订单校验、流水落库、累计已收累加在单个立即事务内完成：
    并发下同一订单累计已收绝不超过订单金额；任何失败都回滚，不留部分数据。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if idempotency_key is not None:
            existing = conn.execute(
                f"SELECT {_PAYMENT_COLUMNS} FROM order_payments WHERE tenant=? AND idempotency_key=?",
                (tenant, idempotency_key),
            ).fetchone()
            if existing is not None:
                # 幂等：超时重试只命中同一次结论，金额与流水都不再变动
                conn.execute("ROLLBACK")
                return "duplicate", _payment_view(existing)

        order = conn.execute(
            "SELECT amount_cents, paid_cents, currency FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 跨租户或不存在一律按“不存在”处理，不泄漏订单是否存在
            conn.execute("ROLLBACK")
            return "order_not_found", None

        if amount_cents <= 0 or order["paid_cents"] + amount_cents > order["amount_cents"]:
            conn.execute("ROLLBACK")
            raise PaymentExceedsOutstanding("payment exceeds outstanding amount")

        seq = _next_seq(conn, tenant)
        payment_id = f"pay-{seq:010d}-{order_id}"
        created_at = _now()
        conn.execute(
            "INSERT INTO order_payments(tenant, payment_id, order_id, amount_cents, status, seq, "
            "created_at, idempotency_key) VALUES(?,?,?,?,'accepted',?,?,?)",
            (tenant, payment_id, order_id, amount_cents, seq, created_at, idempotency_key),
        )
        new_paid = order["paid_cents"] + amount_cents
        # 累计已收累加；达到订单金额时订单结清并标记“曾结清”
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=CASE WHEN ? >= amount_cents THEN 'settled' ELSE status END, "
            "ever_settled=CASE WHEN ? >= amount_cents THEN 1 ELSE ever_settled END "
            "WHERE tenant=? AND order_id=?",
            (new_paid, new_paid, new_paid, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return "accepted", get(tenant, payment_id)


def reverse(tenant: str, payment_id: str) -> dict | None:
    """冲正一笔收款流水：把该笔金额从累计已收中整体退回并标记已冲正。

    流水不存在（含跨租户）返回 None；已冲正抛出 PaymentAlreadyReversed。
    判定与入账在单个立即事务内完成：同一流水并发冲正最多一个成功。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_PAYMENT_COLUMNS} FROM order_payments WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["status"] == "reversed":
            conn.execute("ROLLBACK")
            raise PaymentAlreadyReversed("payment already reversed")

        reversed_at = _now()
        conn.execute(
            "UPDATE order_payments SET status='reversed', reversed_at=? WHERE tenant=? AND payment_id=?",
            (reversed_at, tenant, payment_id),
        )
        # 只整体退回本笔金额；退款/结算链路的任何金额都不动。
        # ever_settled 保持不变：曾结清过的订单冲正后可与“从未结清”区分。
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents - ?, "
            "status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (row["amount_cents"], row["amount_cents"], tenant, row["order_id"]),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, payment_id)


def list_for_order(tenant: str, order_id: str) -> list[dict]:
    """列出订单的全部收款流水，按登记顺序（seq 升序）稳定排列。"""
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_PAYMENT_COLUMNS} FROM order_payments WHERE tenant=? AND order_id=? ORDER BY seq ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_payment_view(r) for r in rows]


def search(
    tenant: str,
    order_id: str | None = None,
    min_amount: int | None = None,
    max_amount: int | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    reversed_only: bool | None = None,
    after_seq: int | None = None,
    limit: int = 50,
) -> tuple[list[dict], int | None]:
    """按组合条件检索收款流水，按登记顺序（seq 升序）稳定排列。

    返回 (本页流水, 下一页游标 next_seq)；游标分页保证不重不漏，
    同一条件重复执行结果一致。
    """
    where = ["tenant=?"]
    params: list[object] = [tenant]
    if order_id is not None:
        where.append("order_id=?")
        params.append(order_id)
    if min_amount is not None:
        where.append("amount_cents >= ?")
        params.append(min_amount)
    if max_amount is not None:
        where.append("amount_cents <= ?")
        params.append(max_amount)
    if created_from is not None:
        where.append("created_at >= ?")
        params.append(created_from)
    if created_to is not None:
        where.append("created_at <= ?")
        params.append(created_to)
    if reversed_only is True:
        where.append("status='reversed'")
    elif reversed_only is False:
        where.append("status='accepted'")
    if after_seq is not None:
        where.append("seq > ?")
        params.append(after_seq)

    sql = (
        f"SELECT {_PAYMENT_COLUMNS} FROM order_payments WHERE {' AND '.join(where)} "
        "ORDER BY seq ASC LIMIT ?"
    )
    params.append(limit + 1)

    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    page = rows[:limit]
    next_seq = page[-1]["seq"] if len(rows) > limit and page else None
    return [_payment_view(r) for r in page], next_seq
