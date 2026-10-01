import re
from datetime import UTC, datetime

# 完整日期时间：年-月-日 时:分:秒，秒为最小精度（不接受仅到分钟、不接受小数秒），
# 且必须带时区偏移（Z 或 ±HH:MM）；历法合法性再交由 datetime.fromisoformat 校验。
_BUSINESS_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")


class BusinessTimeError(ValueError):
    """业务发生时间缺失或不合法。"""


def parse_business_time(value: str | None) -> datetime:
    """把调用方提供的业务发生时间解析为带时区的时间并统一换算到 UTC。

    取值规则：必须是带时区偏移的完整日期时间、最小精确到秒；非法（缺日期/时间/偏移、
    精度不足、小数秒、历法不存在等）抛 BusinessTimeError。
    """
    if not isinstance(value, str) or not _BUSINESS_TIME_RE.match(value):
        raise BusinessTimeError(
            "business_time must be a complete date-time with timezone offset, precise to the second"
        )
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as error:
        raise BusinessTimeError("business_time is not a valid date-time") from error
    if dt.tzinfo is None or dt.microsecond != 0:
        raise BusinessTimeError(
            "business_time must be a complete date-time with timezone offset, precise to the second"
        )
    return dt.astimezone(UTC)


def to_storage(dt: datetime) -> str:
    """落库/出参文本：统一 UTC、到秒、带偏移（如 2026-10-02T08:30:00+00:00）。"""
    return dt.astimezone(UTC).isoformat(timespec="seconds")
