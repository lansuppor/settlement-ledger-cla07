import csv
import io
import sqlite3
import uuid
from pathlib import Path

from app.store.db import connect
from app.store.orders import LedgerError, _apply_payment_locked, _now

# 行级格式错误（单笔收款由 HTTP 入参校验拦截，批量文件内无法解析的行给出稳定原因）
LINE_ORDER_ID_REQUIRED = "order id is required"
LINE_INVALID_AMOUNT = "invalid amount_cents"
LINE_INVALID_SHAPE = "invalid payment line"
ORDER_NOT_FOUND = "order not found"

REQUIRED_HEADER = ["order_id", "amount_cents"]
OPTIONAL_HEADER = "installment_id"


class ImportFileError(Exception):
    """导入文件缺失或格式不合法（HTTP 层映射为 4xx）。"""


def _new_batch_id() -> str:
    return f"imp_{uuid.uuid4().hex}"


def parse_import_file(file_path: str) -> list[dict]:
    """读取并解析收款导入 CSV。

    表头必须为 order_id,amount_cents[,installment_id]；逐行返回
    {line_no, order_id, amount_cents, installment_id, error}：文件级问题
    （文件缺失、表头错误）抛 ImportFileError；单行格式问题在行内以 error 标注，
    受理时作为该行的拒绝原因，不影响其他行。
    """
    path = Path(file_path)
    if not path.is_file():
        raise ImportFileError("import file not found")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ImportFileError("import file not found") from error

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ImportFileError("invalid import file: missing header")
    header = [name.strip() for name in rows[0]]
    if len(header) < 2 or header[:2] != REQUIRED_HEADER or (
        len(header) > 2 and header[2] != OPTIONAL_HEADER
    ) or len(header) > 3:
        raise ImportFileError(
            "invalid import file: header must be order_id,amount_cents[,installment_id]"
        )

    lines = []
    for raw in rows[1:]:
        if not raw or all(cell == "" for cell in raw):
            # 跳过空行（含文件末尾空行），空行不占行号结论
            continue
        line_no = len(lines) + 1
        if len(raw) not in (2, 3):
            lines.append({"line_no": line_no, "order_id": "", "amount_cents": None,
                          "installment_id": None, "error": LINE_INVALID_SHAPE})
            continue
        order_id = raw[0].strip()
        amount_raw = raw[1].strip()
        installment_id = (raw[2].strip() if len(raw) == 3 else "") or None
        if not order_id:
            lines.append({"line_no": line_no, "order_id": "", "amount_cents": None,
                          "installment_id": installment_id, "error": LINE_ORDER_ID_REQUIRED})
            continue
        try:
            amount_cents = int(amount_raw)
        except (TypeError, ValueError):
            lines.append({"line_no": line_no, "order_id": order_id, "amount_cents": None,
                          "installment_id": installment_id, "error": LINE_INVALID_AMOUNT})
            continue
        # 金额必须为正：与单笔收款入参 gt=0 同一约束，非正金额按行级格式错误拒绝
        error = None if amount_cents > 0 else LINE_INVALID_AMOUNT
        lines.append({"line_no": line_no, "order_id": order_id, "amount_cents": amount_cents,
                      "installment_id": installment_id, "error": error})
    return lines


