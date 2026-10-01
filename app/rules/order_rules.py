import re
from datetime import datetime

ALLOWED_CURRENCIES = {"CNY", "USD", "EUR", "JPY"}

def assert_currency(currency: str) -> None:
    if currency not in ALLOWED_CURRENCIES:
        raise ValueError("unsupported currency")

# 业务发生时间必须是带时区偏移的完整日期时间（到秒），如 2026-10-02T15:04:05+08:00。
_FULL_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

def parse_business_time(value: str) -> datetime:
    """解析业务发生时间：必须为带时区偏移的完整日期时间（到秒），否则抛 ValueError。"""
    if not isinstance(value, str) or not _FULL_DATETIME.match(value):
        raise ValueError("invalid business time: expect full datetime with timezone offset, e.g. 2026-10-02T15:04:05+08:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid business time: expect full datetime with timezone offset, e.g. 2026-10-02T15:04:05+08:00") from None
    if parsed.tzinfo is None:
        raise ValueError("invalid business time: timezone offset is required")
    return parsed

def format_business_time(moment: datetime) -> str:
    """统一为带时区偏移、精确到秒的字符串表示（去掉亚秒部分）。"""
    return moment.replace(microsecond=0).isoformat()

def now_business_time() -> str:
    """服务按当前时间记账时使用的业务发生时间（本地时区、带偏移、到秒）。"""
    return format_business_time(datetime.now().astimezone())
