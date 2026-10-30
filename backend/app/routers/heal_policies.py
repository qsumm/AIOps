"""自愈策略 API（D140 用户需求）。

策略 CRUD（admin）+ 执行留痕查询 + manual 档「人工确认执行」+ 即时演练（dry-run）。
所有真实下发都走 services/heal_policy 与人工 heal 同源的护栏路径，本路由不实现任何新动作。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.security import audit, require
from app.db.session import get_db
from app.models.ops import HealPolicy, HealPolicyRun
from app.models.shell import User
from app.services import heal
from app.services import heal_policy as hp

router = APIRouter(prefix="/api/ops", tags=["heal-policies"])

ACTION_IDS = tuple(heal.ACTIONS.keys())


class PolicyIn(BaseModel):
    name: str
    enabled: bool = True
    trigger_rule_id: int | None = None
    trigger_metric: str = ""
    trigger_op: str = ">="          # >= / <=
    trigger_threshold: float = 90
    trigger_severity: int = 1       # 1/2/3
    action: str = "restart_service"
    service_id: int | None = None
    mode: str = "suggest"           # suggest / manual / auto
    cooldown_min: int = 15
    predict_min: int = 0            # D143 预测预警提前量（分钟；0=关闭）


def _policy_out(p: HealPolicy) -> dict:
    return {
        "id": p.id, "name": p.name, "enabled": p.enabled,
        "trigger_rule_id": p.trigger_rule_id, "trigger_metric": p.trigger_metric,
        "trigger_op": p.trigger_op, "trigger_threshold": p.trigger_threshold,
        "trigger_severity": p.trigger_severity,
        "action": p.action, "action_label": heal.ACTIONS[p.action]["label"] if p.action in heal.ACTIONS else p.action,
        "service_id": p.service_id,
        "mode": p.mode, "cooldown_min": p.cooldown_min, "predict_min": p.predict_min,
        "last_fired_at": p.last_fired_at.isoformat(sep=" ") if p.last_fired_at else "",
        "last_status": p.last_status, "last_detail": p.last_detail or {},
    }


def _validate(body: PolicyIn) -> None:
    if not body.name.strip():
        raise HTTPException(400, "策略名称为必填项")
    if len(body.name.strip()) > 128:
        raise HTTPException(400, "策略名称过长（≤128 字符）")
    if body.action not in ACTION_IDS:
        raise HTTPException(400, f"动作不在白名单内（仅允许 {'/'.join(ACTION_IDS)}）")
    if body.mode not in ("suggest", "manual", "auto"):
        raise HTTPException(400, "mode 只允许 suggest / manual / auto")
    if body.trigger_op not in (">=", "<="):
        raise HTTPException(400, "trigger_op 只允许 >= / <=")
    if body.trigger_severity not in (1, 2, 3):
        raise HTTPException(400, "trigger_severity 只允许 1（紧急）/ 2（警告）/ 3（通知）")
    if not body.trigger_rule_id and not body.trigger_metric.strip():
        raise HTTPException(400, "触发条件必须二选一：关联告警规则，或直填指标名（如 cpu.percent）")
    if body.cooldown_min < 1 or body.cooldown_min > 1440:
        raise HTTPException(400, "冷却时间只允许 1~1440 分钟")
    if body.predict_min < 0 or body.predict_min > 240:
        raise HTTPException(400, "预测预警提前量只允许 0~240 分钟（0=关闭）")
    if body.predict_min > 0 and (body.trigger_rule_id or not body.trigger_metric.strip()):
        raise HTTPException(400, "预测预警只支持「直填指标」模式的策略（关联规则的由告警本身触发）")
    if not body.enabled and body.mode == "auto":
        raise HTTPException(400, "停用策略无需保存 auto 档——先启用再改档位")


@router.get("/heal-policies")
async def list_policies(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(HealPolicy).order_by(HealPolicy.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [_policy_out(p) for p in rows]}}


@router.post("/heal-policies")
async def create_policy(body: PolicyIn, request: Request, db=Depends(get_db),
                        user: User = Depends(require("write"))):
    _validate(body)
    p = HealPolicy(**body.model_dump())
    db.add(p)
    await db.commit()
    await audit(db=db, request=request, user=user, action="heal_policy_create",
                resource=f"heal_policy:{p.id}", detail={"action": p.action, "mode": p.mode})
    return {"code": 0, "msg": "ok", "data": {"id": p.id}}


@router.put("/heal-policies/{pid}")
async def update_policy(pid: int, body: PolicyIn, request: Request, db=Depends(get_db),
                        user: User = Depends(require("write"))):
    p = await db.get(HealPolicy, pid)
    if p is None:
        raise HTTPException(404, "策略不存在或已被删除")
    _validate(body)
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    await db.commit()
    await audit(db=db, request=request, user=user, action="heal_policy_update",
                resource=f"heal_policy:{pid}", detail={"action": p.action, "mode": p.mode})
    return {"code": 0, "msg": "ok", "data": {"id": pid}}


@router.delete("/heal-policies/{pid}")
async def delete_policy(pid: int, request: Request, db=Depends(get_db),
                        user: User = Depends(require("write"))):
    p = await db.get(HealPolicy, pid)
    if p is None:
        raise HTTPException(404, "策略不存在或已被删除")
    await db.delete(p)
    await db.commit()
    await audit(db=db, request=request, user=user, action="heal_policy_delete",
                resource=f"heal_policy:{pid}")
    return {"code": 0, "msg": "ok", "data": {"id": pid}}


@router.post("/heal-policies/recommend-threshold")
async def recommend_threshold(body: dict, request: Request, db=Depends(get_db),
                              _: User = Depends(require("read"))):
    """AI 阈值推荐（D142）：基于 metric_agg 历史的分位数 + 趋势推断，附人话依据。

    body: {metric, op?, days?}——按全部主机聚合（策略是全主机触发的）。
    """
    metric = str((body or {}).get("metric") or "").strip()
    if not metric:
        raise HTTPException(400, "metric 为必填（如 cpu.percent / disk.percent）")
    op = str((body or {}).get("op") or ">=")
    if op not in (">=", "<="):
        raise HTTPException(400, "op 只允许 >= / <=")
    days = int((body or {}).get("days") or 7)
    from app.services import heal_policy as hp
    return {"code": 0, "msg": "ok", "data": await hp.recommend_threshold(db, metric=metric, op=op, days=days)}


@router.get("/heal-policy-runs")
async def list_runs(policy_id: int = 0, status: str = "", page: int = Query(1, ge=1),
                    page_size: int = Query(30, ge=1, le=100),
                    db=Depends(get_db), _: User = Depends(require("read"))):
    q = select(HealPolicyRun).order_by(HealPolicyRun.id.desc())
    if policy_id:
        q = q.where(HealPolicyRun.policy_id == policy_id)
    if status:
        q = q.where(HealPolicyRun.status == status)
    total = len((await db.execute(q)).scalars().all())
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {
        "items": [{
            "id": r.id, "policy_id": r.policy_id, "policy_name": r.policy_name,
            "alert_hash": r.alert_hash, "host_ident": r.host_ident,
            "metric": r.metric, "metric_value": r.metric_value,
            "action": r.action, "action_label": heal.ACTIONS[r.action]["label"] if r.action in heal.ACTIONS else r.action,
            "mode": r.mode, "status": r.status, "task_id": r.task_id,
            "guard_rule": r.guard_rule, "guard_reason": r.guard_reason,
            "detail": r.detail or {}, "created_at": r.created_at.isoformat(sep=" "),
        } for r in rows], "total": total}}


@router.post("/heal-policy-runs/{rid}/confirm")
async def confirm_run(rid: int, request: Request, db=Depends(get_db),
                      user: User = Depends(require("write"))):
    """manual 档「人工确认执行」：护栏重新评估后下发（不照旧结论放行）。"""
    try:
        result = await hp.confirm_run(db, run_id=rid, user=user)
    except ValueError as e:
        raise HTTPException(409, str(e))
    await audit(db=db, request=request, user=user, action="heal_dispatch",
                resource=f"heal_policy_run:{rid}",
                detail={"confirmed_by": user.username, **result})
    return {"code": 0, "msg": "ok", "data": result}


@router.post("/heal-policies/{pid}/dry-run")
async def dry_run(pid: int, request: Request, db=Depends(get_db),
                  user: User = Depends(require("write"))):
    """即时演练：立即以当前状态跑一遍护栏评估并留痕（不下发）——上线前先看护栏会不会拦。"""
    p = await db.get(HealPolicy, pid)
    if p is None:
        raise HTTPException(404, "策略不存在或已被删除")
    if p.action not in ACTION_IDS:
        raise HTTPException(400, f"动作不在白名单内：{p.action!r}")

    # 找一个目标：策略指定服务优先，否则任一在线主机上的服务
    svc = None
    if p.service_id:
        svc = await db.get(__import__("app.models.svc", fromlist=["Service"]).Service, p.service_id)
    if svc is None:
        from app.models.svc import Service
        svc = (await db.execute(select(Service).order_by(Service.id).limit(1))).scalar_one_or_none()
    if svc is None:
        raise HTTPException(409, "平台还没有已登记服务——先到「服务监控」登记一个，演练才有护栏对象")

    from app.models.host import Host
    host = await db.get(Host, svc.host_id)
    upstream_ok = await heal.check_upstream(db, host.ident if host else "")
    facts = await heal.collect_facts(db, service_id=svc.id, host_id=svc.host_id,
                                     kill_switch=False, upstream_ok=upstream_ok)
    target, target_err = heal.resolve_target(p.action, svc.spec_json or {})
    verdict = heal.evaluate_guards(
        action=p.action, service=svc, guard=facts["guard"],
        recent_heals=facts["recent_heals"], host_tasks_last_hour=facts["host_tasks_last_hour"],
        upstream_ok=facts["upstream_ok"], active_runbooks=facts["active_runbooks"],
        kill_switch=False, idle_confirmed=(svc.status == "up"),
        dry_run=True, target_error=target_err)
    await audit(db=db, request=request, user=user, action="heal_policy_dry_run",
                resource=f"heal_policy:{pid}", detail={"service": svc.name, "verdict": verdict})
    return {"code": 0, "msg": "ok", "data": {"service": svc.name,
                                             "action_label": heal.ACTIONS[p.action]["label"],
                                             "verdict": verdict}}