def create_import_batch(tenant: str, file_path: str, originator: str | None) -> dict:
    """按（租户, 文件路径, 发起方）幂等建批次并把逐行输入快照为待受理行。

    发起方未声明时沿用单笔收款缺省规则（取租户标识）。结构性非法的行在建批次时
    即落为 rejected 结论（原因确定，不依赖账面，重启不丢）。重复提交命中已有批次，
    写事务串行化保证并发下同一（文件路径, 发起方）也只建一个批次。
    """
    effective_originator = originator or tenant
    parsed = parse_import_file(file_path)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT batch_id FROM import_batches WHERE tenant=? AND file_path=? AND originator=?",
            (tenant, file_path, effective_originator),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return {"batch_id": existing["batch_id"], "recreated": True}

        batch_id = _new_batch_id()
        now = _now()
        try:
            conn.execute(
                """INSERT INTO import_batches(tenant, batch_id, file_path, originator, total_lines, status, created_at)
                   VALUES(?,?,?,?,?,'processing',?)""",
                (tenant, batch_id, file_path, effective_originator, len(parsed), now),
            )
        except sqlite3.IntegrityError:
            # 唯一索引兜底：并发首次提交同一（文件路径, 发起方）只建一个批次
            conn.execute("ROLLBACK")
            return create_import_batch(tenant, file_path, originator)
        for line in parsed:
            if line["error"] is not None:
                # 确定性的行级格式错误：直接留结论，无需进入受理循环
                conn.execute(
                    """INSERT INTO import_batch_lines(tenant, batch_id, line_no, order_id, amount_cents,
                                                      installment_id, status, reject_reason, processed_at)
                       VALUES(?,?,?,?,?,?,'rejected',?,?)""",
                    (tenant, batch_id, line["line_no"], line["order_id"], line["amount_cents"],
                     line["installment_id"], line["error"], now),
                )
            else:
                conn.execute(
                    """INSERT INTO import_batch_lines(tenant, batch_id, line_no, order_id, amount_cents,
                                                      installment_id, status)
                       VALUES(?,?,?,?,?,?,'pending')""",
                    (tenant, batch_id, line["line_no"], line["order_id"], line["amount_cents"],
                     line["installment_id"]),
                )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {"batch_id": batch_id, "recreated": False}


