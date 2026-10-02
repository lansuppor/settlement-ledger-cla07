"""订单账面条件检索的参数校验与续取标记编解码。

续取标记是不透明字符串：仅承载稳定排序键（上一页末尾订单标识），不含任何账面
快照，因此翻页过程中单据的收款、冲正、退款等变更不会让已返回过的订单被重复
返回，也不会因偏移漂移而跳过订单。过滤条件由调用方在每次请求时原样带上。
"""

import base64
import json

# 状态过滤取值沿用订单对象的 status 词表：accepted=未结清，settled=已结清
SEARCH_STATUSES = ("accepted", "settled")
DEFAULT_PAGE_SIZE = 20

_CURSOR_VERSION = "v1"


class SearchParamsError(ValueError):
    """检索条件/分页参数不合法（HTTP 层映射为 400）。"""


def _parse_page_size(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_PAGE_SIZE
    try:
        size = int(raw)
    except ValueError as error:
        raise SearchParamsError("page_size must be a positive integer") from error
    if size <= 0:
        raise SearchParamsError("page_size must be a positive integer")
    return size


def _parse_amount_bound(name: str, raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise SearchParamsError(f"{name} must be a non-negative integer") from error
    if value < 0:
        raise SearchParamsError(f"{name} must be a non-negative integer")
    return value


def _parse_has_installment(raw: str | None) -> bool | None:
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise SearchParamsError("has_installment must be 'true' or 'false'")


def normalize_query(
    *,
    status: str | None,
    currency: str | None,
    amount_min: str | None,
    amount_max: str | None,
    originator: str | None,
    has_installment: str | None,
    page_size: str | None,
) -> dict:
    """校验并归一化原始查询串；任一不合法抛 SearchParamsError，不改动任何数据。"""
    size = _parse_page_size(page_size)
    if status is not None and status not in SEARCH_STATUSES:
        raise SearchParamsError("unsupported status filter")
    low = _parse_amount_bound("amount_min", amount_min)
    high = _parse_amount_bound("amount_max", amount_max)
    if low is not None and high is not None and low > high:
        raise SearchParamsError("amount_min must not be greater than amount_max")
    if currency is not None and not currency:
        raise SearchParamsError("currency filter must not be empty")
    if originator is not None and not originator:
        raise SearchParamsError("originator filter must not be empty")
    has_plan = _parse_has_installment(has_installment)
    return {
        "status": status,
        "currency": currency,
        "amount_min": low,
        "amount_max": high,
        "originator": originator,
        "has_installment": has_plan,
        "page_size": size,
    }


def encode_cursor(order_id: str) -> str:
    payload = json.dumps({"v": _CURSOR_VERSION, "after": order_id}, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(token: str) -> str:
    """解码续取标记为上一页末尾订单标识；无法解析或内容不合法抛 SearchParamsError。"""
    try:
        padding = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise SearchParamsError("cursor is not parseable") from error
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise SearchParamsError("cursor is not parseable")
    after = payload.get("after")
    if not isinstance(after, str) or not after:
        raise SearchParamsError("cursor is not parseable")
    return after
