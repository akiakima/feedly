from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings

LOCAL_TZ = ZoneInfo(settings.timezone)


def to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def format_local(value: datetime | None, fmt: str = "%m/%d %H:%M") -> str:
    local = to_local(value)
    return local.strftime(fmt) if local else "-"


def relative_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    seconds = int((now - value).total_seconds())
    if seconds < 60:
        return "たった今"
    if seconds < 3600:
        return f"{seconds // 60}分前"
    if seconds < 86400:
        return f"{seconds // 3600}時間前"
    if seconds < 86400 * 7:
        return f"{seconds // 86400}日前"
    return format_local(value, "%Y/%m/%d")
