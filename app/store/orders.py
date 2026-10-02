import sqlite3
import uuid
from datetime import UTC, datetime

from app.store.db import connect

# 可区分的业务拒绝原因
PAYMENT_EXCEEDS_OUTSTANDING = "payment exceeds outstanding amount"
PAYMENT_ALREADY_REVERSED = "payment already reversed"
REVERSAL_AMOUNT_INVALID = "paid amount would become negative after reversal"


class LedgerError(Exception):
    """账本业务规则冲突（HTTP 层映射为 409）。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_record_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


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


def add_payment(tenant: str, order_id: str, amount_cents: int, originator: str) -> dict | None:
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
            raise LedgerError(PAYMENT_EXCEEDS_OUTSTANDING)
        record_id = _new_record_id("pay")
        # 留痕与账面推进在同一事务内原子提交
        conn.execute(
            """INSERT INTO payment_records(tenant, order_id, record_id, record_type, amount_cents, originator, related_record_id, created_at)
               VALUES(?,?,?,'payment',?,?,NULL,?)""",
            (tenant, order_id, record_id, amount_cents, originator, _now()),
        )
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)


def reverse_payment(tenant: str, order_id: str, record_id: str, originator: str) -> dict | None:
    """冲正一笔收款。订单不存在返回 None；其余业务冲突抛 LedgerError。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None

        # 只能冲正属于当前租户、且挂在该订单下的收款；否则一律按流水不存在处理
        payment = conn.execute(
            "SELECT amount_cents FROM payment_records WHERE tenant=? AND order_id=? AND record_id=? AND record_type='payment'",
            (tenant, order_id, record_id),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            raise LedgerError("payment record not found")

        duplicated = conn.execute(
            "SELECT 1 FROM payment_records WHERE tenant=? AND record_type='reversal' AND related_record_id=?",
            (tenant, record_id),
        ).fetchone()
        if duplicated is not None:
            conn.execute("ROLLBACK")
            raise LedgerError(PAYMENT_ALREADY_REVERSED)

        amount = payment["amount_cents"]
        new_paid = order["paid_cents"] - amount
        if new_paid < 0:
            # 账面不变更、不留痕
            conn.execute("ROLLBACK")
            raise LedgerError(REVERSAL_AMOUNT_INVALID)

        reversal_id = _new_record_id("rev")
        try:
            conn.execute(
                """INSERT INTO payment_records(tenant, order_id, record_id, record_type, amount_cents, originator, related_record_id, created_at)
                   VALUES(?,?,?,'reversal',?,?,?,?)""",
                (tenant, order_id, reversal_id, amount, originator, record_id, _now()),
            )
        except sqlite3.IntegrityError:
            # 唯一索引兜底：并发下同一收款也只允许一次冲正，账面不变更
            conn.execute("ROLLBACK")
            raise LedgerError(PAYMENT_ALREADY_REVERSED)
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (new_paid, new_paid, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)


def list_records(tenant: str, order_id: str) -> list[dict] | None:
    """按发生顺序返回订单的全部收款与冲正；订单不存在（含跨租户）返回 None。"""
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            """SELECT record_id, record_type, amount_cents, created_at, originator, related_record_id
               FROM payment_records
               WHERE tenant=? AND order_id=?
               ORDER BY created_at, record_id""",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
