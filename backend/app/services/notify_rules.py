"""通知规则求值与派发目标解析（FR-423；D154）。

四维筛选语义（**照抄夜莺** `models/notify_rule.go:50-53` + `zh__docs__notifications__rules.md`）：

| 维度 | 字段 | 留空的含义（易错点） |
|---|---|---|
| 适用级别 | `severities` | **不是不限制，是一条都不匹配** → 该条配置等于停用 |
| 适用时段 | `time_ranges` | 不限制；支持跨零点（21:00–09:00） |
| 适用标签 | `label_keys` | 不限制；按**事件标签**匹配（不匹配注解） |
| 适用属性 | `attributes` | 不限制；按平台加的事件元信息匹配（业务组/数据源/规则/是否恢复） |

与老路径的关系（**互斥，不是叠加**）：
1. 事件命中任一「启用的新通知规则」→ 走新路径，**不再走** `channels_by_level`；
2. 否则 → 走老路径（行为逐字节不变）；
3. 两者都没命中 → 老的 S1 兜底（全渠道广播 + degraded 说明）。

执行顺序（四段，与夜莺一致）：**规则级 Pipeline → 屏蔽 → 落库+订阅 → 通知规则（内含通知级 Pipeline）**。
关键：**Pipeline 在屏蔽之前**（它改写的标签，屏蔽规则要能匹配到）。
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from app.services.mute import tag_match

# 适用时段按**北京时间**判定（与平台既有「生效时段」口径一致，D144 已统一），
# 而库内时间戳一律 UTC —— 换算只发生在这里。
TZ_OFFSET = timedelta(hours=8)


def _parse_hhmm(v: str, default: time) -> time:
    try:
        h, m = str(v or "").split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, IndexError):
        return default


def in_time_ranges(ranges: list[dict] | None, now_utc: datetime) -> bool:
    """适用时段判定。留空 = 不限制（恒真）。

    `weekdays` 用 0=周一 … 6=周日（与平台既有生效时段一致，见 D144）。
    支持**跨零点**（start > end 时，跨到次日算命中）。
    """
    if not ranges:
        return True
    local = now_utc + TZ_OFFSET
    for r in ranges:
        days = r.get("weekdays")
        if days:
            # 平台既有的星期口径：0=周一；Python weekday() 也是 0=周一
            if local.weekday() not in days:
                continue
        s = _parse_hhmm(r.get("start", ""), time(0, 0))
        e = _parse_hhmm(r.get("end", ""), time(23, 59))
        cur = local.time()
        if s <= e:
            if s <= cur <= e:
                return True
        else:                       # 跨零点
            if cur >= s or cur <= e:
                return True
    return False


def match_config(cfg: dict, event: dict, now_utc: datetime) -> bool:
    """一条通知配置是否命中该事件。

    event 需含：severity、tags（事件标签）、attributes（事件属性）。
    """
    if not isinstance(cfg, dict):
        return False
    # ① 级别：留空 = 一条都不匹配（等于停用该条配置）
    sevs = cfg.get("severities")
    if not sevs:
        return False
    if int(event.get("severity", 0)) not in [int(s) for s in sevs]:
        return False
    # ② 时段：留空 = 不限制
    if not in_time_ranges(cfg.get("time_ranges"), now_utc):
        return False
    # ③ 标签：按事件标签匹配（不匹配注解）
    if not tag_match(cfg.get("label_keys") or [], event.get("tags") or {}):
        return False
    # ④ 属性：按事件属性匹配
    if not tag_match(cfg.get("attributes") or [], event.get("attributes") or {}):
        return False
    return True


def event_attributes(ev, rule=None, busi_group: str = "") -> dict:
    """事件的「属性」维度（平台加的元信息，与用户自带标签区分开）。"""
    attrs = {
        "rule_id": str(getattr(rule, "id", "") or ""),
        "rule_name": str(getattr(rule, "name", "") or ""),
        "host": str(getattr(ev, "host_ident", "") or ""),
        "metric": str(getattr(ev, "metric", "") or ""),
        "is_recovered": "1" if getattr(ev, "is_recovered", False) else "0",
    }
    if busi_group:
        attrs["busi_group"] = busi_group
    return attrs


def resolve_targets(rules: list, event: dict, now_utc: datetime) -> list[dict]:
    """从**启用的新通知规则**里解析出投递目标。

    返回 [{channel_id, template_id, targets, rule_id, rule_name, pipeline_id}]。
    没有命中任何新规则 → 返回空列表（调用方据此回落老路径）。
    """
    out: list[dict] = []
    for rule in rules:
        if not getattr(rule, "enabled", True):
            continue
        for cfg in (getattr(rule, "configs", None) or []):
            if not match_config(cfg, event, now_utc):
                continue
            out.append({
                "channel_id": cfg.get("channel_id"),
                "template_id": cfg.get("template_id"),
                "targets": cfg.get("targets") or [],
                "pipeline_id": cfg.get("pipeline_id"),
                "rule_id": getattr(rule, "id", None),
                "rule_name": getattr(rule, "name", ""),
            })
    return out


def match_subscribe(sub: dict, event: dict, now_utc: datetime) -> bool:
    """订阅规则是否命中该事件（筛选语义与通知配置一致）。"""
    f = (sub or {}).get("severity_filter") or {}
    sevs = f.get("severities")
    if sevs and int(event.get("severity", 0)) not in [int(s) for s in sevs]:
        return False
    if not in_time_ranges(f.get("time_ranges"), now_utc):
        return False
    if not tag_match(f.get("label_keys") or [], event.get("tags") or {}):
        return False
    if not tag_match(f.get("attributes") or [], event.get("attributes") or {}):
        return False
    return True
