"""UTC 时间工具：所有入库时间统一为带 Z 的 ISO 8601 字符串。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().strftime(ISO_FORMAT)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)


def plus_hours(moment: datetime, hours: int) -> datetime:
    return moment + timedelta(hours=hours)


def to_local_display(value: str) -> str:
    """把入库的 UTC ISO 时间转成本地时区的可读串，仅用于页面展示。

    解析失败时原样返回，展示层不得影响数据本身。
    """
    try:
        moment = parse_iso(value).astimezone()
    except (ValueError, TypeError):
        return value
    return moment.strftime("%Y-%m-%d %H:%M")
