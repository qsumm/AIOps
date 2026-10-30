"""时区口径：平台 UI 统一显示北京时间，凡"今日"类统计必须用北京日界。

容器内 datetime.utcnow() 是 UTC；北京无夏令时，固定 +08:00。
"""
from datetime import datetime, timedelta

BEIJING_OFFSET = timedelta(hours=8)


def beijing_day_start_utc(now: datetime | None = None) -> datetime:
    """返回"当前北京时间所在自然日"的零点，换算成 UTC 供数据库比较。"""
    now = now or datetime.utcnow()
    bj = now + BEIJING_OFFSET
    return bj.replace(hour=0, minute=0, second=0, microsecond=0) - BEIJING_OFFSET
