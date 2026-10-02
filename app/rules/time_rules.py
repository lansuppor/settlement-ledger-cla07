import re
from datetime import UTC, datetime, timedelta, timezone

# 完整日期时间：年-月-日 时:分:秒，秒为最小精度（不接受仅到分钟、不接受小数秒），
# 且必须带时区偏移（Z 或 ±HH:MM）；历法合法性再交由 datetime.fromisoformat 校验。
_BUSINESS_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")

# 对账时区偏移：Z 或 ±HH:MM。
_TZ_OFFSET_RE = re.compile(r"^(?:Z|[+-]\d{2}:\d{2})$")


class BusinessTimeError(ValueError):
    """业务发生时间缺失或不合法。"""


def parse_business_time(value: str | None) -> datetime:
    """把调用方提供的业务发生时间解析为带时区的时间，保留其登记时的偏移。

    取值规则：必须是带时区偏移的完整日期时间、最小精确到秒；非法（缺日期/时间/偏移、
    精度不足、小数秒、历法不存在等）抛 BusinessTimeError。返回值保留原偏移，
    需要比较或落库时由调用方换算到 UTC。
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
    return dt


def to_storage(dt: datetime) -> str:
    """落库文本：统一 UTC、到秒、带偏移（如 2026-10-02T08:30:00+00:00），供区间比较。"""
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def to_display(dt: datetime) -> str:
    """读回文本：保留登记时的偏移写法、到秒（如 2026-10-02T08:30:00+08:00；Z 规范化为 +00:00）。"""
    return dt.isoformat(timespec="seconds")


def parse_tz_offset(value: str | None) -> timezone:
    """把调用方给出的对账时区偏移（Z 或 ±HH:MM）解析为 timezone；非法抛 BusinessTimeError。"""
    if not isinstance(value, str) or not _TZ_OFFSET_RE.match(value):
        raise BusinessTimeError("tz must be a timezone offset like +08:00 or Z")
    if value == "Z":
        return UTC
    sign = 1 if value[0] == "+" else -1
    hours = int(value[1:3])
    minutes = int(value[4:6])
    if hours > 23 or minutes > 59:
        raise BusinessTimeError("tz must be a timezone offset like +08:00 or Z")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def tz_offset_text(tz: timezone) -> str:
    """把对账时区偏移规范化为 ±HH:MM 文本（如 +08:00）。"""
    total = int(tz.utcoffset(None).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"
