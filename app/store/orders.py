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

# 退款相关的可区分业务拒绝原因
REFUND_ALREADY_APPLIED = "refund already applied"
REFUND_AMOUNT_NOT_POSITIVE = "refund amount must be positive"
REFUND_EXCEEDS_REFUNDABLE = "refund exceeds refundable amount"
REFUND_INSTALLMENT_NOT_PAID = "installment is not paid"
REFUND_INSTALLMENT_AMOUNT_MISMATCH = "refund amount does not match installment amount"


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


def _apply_payment(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    amount_cents: int,
    originator: str,
    installment_id: str | None = None,
) -> dict | None:
    """在调用方管理的事务内登记一笔收款：业务校验、留痕、期次与账面推进。

    订单不存在（含跨租户）返回 None；业务冲突抛 LedgerError（由调用方回滚）。
    成功返回收款流水标识与受理后账面快照（paid_cents / outstanding_cents / status）。
    单笔登记与批量导入共用本函数，保证两者的校验与账面推进完全等同。
    """
    row = conn.execute(
        "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        return None

    has_plan = conn.execute(
        "SELECT 1 FROM installments WHERE tenant=? AND order_id=? LIMIT 1",
        (tenant, order_id),
    ).fetchone() is not None

    installment = None
    if has_plan:
        # 已受理分期计划：必须声明期次，且金额必须等于该期应收
        if not installment_id:
            raise LedgerError(INSTALLMENT_ID_REQUIRED)
        installment = conn.execute(
            "SELECT amount_cents, status FROM installments WHERE tenant=? AND order_id=? AND installment_id=?",
            (tenant, order_id, installment_id),
        ).fetchone()
        if installment is None:
            raise LedgerError(INSTALLMENT_NOT_FOUND)
        if installment["status"] == "paid":
            # 同一期次至多收讫一笔：并发或重放都落到此处，账面不变、不留痕
            raise LedgerError(INSTALLMENT_ALREADY_PAID)
        if amount_cents != installment["amount_cents"]:
            raise LedgerError(INSTALLMENT_AMOUNT_MISMATCH)
    else:
        # 未受理分期计划：保持整单收款语义，不允许按期次收款
        if installment_id:
            raise LedgerError(PLAN_NOT_ACCEPTED)
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
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
    paid = row["paid_cents"] + amount_cents
    return {
        "record_id": record_id,
        "paid_cents": paid,
        "outstanding_cents": row["amount_cents"] - paid,
        "status": "settled" if paid >= row["amount_cents"] else "accepted",
    }


def add_payment(tenant: str, order_id: str, amount_cents: int, originator: str, installment_id: str | None = None) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _apply_payment(conn, tenant, order_id, amount_cents, originator, installment_id)
        except LedgerError:
            conn.execute("ROLLBACK")
            raise
        if result is None:
            conn.execute("ROLLBACK")
            return None
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
    """登记一笔退款并推进账面/期次/流水。

    订单不存在（含跨租户）返回 None；原收款流水不存在或不属于该订单/租户抛
    LedgerError("payment record not found")；其余业务冲突抛可区分的 LedgerError。
    paid_cents 始终为“收款−冲正−退款”的净收款；账面推进、期次回退与退款留痕
    在同一事务内原子提交。同一退款请求（同订单、同原收款、同金额、同期次）重放只生效一次。
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

        # 只能退属于当前租户、且挂在该订单下的收款；否则一律按流水不存在处理
        payment = conn.execute(
            "SELECT amount_cents, installment_id FROM payment_records "
            "WHERE tenant=? AND order_id=? AND record_id=? AND record_type='payment'",
            (tenant, order_id, record_id),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            raise LedgerError("payment record not found")

        if amount_cents is None or amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise LedgerError(REFUND_AMOUNT_NOT_POSITIVE)

        paid_installment_id = payment["installment_id"]
        if paid_installment_id:
            # 分期收款的退款必须声明期次，且只能退该期当前已收的金额
            if not installment_id:
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_ID_REQUIRED)
            installment = conn.execute(
                "SELECT amount_cents, status, paid_record_id FROM installments "
                "WHERE tenant=? AND order_id=? AND installment_id=?",
                (tenant, order_id, installment_id),
            ).fetchone()
            if installment is None:
                conn.execute("ROLLBACK")
                raise LedgerError(INSTALLMENT_NOT_FOUND)
            if installment["status"] != "paid" or installment["paid_record_id"] != record_id:
                # 该期未收讫（从未收、已退或已冲正后回到未收）：不可退
                conn.execute("ROLLBACK")
                raise LedgerError(REFUND_INSTALLMENT_NOT_PAID)
            if amount_cents != installment["amount_cents"]:
                conn.execute("ROLLBACK")
                raise LedgerError(REFUND_INSTALLMENT_AMOUNT_MISMATCH)
            refund_installment_id = installment_id
        else:
            # 整单收款的退款无需也不得声明期次
            if installment_id:
                conn.execute("ROLLBACK")
                raise LedgerError(PLAN_NOT_ACCEPTED)
            refund_installment_id = None

        # 该笔原收款自身的可退余额 = 原额 − 已挂在它名下的退款（支持部分退款）
        refunded_on_payment = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM payment_records "
            "WHERE tenant=? AND order_id=? AND record_type='refund' AND related_record_id=?",
            (tenant, order_id, record_id),
        ).fetchone()["total"]
        remaining_on_payment = payment["amount_cents"] - refunded_on_payment

        # 重放判定先于余额判定：同额、同期次的退款即同一请求，重放不重复扣减/留痕
        duplicate = conn.execute(
            "SELECT 1 FROM payment_records "
            "WHERE tenant=? AND order_id=? AND record_type='refund' AND related_record_id=? "
            "AND amount_cents=? AND COALESCE(installment_id, '')=COALESCE(?, '')",
            (tenant, order_id, record_id, amount_cents, refund_installment_id),
        ).fetchone()
        if duplicate is not None:
            conn.execute("ROLLBACK")
            raise LedgerError(REFUND_ALREADY_APPLIED)

        if amount_cents > remaining_on_payment:
            # 超过该收款的可退余额（分期路径此处恒为整额，通常由上面的期次校验拦截）
            conn.execute("ROLLBACK")
            raise LedgerError(REFUND_EXCEEDS_REFUNDABLE)

        # 订单级可退余额即当前净收款（已收毛额−已退−已冲正）；不得退成负数
        if amount_cents > order["paid_cents"]:
            conn.execute("ROLLBACK")
            raise LedgerError(REFUND_EXCEEDS_REFUNDABLE)

        new_paid = order["paid_cents"] - amount_cents
        refund_id = _new_record_id("ref")
        try:
            conn.execute(
                """INSERT INTO payment_records(tenant, order_id, record_id, record_type, amount_cents,
                                                originator, related_record_id, created_at, installment_id)
                   VALUES(?,?,?,'refund',?,?,?,?,?)""",
                (tenant, order_id, refund_id, amount_cents, originator, record_id, _now(),
                 refund_installment_id),
            )
        except sqlite3.IntegrityError:
            # 唯一索引兜底：并发或重放下同一退款只允许一笔，账面不变更、不留痕
            conn.execute("ROLLBACK")
            raise LedgerError(REFUND_ALREADY_APPLIED)

        if refund_installment_id:
            # 该期回到未收、清空收讫流水引用，可再次收讫
            conn.execute(
                "UPDATE installments SET status='unpaid', paid_record_id=NULL "
                "WHERE tenant=? AND order_id=? AND installment_id=?",
                (tenant, order_id, refund_installment_id),
            )
        # 未收 = 订单金额 − 净收款；净收款退完（净额 0）回到未收款状态，未收转正即不再结清
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


# 条件检索支持的账面状态取值：按查询当下账面含义判定（未收为 0 即已结清）
SEARCH_STATUS_SETTLED = "settled"
SEARCH_STATUS_UNSETTLED = "unsettled"


def search_orders(
    tenant: str,
    *,
    status: str | None = None,
    currency: str | None = None,
    min_amount_cents: int | None = None,
    max_amount_cents: int | None = None,
    payment_originator: str | None = None,
    has_installment_plan: bool | None = None,
    page_size: int,
    after_order_id: str | None = None,
) -> tuple[list[dict], bool]:
    """租户内按条件检索订单账面快照，以订单标识为稳定排序键做 keyset 分页。

    多个条件同时给定时按同时满足处理；始终限定 tenant，任何条件下都不返回其他
    租户的订单。状态不读落库 status 列，而按查询当下的“订单金额−已收金额”判定，
    退款/冲正使未收转正后自然回到未结清。

    取 page_size+1 行判断是否还有下一页，返回 (本页订单, 是否还有更多)。
    翻页只沿不可变的 order_id 键前进（WHERE order_id > 上页末单），因此翻页过程中
    新增或变更的单据既不会让已返回的订单重复出现，也不会让此前符合条件的订单被跳过。
    """
    where = ["tenant = ?"]
    params: list = [tenant]
    if status == SEARCH_STATUS_SETTLED:
        where.append("amount_cents - paid_cents = 0")
    elif status == SEARCH_STATUS_UNSETTLED:
        where.append("amount_cents - paid_cents > 0")
    if currency is not None:
        where.append("currency = ?")
        params.append(currency)
    if min_amount_cents is not None:
        where.append("amount_cents >= ?")
        params.append(min_amount_cents)
    if max_amount_cents is not None:
        where.append("amount_cents <= ?")
        params.append(max_amount_cents)
    if payment_originator is not None:
        # “发起过收款”：存在一条该发起方留下的 payment 流水（冲正/退款不改变发起事实）
        where.append(
            "EXISTS (SELECT 1 FROM payment_records pr WHERE pr.tenant = orders.tenant "
            "AND pr.order_id = orders.order_id AND pr.record_type = 'payment' AND pr.originator = ?)"
        )
        params.append(payment_originator)
    if has_installment_plan is not None:
        plan_clause = (
            "EXISTS (SELECT 1 FROM installments ins WHERE ins.tenant = orders.tenant "
            "AND ins.order_id = orders.order_id)"
        )
        where.append(plan_clause if has_installment_plan else f"NOT {plan_clause}")
    if after_order_id is not None:
        # 稳定续取：只取排序键严格大于上页末单的行
        where.append("order_id > ?")
        params.append(after_order_id)

    sql = (
        "SELECT order_id, amount_cents, paid_cents, currency, status FROM orders "
        + " WHERE " + " AND ".join(where)
        + " ORDER BY order_id ASC LIMIT ?"
    )
    params.append(page_size + 1)
    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    has_more = len(rows) > page_size
    page = rows[:page_size]
    items = []
    for row in page:
        outstanding = row["amount_cents"] - row["paid_cents"]
        items.append(
            {
                "order_id": row["order_id"],
                "amount_cents": row["amount_cents"],
                "paid_cents": row["paid_cents"],
                "outstanding_cents": outstanding,
                "currency": row["currency"],
                # 当前状态按查询当下账面含义给出，保证快照内未收与状态始终自洽
                "status": "settled" if outstanding == 0 else "accepted",
            }
        )
    return items, has_more
