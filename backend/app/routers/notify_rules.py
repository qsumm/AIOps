"""通知规则 / 订阅 / 业务组 管理（FR-423；D154）。

接口一览：
- GET/POST/PUT/DELETE  /api/ops/notify-rules        通知规则
- POST                 /api/ops/notify-rules/{id}/test  用**模拟事件**试投（不真发）
- GET/POST/PUT/DELETE  /api/ops/alert-subscribes    订阅规则
- GET/POST/PUT/DELETE  /api/ops/busi-groups         业务组
- GET/POST/DELETE      /api/ops/busi-groups/{id}/members  业务组成员

老路径（`AlertRule.channels_by_level`）**完全保留**，两套并存且互斥。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.core.security import audit, require
from app.db.session import get_db
from app.models.alert import NotificationChannel
from app.models.notify_rule import AlertSubscribe, BusiGroup, BusiGroupMember, NotifyRule
from app.models.shell import User
from app.services import notify_rules as svc
from app.services.pipeline import PROCESSORS

router = APIRouter(prefix="/api/ops", tags=["ops-notify-rules"])


# ---------------- 入参模型 ----------------
class NotifyConfigIn(BaseModel):
    channel_id: int = 0                       # 通知媒介（对应 notification_channels.id）
    template_id: int = 0                      # 消息模板（预留；当前平台无模板表）
    severities: list[int] = []                # 适用级别（留空 = 一条都不匹配 = 停用该条）
    time_ranges: list[dict] = []              # 适用时段 [{"weekdays":[0..6],"start":"00:00","end":"23:59"}]
    label_keys: list[dict] = []               # 适用标签 [{k,op,v}]
    attributes: list[dict] = []               # 适用属性 [{k,op,v}]
    targets: list[dict] = []                  # 接收对象 [{"type":"team","name":"..."}]
    pipeline_id: int = 0                      # 通知级 Pipeline（挂在单条配置上）


class NotifyRuleIn(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    busi_group_id: int | None = None
    configs: list[NotifyConfigIn] = []
    pipelines: list[dict] = []


class SubscribeIn(BaseModel):
    rule_id: int
    busi_group_id: int | None = None
    name: str = ""
    enabled: bool = True
    severity_filter: dict = {}
    config: dict = {}


class BusiGroupIn(BaseModel):
    name: str
    description: str = ""


class BusiMemberIn(BaseModel):
    team: str
    permission: str = "read"


# ---------------- 校验 ----------------
def _validate_rule(body: NotifyRuleIn) -> None:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "规则名称为必填项（1~128 字符）")
    if len(name) > 128:
        raise HTTPException(400, "规则名称过长（≤128 字符）")
    if not body.configs:
        raise HTTPException(400, "至少要有一条通知配置（否则这条规则永远不会发任何东西）")
    for cfg in body.configs:
        if not cfg.channel_id:
            raise HTTPException(400, "每条通知配置都要选一个通知渠道（媒介）")
        if cfg.severities and any(s not in (1, 2, 3) for s in cfg.severities):
            raise HTTPException(400, "适用级别只允许 1（紧急）/ 2（警告）/ 3（通知）")
        for tr in cfg.time_ranges or []:
            for k in ("start", "end"):
                v = str(tr.get(k) or "")
                if v and not _hhmm_ok(v):
                    raise HTTPException(400, f"适用时段的 {k} 需为 HH:MM 格式（收到 {v!r}）")
    _validate_pipelines(body.pipelines)


def _hhmm_ok(v: str) -> bool:
    try:
        h, m = v.split(":")[:2]
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, IndexError):
        return False


def _validate_pipelines(pipelines: list[dict] | None) -> None:
    for p in pipelines or []:
        kind = str((p or {}).get("processor") or "")
        if kind not in PROCESSORS:
            raise HTTPException(400, f"未知的 Pipeline 处理器 {kind!r}（可用：{'/'.join(PROCESSORS)}）")


def _out_rule(r) -> dict:
    return {
        "id": r.id, "name": r.name, "description": r.description or "",
        "enabled": r.enabled, "busi_group_id": r.busi_group_id,
        "configs": r.configs or [], "pipelines": r.pipelines or [],
        "created_by": r.created_by or "", "created_at": str(r.created_at or ""),
    }


def _out_sub(s) -> dict:
    return {
        "id": s.id, "rule_id": s.rule_id, "busi_group_id": s.busi_group_id,
        "name": s.name or "", "enabled": s.enabled,
        "severity_filter": s.severity_filter or {}, "config": s.config or {},
        "created_by": s.created_by or "", "created_at": str(s.created_at or ""),
    }


def _out_group(g, members: list | None = None) -> dict:
    return {
        "id": g.id, "name": g.name, "description": g.description or "",
        "created_at": str(g.created_at or ""),
        "members": [{"id": m.id, "team": m.team, "permission": m.permission}
                    for m in (members or [])],
    }


# ---------------- 通知规则 ----------------
@router.get("/notify-rules")
async def list_rules(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(NotifyRule).order_by(NotifyRule.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [_out_rule(r) for r in rows], "total": len(rows)}}


@router.post("/notify-rules")
async def create_rule(body: NotifyRuleIn, request: Request, db=Depends(get_db),
                      user: User = Depends(require("write"))):
    _validate_rule(body)
    name = body.name.strip()
    dup = (await db.execute(select(NotifyRule).where(NotifyRule.name == name))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"规则名称「{name}」已存在，请换一个名字")
    r = NotifyRule(name=name, description=(body.description or "").strip()[:255],
                   enabled=body.enabled, busi_group_id=body.busi_group_id,
                   configs=[c.model_dump() for c in body.configs],
                   pipelines=body.pipelines or [], created_by=user.username)
    db.add(r)
    await db.commit()
    await audit(db=db, request=request, user=user, action="notify_rule_create", resource=f"nrule:{r.id}")
    return {"code": 0, "msg": "ok", "data": {"id": r.id}}


@router.put("/notify-rules/{rid}")
async def update_rule(rid: int, body: NotifyRuleIn, request: Request, db=Depends(get_db),
                      user: User = Depends(require("write"))):
    r = await db.get(NotifyRule, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    _validate_rule(body)
    name = body.name.strip()
    dup = (await db.execute(select(NotifyRule).where(NotifyRule.name == name,
                                                    NotifyRule.id != rid))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"规则名称「{name}」已存在，请换一个名字")
    r.name, r.description = name, (body.description or "").strip()[:255]
    r.enabled, r.busi_group_id = body.enabled, body.busi_group_id
    r.configs = [c.model_dump() for c in body.configs]
    r.pipelines = body.pipelines or []
    r.updated_at = datetime.utcnow()
    await db.commit()
    await audit(db=db, request=request, user=user, action="notify_rule_update", resource=f"nrule:{rid}")
    return {"code": 0, "msg": "ok", "data": {"id": rid}}


@router.delete("/notify-rules/{rid}")
async def delete_rule(rid: int, request: Request, db=Depends(get_db),
                      user: User = Depends(require("write"))):
    r = await db.get(NotifyRule, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    await db.delete(r)
    await db.commit()
    await audit(db=db, request=request, user=user, action="notify_rule_delete", resource=f"nrule:{rid}")
    return {"code": 0, "msg": "ok", "data": {"id": rid}}


@router.post("/notify-rules/{rid}/test")
async def test_rule(rid: int, db=Depends(get_db), _: User = Depends(require("write"))):
    """用**模拟事件**试投：验证筛选条件写得对不对（不真的发通知）。

    与「发真消息」的测试不同，这里只回答"这条规则会不会命中这个事件"。
    """
    r = await db.get(NotifyRule, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    now = datetime.utcnow()
    sample = {
        "severity": 1,
        "tags": {"host": "demo-host", "metric": "cpu.percent", "env": "prod"},
        "attributes": {"rule_id": "0", "rule_name": "模拟规则", "host": "demo-host",
                       "metric": "cpu.percent", "is_recovered": "0"},
    }
    hits = []
    for i, cfg in enumerate(r.configs or []):
        ok = svc.match_config(cfg, sample, now)
        hits.append({"index": i, "matched": ok, "channel_id": cfg.get("channel_id"),
                     "severities": cfg.get("severities") or [],
                     "reason": "" if ok else "不匹配（级别/时段/标签/属性）"})
    return {"code": 0, "msg": "ok", "data": {"rule_id": rid, "sample": sample, "configs": hits}}


# ---------------- 订阅 ----------------
@router.get("/alert-subscribes")
async def list_subs(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(AlertSubscribe).order_by(AlertSubscribe.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [_out_sub(s) for s in rows], "total": len(rows)}}


@router.post("/alert-subscribes")
async def create_sub(body: SubscribeIn, request: Request, db=Depends(get_db),
                     user: User = Depends(require("write"))):
    if body.rule_id <= 0:
        raise HTTPException(400, "要订阅的告警规则 id 必填")
    from app.models.alert import AlertRule
    if await db.get(AlertRule, body.rule_id) is None:
        raise HTTPException(404, f"告警规则 {body.rule_id} 不存在")
    s = AlertSubscribe(rule_id=body.rule_id, busi_group_id=body.busi_group_id,
                       name=(body.name or "").strip()[:128], enabled=body.enabled,
                       severity_filter=body.severity_filter or {}, config=body.config or {},
                       created_by=user.username)
    db.add(s)
    await db.commit()
    await audit(db=db, request=request, user=user, action="alert_subscribe_create", resource=f"sub:{s.id}")
    return {"code": 0, "msg": "ok", "data": {"id": s.id}}


@router.put("/alert-subscribes/{sid}")
async def update_sub(sid: int, body: SubscribeIn, request: Request, db=Depends(get_db),
                     user: User = Depends(require("write"))):
    s = await db.get(AlertSubscribe, sid)
    if s is None:
        raise HTTPException(404, "资源不存在或已被删除")
    s.rule_id, s.busi_group_id = body.rule_id, body.busi_group_id
    s.name, s.enabled = (body.name or "").strip()[:128], body.enabled
    s.severity_filter, s.config = body.severity_filter or {}, body.config or {}
    await db.commit()
    await audit(db=db, request=request, user=user, action="alert_subscribe_update", resource=f"sub:{sid}")
    return {"code": 0, "msg": "ok", "data": {"id": sid}}


@router.delete("/alert-subscribes/{sid}")
async def delete_sub(sid: int, request: Request, db=Depends(get_db),
                     user: User = Depends(require("write"))):
    s = await db.get(AlertSubscribe, sid)
    if s is None:
        raise HTTPException(404, "资源不存在或已被删除")
    await db.delete(s)
    await db.commit()
    await audit(db=db, request=request, user=user, action="alert_subscribe_delete", resource=f"sub:{sid}")
    return {"code": 0, "msg": "ok", "data": {"id": sid}}


# ---------------- 业务组 ----------------
@router.get("/busi-groups")
async def list_groups(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(BusiGroup).order_by(BusiGroup.id))).scalars().all()
    out = []
    for g in rows:
        ms = (await db.execute(select(BusiGroupMember).where(
            BusiGroupMember.busi_group_id == g.id))).scalars().all()
        out.append(_out_group(g, ms))
    return {"code": 0, "msg": "ok", "data": {"items": out, "total": len(out)}}


@router.post("/busi-groups")
async def create_group(body: BusiGroupIn, request: Request, db=Depends(get_db),
                       user: User = Depends(require("write"))):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "业务组名称为必填项（1~128 字符）")
    if len(name) > 128:
        raise HTTPException(400, "业务组名称过长（≤128 字符）")
    dup = (await db.execute(select(BusiGroup).where(BusiGroup.name == name))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"业务组「{name}」已存在")
    g = BusiGroup(name=name, description=(body.description or "").strip()[:255])
    db.add(g)
    await db.commit()
    await audit(db=db, request=request, user=user, action="busi_group_create", resource=f"bgroup:{g.id}")
    return {"code": 0, "msg": "ok", "data": {"id": g.id}}


@router.put("/busi-groups/{gid}")
async def update_group(gid: int, body: BusiGroupIn, request: Request, db=Depends(get_db),
                       user: User = Depends(require("write"))):
    g = await db.get(BusiGroup, gid)
    if g is None:
        raise HTTPException(404, "资源不存在或已被删除")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "业务组名称为必填项")
    dup = (await db.execute(select(BusiGroup).where(BusiGroup.name == name,
                                                    BusiGroup.id != gid))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"业务组「{name}」已存在")
    g.name, g.description = name, (body.description or "").strip()[:255]
    await db.commit()
    await audit(db=db, request=request, user=user, action="busi_group_update", resource=f"bgroup:{gid}")
    return {"code": 0, "msg": "ok", "data": {"id": gid}}


@router.delete("/busi-groups/{gid}")
async def delete_group(gid: int, request: Request, db=Depends(get_db),
                       user: User = Depends(require("write"))):
    g = await db.get(BusiGroup, gid)
    if g is None:
        raise HTTPException(404, "资源不存在或已被删除")
    await db.execute(delete(BusiGroupMember).where(BusiGroupMember.busi_group_id == gid))
    await db.delete(g)
    await db.commit()
    await audit(db=db, request=request, user=user, action="busi_group_delete", resource=f"bgroup:{gid}")
    return {"code": 0, "msg": "ok", "data": {"id": gid}}


@router.post("/busi-groups/{gid}/members")
async def add_member(gid: int, body: BusiMemberIn, request: Request, db=Depends(get_db),
                     user: User = Depends(require("write"))):
    g = await db.get(BusiGroup, gid)
    if g is None:
        raise HTTPException(404, "资源不存在或已被删除")
    team = (body.team or "").strip()
    if not team:
        raise HTTPException(400, "团队名为必填项")
    if body.permission not in ("read", "write"):
        raise HTTPException(400, "权限只允许 read（只读）/ write（读写）")
    m = (await db.execute(select(BusiGroupMember).where(
        BusiGroupMember.busi_group_id == gid, BusiGroupMember.team == team))).scalar_one_or_none()
    if m is None:
        m = BusiGroupMember(busi_group_id=gid, team=team[:64], permission=body.permission)
        db.add(m)
    else:
        m.permission = body.permission
    await db.commit()
    await audit(db=db, request=request, user=user, action="busi_member_add", resource=f"bgroup:{gid}")
    return {"code": 0, "msg": "ok", "data": {"id": m.id}}


@router.delete("/busi-groups/{gid}/members/{mid}")
async def del_member(gid: int, mid: int, request: Request, db=Depends(get_db),
                     user: User = Depends(require("write"))):
    m = await db.get(BusiGroupMember, mid)
    if m is None or m.busi_group_id != gid:
        raise HTTPException(404, "资源不存在或已被删除")
    await db.delete(m)
    await db.commit()
    await audit(db=db, request=request, user=user, action="busi_member_del", resource=f"bgroup:{gid}")
    return {"code": 0, "msg": "ok", "data": {"id": mid}}
