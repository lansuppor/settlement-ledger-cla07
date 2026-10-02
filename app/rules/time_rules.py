import re
from datetime import UTC, datetime, timedelta, timezone

# 完整日期时间：年-月-日 时:分:秒，秒为最小精度（不接受仅到分钟、不接受小数秒），
# 且必须带时区偏移（Z 或 ±HH:MM）；历法合法性再交由 datetime.fromisoformat 校验。
_BUSINESS_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")

# 对账时区偏移：Z 或 ±HH:MM（如 +08:00、-05:00），用于把流水换算到该偏移后按日历日归组。
_OFFSET_RE = re.compile(r"^(?:Z|[+-]\d{2}:\d{2})$")


class BusinessTimeError(ValueError):
    """业务发生时间缺失或不合法。"""


class OffsetError(ValueError):
    """对账时区偏移缺失或不合法。"""


def parse_business_time(value: str | None) -> datetime:
    """把调用方提供的业务发生时间解析为带时区的时间（保留调用方写法所用的时区偏移）。

    取值规则：必须是带时区偏移的完整日期时间、最小精确到秒；非法（缺日期/时间/偏移、
    精度不足、小数秒、历法不存在等）抛 BusinessTimeError。

    返回的 datetime 携带调用方给出的偏移（``Z`` 按 +00:00）；同一时刻的不同写法得到
    相同的 UTC 基准，比较与存储一律先换算到 UTC，不因写法不同而视为不同时刻。
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


def parse_offset(value: str | None) -> timezone:
    """把对账时区偏移文本解析为固定时区（Z 或 ±HH:MM）；非法或缺省抛 OffsetError。"""
    if not isinstance(value, str) or not _OFFSET_RE.match(value):
        raise OffsetError("offset must be a timezone offset like +08:00 or Z")
    text = "00:00" if value == "Z" else value[1:]
    sign = 1 if value == "Z" or value[0] == "+" else -1
    hours, minutes = (int(part) for part in text.split(":"))
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def offset_minutes(dt: datetime) -> int:
    """该带时区时间相对 UTC 的偏移分钟数（登记时偏移写法的持久化形态）。"""
    return int(dt.utcoffset().total_seconds()) // 60


def timezone_from_minutes(minutes: int) -> timezone:
    """把偏移分钟数还原为固定时区。"""
    return timezone(timedelta(minutes=minutes))


def to_storage(dt: datetime) -> str:
    """落库文本：统一 UTC、到秒、带偏移（如 2026-10-02T08:30:00+00:00）。"""
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def to_display(dt: datetime, offset: timezone) -> str:
    """出参文本：按指定时区偏移渲染、到秒（如 2026-10-02T16:30:00+08:00）。

    时间基准不变，只换写法；同一 UTC 时刻配合登记时偏移即还原登记时的偏移写法。
    """
    return dt.astimezone(offset).isoformat(timespec="seconds")
