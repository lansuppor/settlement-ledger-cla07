import sqlite3
from datetime import UTC, datetime

from app.store.db import connect

STATUS_ACCEPTED = "accepted"
STATUS_REVERSED = "reversed"

ORDER_NOT_FOUND = "order not found"
ORDER_NOT_SETTLED = "order is not settled"
REFUND_EXCEEDS_REFUNDABLE = "refund amount exceeds refundable balance"
REFUND_ALREADY_ACCEPTED = "refund already accepted"
REFUND_ALREADY_REVERSED = "refund already reversed"
REVERSAL_AFTER_REVERSAL = "refund already reversed"

class RefundRuleError(ValueError):
    pass

def _now() -> str:
    return datetime.now(UTC).isoformat()

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)

def get(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, refund_id, order_id, amount_cents, status, created_at, reversed_at "
            "FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_dict(row)

def accept(tenant: str, refund_id: str, order_id: str, amount_cents: int) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT status FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            if existing["status"] == STATUS_REVERSED:
                raise RefundRuleError(REFUND_ALREADY_REVERSED)
            raise RefundRuleError(REFUND_ALREADY_ACCEPTED)

        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            raise RefundRuleError(ORDER_NOT_FOUND)
        if order["paid_cents"] < order["amount_cents"]:
            conn.execute("ROLLBACK")
            raise RefundRuleError(ORDER_NOT_SETTLED)
        if amount_cents > order["amount_cents"] - order["refunded_cents"]:
            conn.execute("ROLLBACK")
            raise RefundRuleError(REFUND_EXCEEDS_REFUNDABLE)

        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, status, created_at) "
            "VALUES(?,?,?,?,'accepted',?)",
            (tenant, refund_id, order_id, amount_cents, _now()),
        )
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents + ? WHERE tenant=? AND order_id=?",
            (amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    refund = get(tenant, refund_id)
    assert refund is not None
    return refund

def reverse(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT order_id, amount_cents, status FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["status"] == STATUS_REVERSED:
            conn.execute("ROLLBACK")
            raise RefundRuleError(REVERSAL_AFTER_REVERSAL)

        conn.execute(
            "UPDATE refunds SET status='reversed', reversed_at=? WHERE tenant=? AND refund_id=?",
            (_now(), tenant, refund_id),
        )
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents - ? WHERE tenant=? AND order_id=?",
            (row["amount_cents"], tenant, row["order_id"]),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    refund = get(tenant, refund_id)
    assert refund is not None
    return refund
