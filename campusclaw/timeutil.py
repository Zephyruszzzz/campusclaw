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
