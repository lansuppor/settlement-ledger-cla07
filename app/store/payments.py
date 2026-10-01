import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from app.store.db import connect


class PaymentExceedsOutstanding(Exception):
    """本次登记会使累计已收超过订单金额。"""


class PaymentAlreadyReversed(Exception):
    """收款流水已处于已冲正状态，不能重复冲正。"""


class OrderReopenedByReversal(Exception):
    """订单曾结清，但因收款冲正使未收金额重新大于 0，不得受理退款/结算单。"""


def _now() -> str:
    # 微秒精度：同一秒内连续登记的流水也有可区分、可比较的登记时刻
    return datetime.now(UTC).isoformat(timespec="microseconds")


_FLOW_COLUMNS = (
    "tenant, payment_id, order_id, amount_cents, status, created_at, reversed_at"
)


def _flow_view(row: sqlite3.Row) -> dict:
    return dict(row)


def _parse_instant(value: str) -> str:
    """把外部传入的 ISO-8601 时刻规范化为与登记时刻一致的 UTC 微秒文本。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # 容忍查询串里未编码的 '+' 被解码为空格，如 "...11.18 00:00"
        normalized = re.sub(
            r"(\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s+(\d{2}:\d{2})$", r"\1+\2", value
        )
        parsed = datetime.fromisoformat(normalized)  # 仍非法则抛出，由入口返回 400
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def get(tenant: str, payment_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """按流水标识读取；其他租户或不存在一律返回 None。"""
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_FLOW_COLUMNS} FROM order_payments WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        ).fetchone()
    finally:
        if own_conn:
            conn.close()
    return None if row is None else _flow_view(row)


def list_for_order(tenant: str, order_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """读取订单的全部收款流水，按登记顺序（seq）稳定排列。"""
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_FLOW_COLUMNS} FROM order_payments WHERE tenant=? AND order_id=? ORDER BY seq",
            (tenant, order_id),
        ).fetchall()
    finally:
        if own_conn:
            conn.close()
    return [_flow_view(row) for row in rows]


def has_reversed(tenant: str, order_id: str, conn: sqlite3.Connection) -> bool:
    """订单是否存在已冲正的收款流水（用于区分“冲正重开未收”与“从未结清”）。"""
    row = conn.execute(
        "SELECT 1 FROM order_payments WHERE tenant=? AND order_id=? AND status='reversed' LIMIT 1",
        (tenant, order_id),
    ).fetchone()
    return row is not None


def register(tenant: str, order_id: str, amount_cents: int, idempotency_key: str | None) -> tuple[str, dict]:
    """逐笔登记订单收款（分期）。

    返回 (结论, 流水视图)，结论为 registered / replayed：
    - 幂等键命中首次成功登记：replayed，返回首次流水，不二次入账、不产生第二条流水；
    - 订单不存在（含跨租户）：order_not_found；
    - 金额非法或累计已收会超过订单金额：抛出 PaymentExceedsOutstanding。
    判重、订单校验、流水落库、累计已收更新在单个立即事务内原子完成。
    """
    key = idempotency_key or uuid4().hex
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 先查幂等键：超时重试即使在业务边界附近也只命中同一次结论
        existing = conn.execute(
            f"SELECT {_FLOW_COLUMNS} FROM order_payments WHERE tenant=? AND idempotency_key=?",
            (tenant, key),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            return "replayed", _flow_view(existing)

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 跨租户或不存在一律按“不存在”处理，不泄漏订单是否存在
            conn.execute("ROLLBACK")
            return "order_not_found", {}

        if amount_cents <= 0 or order["paid_cents"] + amount_cents > order["amount_cents"]:
            conn.execute("ROLLBACK")
            raise PaymentExceedsOutstanding("payment exceeds outstanding amount")

        payment_id = uuid4().hex
        created_at = _now()
        conn.execute(
            "INSERT INTO order_payments(tenant, payment_id, idempotency_key, order_id, amount_cents, "
            "status, created_at) VALUES(?,?,?,?,?, 'accepted', ?)",
            (tenant, payment_id, key, order_id, amount_cents, created_at),
        )
        # 只改写订单收款链路自身；refunded_cents / settled_cents 一律不动
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return "registered", get(tenant, payment_id)


def reverse(tenant: str, payment_id: str) -> dict | None:
    """逐笔冲正收款流水：把该笔金额从累计已收中整体退回并标记已冲正。

    流水不存在（含跨租户）返回 None；已冲正抛出 PaymentAlreadyReversed。
    判定与入账在单个立即事务内完成：同一流水并发冲正最多一个成功。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payment_id, order_id, amount_cents, status FROM order_payments "
            "WHERE tenant=? AND payment_id=?",
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
        # 只退回收款链路自身的金额；退款 / 结算链路数据一律不动
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


def search(
    tenant: str,
    *,
    order_id: str | None = None,
    min_amount_cents: int | None = None,
    max_amount_cents: int | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    reversed_only: bool | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> dict:
    """按租户内条件组合检索收款流水，登记顺序（seq）稳定排列的键集分页。

    同一查询重复执行结果一致、不重不漏：以 seq 为游标，WHERE seq > cursor。
    """
    clauses = ["tenant=?"]
    params: list = [tenant]
    if order_id is not None:
        clauses.append("order_id=?")
        params.append(order_id)
    if min_amount_cents is not None:
        clauses.append("amount_cents >= ?")
        params.append(min_amount_cents)
    if max_amount_cents is not None:
        clauses.append("amount_cents <= ?")
        params.append(max_amount_cents)
    if created_from is not None:
        clauses.append("created_at >= ?")
        params.append(_parse_instant(created_from))
    if created_to is not None:
        clauses.append("created_at <= ?")
        params.append(_parse_instant(created_to))
    if reversed_only is True:
        clauses.append("status='reversed'")
    elif reversed_only is False:
        clauses.append("status='accepted'")
    if cursor is not None:
        clauses.append("seq > ?")
        params.append(cursor)

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_FLOW_COLUMNS}, seq FROM order_payments WHERE {' AND '.join(clauses)} "
            "ORDER BY seq LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for row in rows[:limit]:
        flow = dict(row)
        flow.pop("seq", None)  # seq 仅用作分页游标，不进入流水对象
        items.append(flow)
    # 多取一条用于判断是否有下一页；游标必须是“本页最后一条”的 seq，
    # 否则下一页 WHERE seq > cursor 会丢掉多看的那一条
    next_cursor = rows[limit - 1]["seq"] if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}
