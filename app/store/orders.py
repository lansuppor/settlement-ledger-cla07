import sqlite3
from datetime import UTC, datetime, timezone

from app.rules.time_rules import (
    offset_minutes,
    timezone_from_minutes,
    to_display,
    to_storage,
)
from app.store.db import connect

_FLOW_COLUMNS = (
    "seq, entry_id, entry_type, amount_cents, paid_cents, outstanding_cents, status,"
    " reversal_id, business_time, business_time_offset"
)

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

def _flow_dict(row: sqlite3.Row) -> dict:
    """把流水行渲染为出参：business_time 按登记时的偏移写法返回（时间基准不变）。

    business_time 以 UTC 落库，business_time_offset 记录登记时偏移；
    二者成对还原。历史流水偏移为 0，即 +00:00，写法与迁移前一致。
    """
    entry = dict(row)
    offset = timezone_from_minutes(entry.pop("business_time_offset"))
    dt = datetime.fromisoformat(entry["business_time"]).replace(tzinfo=UTC)
    entry["business_time"] = to_display(dt, offset)
    return entry


def add_payment(
    tenant: str, order_id: str, amount_cents: int, business_time: datetime | None = None
) -> dict | None:
    if business_time is None:
        # 服务自行记账：以服务当前时间及时区的偏移写法记录。
        business_time = datetime.now().astimezone()
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
            " paid_cents, outstanding_cents, status, reversal_id, business_time, business_time_offset)"
            " VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                tenant, order_id, seq, f"pay-{seq}", "payment", amount_cents,
                paid_after, row["amount_cents"] - paid_after, _settled(paid_after, row["amount_cents"]),
                to_storage(business_time), offset_minutes(business_time),
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)

def reverse_payment(
    tenant: str, order_id: str, reversal_id: str, amount_cents: int,
    business_time: datetime | None = None,
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
    if business_time is None:
        # 服务自行记账：以服务当前时间及时区的偏移写法记录。
        business_time = datetime.now().astimezone()
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
            " paid_cents, outstanding_cents, status, reversal_id, business_time, business_time_offset)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant, order_id, seq, f"rev-{seq}", "reversal", amount_cents,
                paid_after, row["amount_cents"] - paid_after, _settled(paid_after, row["amount_cents"]),
                reversal_id, to_storage(business_time), offset_minutes(business_time),
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
            f"SELECT {_FLOW_COLUMNS} FROM order_flow WHERE tenant=? AND order_id=? ORDER BY seq ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_flow_dict(row) for row in rows]


def _flow_rows_in_range(
    conn: sqlite3.Connection, tenant: str, order_id: str, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    """闭区间 [start, end] 内的流水原始行，按生效先后（seq 升序）排列。

    以 UTC 基准时刻（business_time 文本）比较；调用方须先把起讫点换算到 UTC。
    """
    return conn.execute(
        f"SELECT {_FLOW_COLUMNS} FROM order_flow"
        " WHERE tenant=? AND order_id=? AND business_time >= ? AND business_time <= ? ORDER BY seq ASC",
        (tenant, order_id, to_storage(start), to_storage(end)),
    ).fetchall()


def list_flow_range(
    tenant: str, order_id: str, start: datetime, end: datetime
) -> list[dict] | None:
    """按业务发生时间范围返回该订单在本租户下生效的收款与冲正。

    订单不存在或属于其他租户时返回 None（调用方按 404 处理，不泄漏对象是否存在）。
    闭区间以 business_time（UTC 文本）为准，早于起点或晚于终点的流水不返回；
    结果按生效先后（seq 升序）排列，各条内容与 list_flow 完全一致（含登记时偏移写法）。
    调用方负责先校验起点、终点合法且起点不晚于终点；本函数只读数据。
    """
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    conn = connect()
    try:
        owned = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if owned is None:
            return None
        rows = _flow_rows_in_range(conn, tenant, order_id, start, end)
    finally:
        conn.close()
    return [_flow_dict(row) for row in rows]


def daily_summary(
    tenant: str, order_id: str, start: datetime, end: datetime, offset: timezone
) -> list[dict] | None:
    """按对账时区偏移对闭区间 [start, end] 内的流水按日历日汇总。

    订单不存在或属于其他租户时返回 None（调用方按 404 处理，不泄漏对象是否存在）。
    每条流水按其 UTC 基准时刻换算到对账偏移后的日历日归组，恰落入一个分组；
    无流水的日期不出现，结果按日期升序。每组给出收款合计、冲正合计、流水条数，
    以及组内最早/最晚业务发生时间（按对账偏移写法返回，基准时刻仍是该条流水自身时刻）。
    调用方负责先校验起点、终点与偏移合法且起点不晚于终点；本函数只读数据。
    """
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    conn = connect()
    try:
        owned = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if owned is None:
            return None
        rows = _flow_rows_in_range(conn, tenant, order_id, start, end)
    finally:
        conn.close()
    groups: dict[str, dict] = {}
    for row in rows:
        instant = datetime.fromisoformat(row["business_time"]).replace(tzinfo=UTC)
        day = instant.astimezone(offset).date().isoformat()
        group = groups.get(day)
        if group is None:
            group = {
                "date": day,
                "payment_cents": 0,
                "reversal_cents": 0,
                "entry_count": 0,
                "first_business_time": instant,
                "last_business_time": instant,
            }
            groups[day] = group
        if row["entry_type"] == "payment":
            group["payment_cents"] += row["amount_cents"]
        else:
            group["reversal_cents"] += row["amount_cents"]
        group["entry_count"] += 1
        group["first_business_time"] = min(group["first_business_time"], instant)
        group["last_business_time"] = max(group["last_business_time"], instant)
    result = []
    for day in sorted(groups):
        group = groups[day]
        group["first_business_time"] = to_display(group["first_business_time"], offset)
        group["last_business_time"] = to_display(group["last_business_time"], offset)
        result.append(group)
    return result
