"""事件 Pipeline 处理器（FR-423；D154）。

Pipeline = 有序处理器列表，每个处理器一种动作。**两个入口**（与夜莺同构）：
- **规则级**：挂在通知规则上，影响所有下游（在屏蔽之前跑）；
- **通知级**：挂在单条通知配置上，只影响这一条配置的这一次发送。

处理器类型：
| processor        | 作用                                     | 影响事件身份 |
|------------------|------------------------------------------|--------------|
| `set_tag`        | 改写事件标签（新增/覆盖/删除）            | 是（标签进指纹） |
| `enrich`         | 富化上下文（按主机查业务组等）           | 是（新增维度） |
| `drop`           | 丢弃事件（**不产生事件、不落库、不通知**） | — |
| `set_annotation` | 写注解（**不参与匹配**，只进消息）        | 否 |

**顺序铁律**：Pipeline 在**屏蔽之前**执行 —— 这样 Pipeline 改写出来的标签，
屏蔽规则才能匹配到（夜莺踩过"配了屏蔽仍收到告警"的坑，见 ADR-003）。
"""
from __future__ import annotations

import logging

log = logging.getLogger("pipeline")

# 允许的处理器类型（白名单：处理器名字可来自用户输入，必须收敛）
PROCESSORS = ("set_tag", "del_tag", "enrich", "drop", "set_annotation")

_MAX_TAGS = 16
_MAX_ANNOTATIONS = 16
_MAX_VALUE_LEN = 255


def _clean_tags(tags: dict) -> dict:
    out: dict[str, str] = {}
    for k, v in list((tags or {}).items())[:_MAX_TAGS]:
        if not isinstance(k, str) or not k.strip():
            continue
        if v is None or isinstance(v, (dict, list, tuple, set)):
            continue
        s = str(v).strip()
        if s:
            out[k.strip()[:64]] = s[:_MAX_VALUE_LEN]
    return out


def apply_pipeline(pipelines: list[dict] | None, event: dict) -> tuple[dict, bool]:
    """执行一串 Pipeline 处理器。

    返回 (处理后的事件, 是否被丢弃)。

    event 结构：{"tags": {...}, "annotations": {...}, ...}
    `drop` 处理器 → 返回 (event, True)，调用方**不再产生该事件**。
    """
    ev = dict(event or {})
    ev["tags"] = _clean_tags(ev.get("tags"))
    ann = _clean_tags(ev.get("annotations"))

    for p in pipelines or []:
        if not isinstance(p, dict):
            continue
        kind = str(p.get("processor") or "").strip()
        if kind not in PROCESSORS:
            log.warning("忽略未知 Pipeline 处理器：%s", kind)
            continue
        try:
            if kind == "set_tag":
                key = str(p.get("key") or "").strip()[:64]
                if key:
                    ev["tags"][key] = str(p.get("value") or "")[:_MAX_VALUE_LEN]
            elif kind == "del_tag":
                key = str(p.get("key") or "").strip()[:64]
                ev["tags"].pop(key, None)
            elif kind == "enrich":
                # 富化来源：目前只有 host_busi_group（按主机查业务组）。
                # 未命中不写键（**不凭空造字段**）。
                if str(p.get("source") or "") == "host_busi_group":
                    v = _host_busi_group(ev.get("tags", {}).get("host") or ev.get("host_ident") or "")
                    if v:
                        ev["tags"][str(p.get("target_tag") or "busi_group")[:64]] = v
            elif kind == "set_annotation":
                key = str(p.get("key") or "").strip()[:64]
                if key and len(ann) < _MAX_ANNOTATIONS:
                    ann[key] = str(p.get("value") or "")[:_MAX_VALUE_LEN]
            elif kind == "drop":
                return ev, True
        except Exception:  # noqa: BLE001 —— 单个处理器异常不得中断整条链
            log.exception("Pipeline 处理器异常：%s", kind)
    ev["annotations"] = ann
    return ev, False


def _host_busi_group(ident: str) -> str:
    """按主机查业务组名（enrich 处理器用）。

    查不到就返回空串（调用方不写该键）——**不凭空造字段**。
    """
    if not ident:
        return ""
    try:
        return _q_sync(ident)
    except Exception:  # noqa: BLE001
        log.exception("enrich：查主机业务组失败 ident=%s", ident)
        return ""


def _q_sync(ident: str) -> str:
    """同步查主机业务组。

    Pipeline 在事件消费链路里**同步**调用（事件产生与消费在同一事件循环），
    不能再开一个事件循环跑异步查询——故用同步 Session。
    """
    from app.db.session import SessionLocal
    from app.models.host import Host
    from sqlalchemy import select

    with SessionLocal() as db:
        h = db.execute(select(Host).where(Host.ident == ident)).scalar_one_or_none()
        if h is None:
            return ""
        tags = getattr(h, "tags", None) or {}
        return str(tags.get("busi_group") or "")
