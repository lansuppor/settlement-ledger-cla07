import sqlite3
import uuid
from datetime import UTC, datetime

from app.store.db import connect

# 可区分的业务拒绝原因
PAYMENT_EXCEEDS_OUTSTANDING = "payment exceeds outstanding amount"
PAYMENT_ALREADY_REVERSED = "payment already reversed"
REVERSAL_AMOUNT_INVALID = "paid amount would become negative after reversal"
PLAN_ALREADY_ACCEPTED = "installment plan already accepted"
PLAN_NOT_ACCEPTED = "installment plan not accepted"
PLAN_AMOUNT_MISMATCH = "installment amounts do not add up to order amount"
PLAN_DUPLICATE_ID = "duplicate installment id"
PLAN_AMOUNT_NOT_POSITIVE = "installment amount must be positive"
INSTALLMENT_ID_REQUIRED = "installment id is required"
INSTALLMENT_NOT_FOUND = "installment not found"
INSTALLMENT_ALREADY_PAID = "installment already paid"
INSTALLMENT_AMOUNT_MISMATCH = "payment amount does not match installment amount"

# 退款相关的可区分拒绝原因
REFUND_AMOUNT_NOT_POSITIVE = "refund amount must be positive"
REFUND_EXCEEDS_REFUNDABLE = "refund exceeds refundable balance"
REFUND_INSTALLMENT_ID_REQUIRED = "installment id is required for refund"
REFUND_INSTALLMENT_NOT_PAID = "installment is not paid"
REFUND_INSTALLMENT_AMOUNT_MISMATCH = "refund amount does not match installment paid amount"


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


def add_payment(tenant: str, order_id: str, amount_cents: int, originator: str, installment_id: str | None = None) -> dict | None:
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

        has_plan = conn.execute(
            "SELECT 1 FROM installments WHERE tenant=? AND order_id=? LIMIT 1",
            (tenant, order_id),
        ).fetchone() is not None

        installment = None
        if has_plan:
            # 已受理分期计划：必须声明期次，且金额必须等于该期应收
            if not installment_id:
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_ID_REQUIRED)
            installment = conn.execute(
                "SELECT amount_cents, status FROM installments WHERE tenant=? AND order_id=? AND installment_id=?",
                (tenant, order_id, installment_id),
            ).fetchone()
            if installment is None:
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_NOT_FOUND)
            if installment["status"] == "paid":
                # 同一期次至多收讫一笔：并发或重放都落到此处，账面不变、不留痕
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_ALREADY_PAID)
            if amount_cents != installment["amount_cents"]:
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_AMOUNT_MISMATCH)
        else:
            # 未受理分期计划：保持整单收款语义，不允许按期次收款
            if installment_id:
                conn.execute("ROLLBACK")
                raise LedgerError(PLAN_NOT_ACCEPTED)
            if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
                conn.execute("ROLLBACK")
                raise LedgerError(PAYMENT_EXCEEDS_OUTSTANDING)

        record_id = _new_record_id("pay")
        # 留痕、期次状态与账面推进在同一事务内原子提交
        conn.execute(
            """INSERT INTO payment_records(tenant, order_id, record_id, record_type, amount_cents, originator, related_record_id, created_at, installment_id)
               VALUES(?,?,?,'payment',?,?,NULL,?,?)""",
            (tenant, order_id, record_id, amount_cents, originator, _now(), installment_id),
        )
        if installment is not None:
            conn.execute(
                "UPDATE installments SET status='paid', paid_record_id=? WHERE tenant=? AND order_id=? AND installment_id=?",
                (record_id, tenant, order_id, installment_id),
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
            "SELECT amount_cents, installment_id FROM payment_records WHERE tenant=? AND order_id=? AND record_id=? AND record_type='payment'",
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
        if payment["installment_id"]:
            # 分期收款的冲正必须连同期次一起退回未收，与账面在同一事务内提交
            conn.execute(
                "UPDATE installments SET status='unpaid', paid_record_id=NULL WHERE tenant=? AND order_id=? AND installment_id=?",
                (tenant, order_id, payment["installment_id"]),
            )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (new_paid, new_paid, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)


def refund_payment(
    tenant: str,
    order_id: str,
    record_id: str,
    amount_cents: int,
    originator: str,
    installment_id: str | None = None,
) -> dict | None:
    """对一笔已登记的收款发起退款（支持部分退款，可多次退至可退余额耗尽）。

    订单不存在（含跨租户）返回 None；流水/期次/金额等业务冲突抛 LedgerError。
    成功后已收按净额减少、未收重算、状态推进；退款流水通过原收款标识关联。
    """
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

        # 退款金额必须为正（HTTP 层 pydantic gt=0 兜底，这里再守一次，保证存储语义自洽）
        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise LedgerError(REFUND_AMOUNT_NOT_POSITIVE)

        # 只能退属于当前租户、且挂在该订单下的收款；否则一律按流水不存在处理
        payment = conn.execute(
            "SELECT amount_cents, installment_id FROM payment_records "
            "WHERE tenant=? AND order_id=? AND record_id=? AND record_type='payment'",
            (tenant, order_id, record_id),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            raise LedgerError("payment record not found")

        # 已被冲正的收款款项已通过冲正全额退回，不再接受退款，避免同一笔钱经两条路径重复退回
        reversed_row = conn.execute(
            "SELECT 1 FROM payment_records WHERE tenant=? AND record_type='reversal' AND related_record_id=?",
            (tenant, record_id),
        ).fetchone()
        if reversed_row is not None:
            conn.execute("ROLLBACK")
            raise LedgerError(PAYMENT_ALREADY_REVERSED)

        if payment["installment_id"]:
            # 分期收款的退款必须声明期次、期次必须存在且当前为收讫状态，
            # 且只能整笔退该期已收金额（退款后该期回到未收，可再次收讫）。
            # 期次收讫期间不可能存在挂在其名下的退款，故可退余额即该期已收金额。
            if not installment_id:
                conn.execute("ROLLBACK")
                raise LedgerError(REFUND_INSTALLMENT_ID_REQUIRED)
            installment = conn.execute(
                "SELECT amount_cents, status, paid_record_id FROM installments "
                "WHERE tenant=? AND order_id=? AND installment_id=?",
                (tenant, order_id, installment_id),
            ).fetchone()
            if installment is None:
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_NOT_FOUND)
            # 该期必须当前正由被退款的这笔收款收讫：期次不符、已退回未收（含退款重放）
            # 或已由另一笔收款重新收讫，都按“期次未收讫”拒绝
            if (
                installment_id != payment["installment_id"]
                or installment["status"] != "paid"
                or installment["paid_record_id"] != record_id
            ):
                conn.execute("ROLLBACK")
                raise LedgerError(REFUND_INSTALLMENT_NOT_PAID)
            if amount_cents != payment["amount_cents"]:
                conn.execute("ROLLBACK")
                raise LedgerError(REFUND_INSTALLMENT_AMOUNT_MISMATCH)
        else:
            # 整单收款的退款无需、也不允许声明期次
            if installment_id:
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_NOT_FOUND)
            # 该笔收款的可退余额 = 原收款额 − 已挂在其名下的退款（与冲正相互独立、互不引用）
            refunded = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS refunded FROM payment_records "
                "WHERE tenant=? AND record_type='refund' AND related_record_id=?",
                (tenant, record_id),
            ).fetchone()["refunded"]
            if amount_cents > payment["amount_cents"] - refunded:
                # 账面不变更、不留痕；整单退款重放（余额已退完）也落到此处
                conn.execute("ROLLBACK")
                raise LedgerError(REFUND_EXCEEDS_REFUNDABLE)

        new_paid = order["paid_cents"] - amount_cents
        # 账面下限兜底：退款与冲正并存时，保证已收绝不被退成负数
        if new_paid < 0:
            conn.execute("ROLLBACK")
            raise LedgerError(REFUND_EXCEEDS_REFUNDABLE)

        refund_record_id = _new_record_id("ref")
        # 留痕、期次状态与账面推进在同一事务内原子提交
        conn.execute(
            """INSERT INTO payment_records(tenant, order_id, record_id, record_type, amount_cents,
                                           originator, related_record_id, created_at, installment_id)
               VALUES(?,?,?,'refund',?,?,?,?,?)""",
            (tenant, order_id, refund_record_id, amount_cents, originator, record_id, _now(), payment["installment_id"]),
        )
        if payment["installment_id"]:
            # 分期收款退款成功：该期回到未收并清空收讫流水引用，可再次收讫
            conn.execute(
                "UPDATE installments SET status='unpaid', paid_record_id=NULL "
                "WHERE tenant=? AND order_id=? AND installment_id=?",
                (tenant, order_id, payment["installment_id"]),
            )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (new_paid, new_paid, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)


