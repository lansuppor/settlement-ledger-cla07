"""批量收款导入：按 CSV 文件逐行受理，部分失败可解释、可续跑、可重放。

每行的业务校验与账面推进复用 orders._apply_payment，与单笔收款完全等同；
行内“收款 + 结论留痕”在同一事务内原子提交，任一行失败只拒绝该行。
同一（租户, 文件路径, 发起方标识）只对应一个导入批次：重复提交返回首次结论，
中断后续跑只补齐缺失行，已生效行不重复记账、不重复留痕。
"""

import csv
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.store import orders
from app.store.db import connect
from app.store.orders import LedgerError

# 行级可区分拒绝原因（账本原因与单笔收款一致，此处仅补充文件/行级原因）
INVALID_LINE = "invalid line"
ORDER_NOT_FOUND = "order not found"


class ImportFileError(Exception):
    """导入文件不可读（HTTP 层映射为 400）。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_batch_id() -> str:
    return f"imp_{uuid.uuid4().hex}"


def _parse_file(file_path: str) -> list[dict]:
    """解析 CSV：每行 `order_id,amount_cents[,installment_id]`；首行若为表头则跳过。

    返回数据行列表（line_no 从 1 起按文件顺序编号）。无法解析的行不抛出，
    而是标记 error，由后续按行拒绝，不影响同批其他行。
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except OSError as error:
        raise ImportFileError(f"import file not readable: {file_path}") from error

    lines: list[dict] = []
    for raw in csv.reader(text.splitlines()):
        fields = [field.strip() for field in raw]
        if not any(fields):
            continue  # 空行不占行号
        if not lines and fields[0].lower() == "order_id":
            continue  # 表头
        line_no = len(lines) + 1
        order_id = fields[0] if fields else ""
        amount_cents: int | None = None
        installment_id: str | None = None
        error = None
        if not order_id or len(fields) < 2 or len(fields) > 3:
            error = INVALID_LINE
        else:
            try:
                amount_cents = int(fields[1])
            except ValueError:
                error = INVALID_LINE
            if len(fields) == 3 and fields[2]:
                installment_id = fields[2]
        lines.append(
            {
                "line_no": line_no,
                "order_id": order_id or None,
                "amount_cents": amount_cents,
                "installment_id": installment_id,
                "error": error,
            }
        )
    return lines


def _find_batch(tenant: str, file_path: str, originator: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT batch_id, file_path, originator, status, total_lines, created_at, finished_at "
            "FROM import_batches WHERE tenant=? AND file_path=? AND originator=?",
            (tenant, file_path, originator),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _ensure_batch(tenant: str, file_path: str, originator: str, total_lines: int) -> dict:
    """按（租户, 文件路径, 发起方标识）取或建批次；并发下唯一索引兜底，只建一次。"""
    existing = _find_batch(tenant, file_path, originator)
    if existing is not None:
        return existing
    batch_id = _new_batch_id()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO import_batches(tenant, batch_id, file_path, originator, status, total_lines, created_at) "
            "VALUES(?,?,?,?,'running',?,?)",
            (tenant, batch_id, file_path, originator, total_lines, _now()),
        )
    except sqlite3.IntegrityError:
        # 并发创建撞唯一索引：以已存在的批次为准
        pass
    finally:
        conn.close()
    return _find_batch(tenant, file_path, originator)


def _process_line(tenant: str, batch_id: str, line: dict, originator: str) -> None:
    """受理一行：收款推进与行结论留痕在同一事务内提交，失败行无任何部分效果残留。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute(
            "SELECT 1 FROM import_rows WHERE tenant=? AND batch_id=? AND line_no=?",
            (tenant, batch_id, line["line_no"]),
        ).fetchone()
        if recorded is not None:
            # 续跑/并发下该行已有结论：不重复记账、不重复留痕
            conn.execute("ROLLBACK")
            return

        outcome: dict
        if line["error"] is not None:
            outcome = {"outcome": "rejected", "reason": line["error"]}
        else:
            try:
                result = orders._apply_payment(
                    conn, tenant, line["order_id"], line["amount_cents"], originator,
                    installment_id=line["installment_id"],
                )
            except LedgerError as error:
                # 与单笔收款一致的可区分拒绝原因；事务回滚后该行不留任何痕迹
                outcome = {"outcome": "rejected", "reason": str(error)}
            else:
                if result is None:
                    # 订单不存在或跨租户：一律按不存在处理，不泄漏对象是否存在
                    outcome = {"outcome": "rejected", "reason": ORDER_NOT_FOUND}
                else:
                    outcome = {
                        "outcome": "accepted",
                        "record_id": result["record_id"],
                        "paid_cents": result["paid_cents"],
                        "outstanding_cents": result["outstanding_cents"],
                        "order_status": result["status"],
                    }
        conn.execute(
            "INSERT INTO import_rows(tenant, batch_id, line_no, order_id, installment_id, outcome, reason, "
            "record_id, paid_cents, outstanding_cents, order_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant, batch_id, line["line_no"], line["order_id"], line["installment_id"],
                outcome["outcome"], outcome.get("reason"), outcome.get("record_id"),
                outcome.get("paid_cents"), outcome.get("outstanding_cents"),
                outcome.get("order_status"), _now(),
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        # 并发续跑撞行主键：另一执行者已记录该行，本行支付随事务回滚，不产生部分效果
        conn.execute("ROLLBACK")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def import_payments(tenant: str, file_path: str, originator: str) -> dict:
    """受理一个批量收款导入文件，返回批次标识与逐行结论。

    同（文件路径, 发起方标识）重复提交视为同一批次：已完成的批次直接返回首次结论；
    中断的批次续跑，只补齐尚未记录结论的行。
    """
    batch = _find_batch(tenant, file_path, originator)
    if batch is not None and batch["status"] == "completed":
        # 重放已完成批次：直接返回首次导入的结论，无需再读文件
        return get_batch(tenant, batch["batch_id"])

    lines = _parse_file(file_path)
    if batch is None:
        batch = _ensure_batch(tenant, file_path, originator, len(lines))

    done = _done_line_nos(tenant, batch["batch_id"])
    for line in lines:
        if line["line_no"] in done:
            continue
        _process_line(tenant, batch["batch_id"], line, batch["originator"])

    conn = connect()
    try:
        conn.execute(
            "UPDATE import_batches SET status='completed', finished_at=?, total_lines=? "
            "WHERE tenant=? AND batch_id=?",
            (_now(), len(lines), tenant, batch["batch_id"]),
        )
    finally:
        conn.close()
    return get_batch(tenant, batch["batch_id"])


def _done_line_nos(tenant: str, batch_id: str) -> set[int]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT line_no FROM import_rows WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    return {row["line_no"] for row in rows}


def get_batch(tenant: str, batch_id: str) -> dict | None:
    """凭批次标识查询逐行结论；批次不存在或跨租户返回 None（按不存在处理）。"""
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT batch_id, file_path, originator, status, total_lines, created_at, finished_at "
            "FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if batch is None:
            return None
        rows = conn.execute(
            "SELECT line_no, order_id, installment_id, outcome, reason, record_id, "
            "paid_cents, outstanding_cents, order_status "
            "FROM import_rows WHERE tenant=? AND batch_id=? ORDER BY line_no",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    return {**dict(batch), "results": [dict(row) for row in rows]}
