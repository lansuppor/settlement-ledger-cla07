import sqlite3
from datetime import UTC, datetime

from app.store.db import connect


class OrderNotSettled(Exception):
    """订单未结清（未收金额不为 0），不能受理退款。"""


class OrderPaymentReversed(Exception):
    """订单曾结清，但收款被冲正导致未收重新大于 0，不能受理退款。"""


class RefundExceedsBalance(Exception):
    """退款金额超过可退余额（订单金额 − 累计已退金额）。"""


class RefundAlreadyReversed(Exception):
    """退款单已处于已冲正状态，不能重复冲正。"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _refund_view(row: sqlite3.Row) -> dict:
    return dict(row)


def get(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, refund_id, order_id, amount_cents, currency, status, created_at, reversed_at "
            "FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _refund_view(row)


def accept(tenant: str, refund_id: str, order_id: str, amount_cents: int) -> tuple[str, dict | None]:
    """受理退款单。

    返回 (结论, 退款单视图)，结论为 accepted / duplicate。
    订单不存在（含跨租户）返回键不存在；业务校验失败抛出可区分的异常。
    整个判定与入账在一个立即事务内完成：并发下最多一个 accepted，
    任何失败都回滚，不留部分数据。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, refund_id, order_id, amount_cents, currency, status, created_at, reversed_at "
            "FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            # 幂等：重复受理不重复入账，也不改变首次受理的单据；
            # 已冲正的标识不得再次受理
            conn.execute("ROLLBACK")
            result = "already_reversed" if existing["status"] == "reversed" else "duplicate"
            return result, _refund_view(existing)

        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, ever_settled, currency FROM orders "
            "WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 跨租户或不存在一律按“不存在”处理，不泄漏订单是否存在
            conn.execute("ROLLBACK")
            return "order_not_found", {}

        if order["amount_cents"] - order["paid_cents"] > 0:
            conn.execute("ROLLBACK")
            # 曾结清却再次未结清，只能是收款被冲正所致：拒绝原因与“从未结清”可区分
            if order["ever_settled"] == 1:
                raise OrderPaymentReversed("order payment reversed")
            raise OrderNotSettled("order is not settled")

        if amount_cents > order["amount_cents"] - order["refunded_cents"]:
            conn.execute("ROLLBACK")
            raise RefundExceedsBalance("refund exceeds refundable amount")

        created_at = _now()
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, currency, status, created_at) "
            "VALUES(?,?,?,?,?,'accepted',?)",
            (tenant, refund_id, order_id, amount_cents, order["currency"], created_at),
        )
        # 只改写累计已退金额；paid_cents 与收款记录保持不动
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents + ? WHERE tenant=? AND order_id=?",
            (amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return "accepted", get(tenant, refund_id)


def reverse(tenant: str, refund_id: str) -> dict | None:
    """冲正已受理的退款单；整体退回累计已退金额。

    退款单不存在（含跨租户）返回 None；已冲正抛出 RefundAlreadyReversed。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, order_id, status FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["status"] == "reversed":
            conn.execute("ROLLBACK")
            raise RefundAlreadyReversed("refund already reversed")

        reversed_at = _now()
        conn.execute(
            "UPDATE refunds SET status='reversed', reversed_at=? WHERE tenant=? AND refund_id=?",
            (reversed_at, tenant, refund_id),
        )
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents - ? WHERE tenant=? AND order_id=?",
            (row["amount_cents"], tenant, row["order_id"]),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, refund_id)