def process_pending_lines(tenant: str, batch_id: str, limit: int | None = None) -> None:
    """按行的先后顺序受理批次内尚未完成的行；每行独立事务，失败只拒绝该行。

    每行的收款留痕、账面/期次推进与逐行结论在同一事务内提交，不存在半行残留。
    服务中断时已完成行的结论已落库，续跑只补齐剩余 pending 行，不重复记账、不重复留痕。
    limit 限制本次最多处理的行数（用于分批续跑/测试中断恢复）。
    """
    conn = connect()
    try:
        pending = conn.execute(
            "SELECT line_no FROM import_batch_lines WHERE tenant=? AND batch_id=? AND status='pending' "
            "ORDER BY line_no",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()

    for processed, row in enumerate(pending):
        if limit is not None and processed >= limit:
            break
        _process_one_line(tenant, batch_id, row["line_no"])

    # 全部行都有结论时收尾批次（独立短事务；可能已被并发续跑者收尾）
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM import_batch_lines WHERE tenant=? AND batch_id=? AND status='pending'",
            (tenant, batch_id),
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute(
                "UPDATE import_batches SET status='completed', completed_at=? "
                "WHERE tenant=? AND batch_id=? AND status='processing'",
                (_now(), tenant, batch_id),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _process_one_line(tenant: str, batch_id: str, line_no: int) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        line = conn.execute(
            "SELECT order_id, amount_cents, installment_id, status FROM import_batch_lines "
            "WHERE tenant=? AND batch_id=? AND line_no=?",
            (tenant, batch_id, line_no),
        ).fetchone()
        if line is None or line["status"] != "pending":
            # 并发续跑：该行已被其他受理者处理
            conn.execute("ROLLBACK")
            return
        if not line["order_id"] or line["amount_cents"] is None or line["amount_cents"] <= 0:
            # 兜底：pending 行理论上均已通过解析校验
            _finish_rejected(conn, tenant, batch_id, line_no, LINE_INVALID_AMOUNT)
            conn.execute("COMMIT")
            return

        originator = conn.execute(
            "SELECT originator FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()["originator"]
        try:
            result = _apply_payment_locked(
                conn, tenant, line["order_id"], line["amount_cents"], originator,
                installment_id=line["installment_id"],
            )
        except LedgerError as error:
            # 业务拒绝原因与单笔收款完全一致；回滚收款的任何部分效果，仅落逐行结论
            _finish_rejected(conn, tenant, batch_id, line_no, str(error))
            conn.execute("COMMIT")
            return
        if result is None:
            # 订单不存在（含跨租户）：与单笔收款一致按 order not found 拒绝，不泄漏对象是否存在
            _finish_rejected(conn, tenant, batch_id, line_no, ORDER_NOT_FOUND)
            conn.execute("COMMIT")
            return

        conn.execute(
            """UPDATE import_batch_lines
               SET status='accepted', reject_reason=NULL, record_id=?, paid_cents=?,
                   outstanding_cents=?, order_status=?, processed_at=?
               WHERE tenant=? AND batch_id=? AND line_no=?""",
            (result["record_id"], result["paid_cents"], result["outstanding_cents"],
             result["status"], _now(), tenant, batch_id, line_no),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _finish_rejected(conn, tenant: str, batch_id: str, line_no: int, reason: str) -> None:
    conn.execute(
        """UPDATE import_batch_lines
           SET status='rejected', reject_reason=?, paid_cents=NULL, outstanding_cents=NULL,
               order_status=NULL, record_id=NULL, processed_at=?
           WHERE tenant=? AND batch_id=? AND line_no=?""",
        (reason, _now(), tenant, batch_id, line_no),
    )


def run_import(tenant: str, file_path: str, originator: str | None) -> dict:
    """批量收款导入入口：幂等建批次（或命中既有批次）→ 续跑剩余行 → 返回逐行结论。"""
    created = create_import_batch(tenant, file_path, originator)
    process_pending_lines(tenant, created["batch_id"])
    result = get_import(tenant, created["batch_id"])
    if result is None:  # 理论不可达：批次在本调用内刚建或刚命中
        raise ImportFileError("import batch not found")
    return result


def _line_result(row) -> dict:
    status = row["status"]
    if status == "pending":
        # 中断的批次：已完成行有结论，未完成行如实呈现为待受理，可凭批次标识续跑
        return {
            "line_no": row["line_no"],
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "installment_id": row["installment_id"],
            "result": "pending",
            "reject_reason": None,
            "record_id": None,
            "paid_cents": None,
            "outstanding_cents": None,
            "order_status": None,
        }
    if status == "accepted":
        return {
            "line_no": row["line_no"],
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "installment_id": row["installment_id"],
            "result": "accepted",
            "reject_reason": None,
            "record_id": row["record_id"],
            "paid_cents": row["paid_cents"],
            "outstanding_cents": row["outstanding_cents"],
            "order_status": row["order_status"],
        }
    return {
        "line_no": row["line_no"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "installment_id": row["installment_id"],
        "result": "rejected",
        "reject_reason": row["reject_reason"],
        "record_id": None,
        "paid_cents": None,
        "outstanding_cents": None,
        "order_status": None,
    }


def get_import(tenant: str, batch_id: str) -> dict | None:
    """按批次标识查询批次与逐行结论；跨租户/不存在返回 None（按不存在处理）。"""
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT batch_id, file_path, originator, total_lines, status, created_at, completed_at "
            "FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if batch is None:
            return None
        rows = conn.execute(
            """SELECT line_no, order_id, amount_cents, installment_id, status, reject_reason,
                      record_id, paid_cents, outstanding_cents, order_status
               FROM import_batch_lines WHERE tenant=? AND batch_id=? ORDER BY line_no""",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    results = [_line_result(row) for row in rows]
    return {
        "batch_id": batch["batch_id"],
        "file_path": batch["file_path"],
        "originator": batch["originator"],
        "status": batch["status"],
        "total_lines": batch["total_lines"],
        "processed_count": sum(1 for item in results if item["result"] != "pending"),
        "accepted_count": sum(1 for item in results if item["result"] == "accepted"),
        "rejected_count": sum(1 for item in results if item["result"] == "rejected"),
        "created_at": batch["created_at"],
        "completed_at": batch["completed_at"],
        "results": results,
    }