def list_records(tenant: str, order_id: str) -> list[dict] | None:
    """按发生顺序返回订单的全部收款、冲正与退款；订单不存在（含跨租户）返回 None。"""
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            """SELECT record_id, record_type, amount_cents, created_at, originator, related_record_id, installment_id
               FROM payment_records
               WHERE tenant=? AND order_id=?
               ORDER BY created_at, record_id""",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def register_plan(tenant: str, order_id: str, items: list[dict]) -> list[dict] | None:
    """受理分期计划。订单不存在返回 None；计划不合法或已受理抛 LedgerError，且不留任何一期。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None

        # 整份计划先校验后落库：任一不合法则整体拒绝
        ids = [item["installment_id"] for item in items]
        if len(set(ids)) != len(ids):
            conn.execute("ROLLBACK")
            raise LedgerError(PLAN_DUPLICATE_ID)
        if any(item["amount_cents"] <= 0 for item in items):
            conn.execute("ROLLBACK")
            raise LedgerError(PLAN_AMOUNT_NOT_POSITIVE)
        if sum(item["amount_cents"] for item in items) != order["amount_cents"]:
            conn.execute("ROLLBACK")
            raise LedgerError(PLAN_AMOUNT_MISMATCH)

        existing = conn.execute(
            "SELECT 1 FROM installments WHERE tenant=? AND order_id=? LIMIT 1",
            (tenant, order_id),
        ).fetchone()
        if existing is not None:
            # 同一订单的分期计划只能受理一次，已受理的计划不被改动
            conn.execute("ROLLBACK")
            raise LedgerError(PLAN_ALREADY_ACCEPTED)

        now = _now()
        for item in items:
            conn.execute(
                """INSERT INTO installments(tenant, order_id, installment_id, amount_cents, due_at, status, paid_record_id, created_at)
                   VALUES(?,?,?,?,?,'unpaid',NULL,?)""",
                (tenant, order_id, item["installment_id"], item["amount_cents"], item["due_at"], now),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return list_installments(tenant, order_id)


def list_installments(tenant: str, order_id: str) -> list[dict] | None:
    """返回订单的分期计划与各期收讫状态；订单不存在（含跨租户）返回 None。"""
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            """SELECT installment_id, amount_cents, due_at, status, paid_record_id
               FROM installments
               WHERE tenant=? AND order_id=?
               ORDER BY created_at, installment_id""",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
