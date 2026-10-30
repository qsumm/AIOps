"""mute 策略链（FR-C4；03 §1.10）。返回 None=未屏蔽；0=MutedAll；1=MutedNotifyOnly。"""
import re
from datetime import datetime, time


def tag_match(filters: list[dict], tags: dict) -> bool:
    for f in filters or []:
        k, op, v = f.get("k", ""), f.get("op", "=="), f.get("v", "")
        actual = str(tags.get(k, ""))
        if op == "==" and actual != str(v):
            return False
        if op == "!=" and actual == str(v):
            return False
        if op == "=~" and not re.search(str(v), actual):
            return False
        if op == "in" and actual not in (v if isinstance(v, list) else [v]):
            return False
    return True


def _in_periodic(periodic: list[dict], now: datetime) -> bool:
    for p in periodic or []:
        days = p.get("days")
        if days and now.weekday() not in days:
            continue
        try:
            s = time(*map(int, p.get("start", "00:00").split(":")))
            e = time(*map(int, p.get("end", "23:59").split(":")))
        except ValueError:
            continue
        if s <= now.time() <= e:
            return True
    return False


def is_muted(mutes: list[dict], event_tags: dict, severity: int, now: datetime | None = None) -> int | None:
    """策略链顺序：TagFilter > severities > 时间模式（一次性区间 或 每日周期）> mute_type。"""
    now = now or datetime.utcnow()
    for m in mutes:
        if m.get("disabled"):
            continue
        if not tag_match(m.get("tag_filters"), event_tags):
            continue
        sevs = m.get("severities") or []
        if sevs and severity not in sevs:
            continue
        hit = False
        b, e = m.get("btime"), m.get("etime")
        if b and e:  # 一次性区间
            b = b if isinstance(b, datetime) else datetime.fromisoformat(str(b))
            e = e if isinstance(e, datetime) else datetime.fromisoformat(str(e))
            hit = b <= now <= e
        if not hit and m.get("periodic_mutes"):  # 每日周期（备份窗口）
            hit = _in_periodic(m["periodic_mutes"], now)
        if hit:
            return int(m.get("mute_type", 0))
    return None
