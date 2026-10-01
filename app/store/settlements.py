import sqlite3
from datetime import UTC, datetime

from app.store.db import connect


class OrderNotSettled(Exception):
    """订单未结清（未收金额不为 0），不能受理结算单。"""


class OrderHasRefunds(Exception):
    """订单已发生退款（累计已退金额不为 0），不能受理结算单。"""


class SettlementExceedsBalance(Exception):
    """结算金额超过订单剩余可结算金额（订单金额 − 已受理结算金额之和，不含已冲正部分）。"""


class SettlementAlreadyReversed(Exception):
    """结算单已处于已冲正状态。"""


class SettlementHasReceipts(Exception):
    """结算单已有收款（累计已收不为 0），不能冲正。"""


class ReceiptExceedsUnreceived(Exception):
    """本次收款后累计已收将超过结算单的结算金额。"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _settlement_view(row: sqlite3.Row) -> dict:
    return {
        **dict(row),
        "unreceived_cents": row["amount_cents"] - row["received_cents"],
    }


_COLUMNS = (
    "tenant, settlement_id, order_id, amount_cents, received_cents, currency, "
    "status, created_at, reversed_at"
)


def get(tenant: str, settlement_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _settlement_view(row)


def accept(tenant: str, settlement_id: str, order_id: str, amount_cents: int) -> tuple[str, dict | None]:
    """受理结算单。

    返回 (结论, 结算单视图)，结论为 accepted / duplicate / already_reversed；
    订单不存在（含跨租户）返回 order_not_found；其余业务校验失败抛出可区分的异常。
    判重、订单校验、结算单落库、占用订单剩余可结算金额在单个立即事务内完成：
    并发下同一标识最多一个 accepted，不同结算单累计已结算绝不超过订单金额，
    任何失败整体回滚，不留部分数据。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            f"SELECT {_COLUMNS} FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if existing is not None:
            # 幂等：重复受理不重复入账，也不改变首次受理的单据；
            # 已冲正的标识不得再次受理
            conn.execute("ROLLBACK")
            result = "already_reversed" if existing["status"] == "reversed" else "duplicate"
            return result, _settlement_view(existing)

        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, settled_cents, currency FROM orders "
            "WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 跨租户或不存在一律按“不存在”处理，不泄漏订单是否存在
            conn.execute("ROLLBACK")
            return "order_not_found", {}

        if order["amount_cents"] - order["paid_cents"] > 0:
            conn.execute("ROLLBACK")
            raise OrderNotSettled("order is not settled")

        if order["refunded_cents"] > 0:
            conn.execute("ROLLBACK")
            raise OrderHasRefunds("order has refunds")

        if amount_cents > order["amount_cents"] - order["settled_cents"]:
            conn.execute("ROLLBACK")
            raise SettlementExceedsBalance("settlement exceeds settleable amount")

        created_at = _now()
        conn.execute(
            "INSERT INTO settlements(tenant, settlement_id, order_id, amount_cents, received_cents, "
            "currency, status, created_at) VALUES(?,?,?,?,0,?,'accepted',?)",
            (tenant, settlement_id, order_id, amount_cents, order["currency"], created_at),
        )
        # 只占用累计已结算金额；paid_cents / 收款记录 / refunded_cents 一律不动
        conn.execute(
            "UPDATE orders SET settled_cents = settled_cents + ? WHERE tenant=? AND order_id=?",
            (amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return "accepted", get(tenant, settlement_id)


def receive(tenant: str, settlement_id: str, amount_cents: int) -> dict | None:
    """登记一笔分期收款。

    结算单不存在（含跨租户）返回 None；已冲正抛出 SettlementAlreadyReversed；
    累计收款将超过结算金额抛出 ReceiptExceedsUnreceived，结算单不做任何改动。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, received_cents, status FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["status"] == "reversed":
            conn.execute("ROLLBACK")
            raise SettlementAlreadyReversed("settlement already reversed")
        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise ValueError("receipt amount must be positive")
        if row["received_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ReceiptExceedsUnreceived("receipt exceeds unreceived amount")

        created_at = _now()
        conn.execute(
            "UPDATE settlements SET received_cents = received_cents + ? WHERE tenant=? AND settlement_id=?",
            (amount_cents, tenant, settlement_id),
        )
        conn.execute(
            "INSERT INTO settlement_payments(tenant, settlement_id, amount_cents, created_at) VALUES(?,?,?,?)",
            (tenant, settlement_id, amount_cents, created_at),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, settlement_id)


def reverse(tenant: str, settlement_id: str) -> dict | None:
    """冲正已受理且累计已收为 0 的结算单；整体释放其占用的订单剩余可结算金额。

    结算单不存在（含跨租户）返回 None；已冲正抛出 SettlementAlreadyReversed；
    存在任何收款抛出 SettlementHasReceipts，订单与结算单数据保持不变。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, received_cents, order_id, status FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["status"] == "reversed":
            conn.execute("ROLLBACK")
            raise SettlementAlreadyReversed("settlement already reversed")
        if row["received_cents"] > 0:
            conn.execute("ROLLBACK")
            raise SettlementHasReceipts("settlement has receipts")

        reversed_at = _now()
        conn.execute(
            "UPDATE settlements SET status='reversed', reversed_at=? WHERE tenant=? AND settlement_id=?",
            (reversed_at, tenant, settlement_id),
        )
        conn.execute(
            "UPDATE orders SET settled_cents = settled_cents - ? WHERE tenant=? AND order_id=?",
            (row["amount_cents"], tenant, row["order_id"]),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, settlement_id)
