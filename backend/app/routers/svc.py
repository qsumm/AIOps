"""服务级监控 API（P5a，FR-408）。

职责分工（与 `services/svc_monitor.py` 配对）：
  · 本模块只管"看与调"——列服务、大盘汇总、改声明（日志路径/端口/单元）、启停、下发取证；
  · 采集在 Agent（本地跑只读探测），解析与判阈在服务层（平台侧可调，不用动 Agent）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import audit, require
from app.db.session import get_db
from app.models.host import Host
from app.models.shell import User
from app.models.svc import AgentTask, Service, ServiceHeal
from app.services import svc_monitor

router = APIRouter(prefix="/api/ops", tags=["ops-svc"])
settings = get_settings()


@router.get("/services")
async def list_services(host: str = "", status: str = "", db=Depends(get_db),
                        _: User = Depends(require("read"))):
    """服务清单（可按主机 ident / 状态过滤）。前端主机详情与总览都用它。"""
    if host:
        h = (await db.execute(select(Host).where(Host.ident == host))).scalar_one_or_none()
        if h is None:
            raise HTTPException(404, "主机不存在")
        rows = [r for r in await svc_monitor.host_services(db, h.id)]
        if status:
            rows = [r for r in rows if r["status"] == status]
        hosts = {h.id: (h.name or h.ident)}
        items = [{**r, "host": hosts.get(h.id, "")} for r in rows]
        return {"code": 0, "msg": "ok", "data": {"items": items, "total": len(items)}}

    rows = await svc_monitor._live_services(db)      # 过滤孤儿（主机已不存在）
    if status:
        rows = [r for r in rows if r.status == status]
    hosts = {h.id: (h.name or h.ident) for h in (await db.execute(select(Host))).scalars().all()}
    items = [{**svc_monitor._svc_out(s), "host": hosts.get(s.host_id, str(s.host_id))} for s in rows]
    return {"code": 0, "msg": "ok", "data": {"items": items, "total": len(items)}}


@router.get("/services/overview")
async def services_overview(db=Depends(get_db), _: User = Depends(require("read"))):
    """大盘用：按状态计数 + 不健康服务清单（先给"哪个服务不对劲"）。"""
    return {"code": 0, "msg": "ok", "data": await svc_monitor.overview(db)}


@router.get("/services/templates")
async def service_templates(_: User = Depends(require("read"))):
    """可选服务类型与默认值（前端"添加服务"表单用）。"""
    return {"code": 0, "msg": "ok", "data": {"items": [
        {"type": k, **{kk: v[kk] for kk in ("label", "default_port", "default_unit", "default_log", "hint")}}
        for k, v in svc_monitor.TEMPLATES.items()]}}


class ServicePatchIn(BaseModel):
    name: str | None = None
    port: int | None = None
    unit: str | None = None
    container: str | None = None
    log_path: str | None = None
    process: str | None = None
    health_url: str | None = None
    enabled: bool | None = None


@router.patch("/services/{sid}")
async def patch_service(sid: int, body: ServicePatchIn, request: Request, db=Depends(get_db),
                        user: User = Depends(require("write"))):
    """改服务声明（重点是 **日志路径**——取证时按它取；以及端口/单元/进程）。

    为什么需要它：日志路径因环境而异，Agent 侧无法猜准；平台侧改一次即可用于取证，
    不必重新安装 Agent。
    """
    s = await db.get(Service, sid)
    if s is None:
        raise HTTPException(404, "服务不存在")
    spec = dict(s.spec_json or {})
    for k in ("port", "unit", "container", "log_path", "process", "health_url"):
        v = getattr(body, k)
        if v is not None:
            spec[k] = v
    if body.name:
        s.name = body.name.strip()[:64]
    if body.enabled is not None:
        s.enabled = body.enabled
    s.spec_json = spec
    await db.commit()
    await audit(db=db, request=request, user=user, action="service_update", resource=f"service:{sid}",
                detail={k: v for k, v in body.model_dump().items() if v is not None})
    return {"code": 0, "msg": "ok", "data": svc_monitor._svc_out(s)}


class ServiceCreateIn(BaseModel):
    ident: str
    type: str = "app"
    name: str = ""
    port: int | None = None
    unit: str = ""
    container: str = ""
    log_path: str = ""
    process: str = ""
    health_url: str = ""


@router.post("/services")
async def create_service(body: ServiceCreateIn, request: Request, db=Depends(get_db),
                         user: User = Depends(require("write"))):
    """手工登记一个服务（自研应用无法自动发现，需显式声明）。"""
    h = (await db.execute(select(Host).where(Host.ident == body.ident))).scalar_one_or_none()
    if h is None:
        raise HTTPException(404, "主机不存在")
    stype = body.type if body.type in svc_monitor.TEMPLATES else "app"
    name = (body.name or svc_monitor.TEMPLATES[stype]["label"])[:64]
    dup = (await db.execute(select(Service).where(
        Service.host_id == h.id, Service.name == name))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"该主机已有同名服务「{name}」")
    spec = svc_monitor.build_spec(stype, name, port=body.port, unit=body.unit,
                                  container=body.container, log_path=body.log_path,
                                  process=body.process, health_url=body.health_url)
    s = Service(host_id=h.id, name=name, type=stype, spec_json=spec, status="unknown",
                note="等待 Agent 下次探测（约 1 分钟内）")
    db.add(s)
    await db.commit()
    await db.refresh(s)
    await audit(db=db, request=request, user=user, action="service_create",
                resource=f"service:{s.id}", detail={"host": body.ident, "type": stype})
    return {"code": 0, "msg": "ok", "data": svc_monitor._svc_out(s)}


@router.delete("/services/{sid}")
async def delete_service(sid: int, request: Request, db=Depends(get_db),
                         user: User = Depends(require("write"))):
    s = await db.get(Service, sid)
    if s is None:
        raise HTTPException(404, "服务不存在")
    await db.delete(s)
    await db.commit()
    await audit(db=db, request=request, user=user, action="service_delete", resource=f"service:{sid}")
    return {"code": 0, "msg": "ok", "data": {"id": sid}}


# ── 日志取证（按服务一键下发，窗口来自故障事件或显式给定）────────────
class SvcLogFetchIn(BaseModel):
    since: str = ""          # ISO8601；留空则用 [now-window_min, now+2min]
    until: str = ""
    window_min: int = 5      # 未给 since 时：向前取多少分钟（默认 5，见方案 §3.1 前多后少）
    pattern: str = ""
    context: int = 3
    origin: str = ""         # 如 incident:12 —— 用于报告标注来源轮次
    anchor: str = ""         # 告警首次触发时刻（优先于 now 作为锚点）


@router.post("/services/{sid}/log-fetch")
async def service_log_fetch(sid: int, body: SvcLogFetchIn, request: Request, db=Depends(get_db),
                            user: User = Depends(require("diag"))):
    """按服务下发一次日志取证（FR-409）。

    **时间窗以"告警首次触发时刻"为锚**（不是 now）——故障常发生在触发之前，
    用 now 取尾部会整段错过成因（方案 §3.1 原则一）。未给锚点时回落到 now。
    """
    from app.routers.tasks import enqueue

    s = await db.get(Service, sid)
    if s is None:
        raise HTTPException(404, "服务不存在")
    host = await db.get(Host, s.host_id)
    if host is None:
        raise HTTPException(404, "主机不存在")
    spec = s.spec_json or {}
    path, unit = str(spec.get("log_path") or ""), str(spec.get("unit") or "")
    # P2 修补：主机离线时 Agent 收不到任务——下发前就短路，不让用户对着
    # "等待 Agent 执行…（已等 40s）"干等 60 秒（用户实测报障）。
    beat_age = (datetime.utcnow() - host.last_beat).total_seconds() if host.last_beat else None
    if host.status == "down" or beat_age is None or beat_age > 45:
        raise HTTPException(409, f"主机当前离线（最近心跳 {int(beat_age) if beat_age is not None else '未知'} 秒前）"
                                 "——Agent 收不到取证任务，任务未下发；请先恢复主机/Agent 再试")
    if path:
        from app.routers.tasks import _check_log_path_shape

        _check_log_path_shape(path)   # 声明里的路径也要过形状校验（服务声明是用户可填字段）
    discover_block: dict | None = None
    if not path and not unit:
        # P2：未声明日志来源 → 让 Agent 按进程自动发现（需 Agent ≥0.3.2）。
        ver = str(getattr(host, "agent_version", "") or "")
        try:
            vmajor, vminor, vpatch = (int(x) for x in ver.split(".")[:3])
        except Exception:  # noqa: BLE001
            vmajor, vminor, vpatch = 0, 0, 0
        if (vmajor, vminor, vpatch) < (0, 3, 2):
            raise HTTPException(400, f"该服务未声明日志路径，且目标机 Agent 版本（{ver or '未知'}）"
                                     "不支持自动发现（需 ≥0.3.2，重跑安装命令升级）——"
                                     "或在服务详情里填写 log_path")
        if not any([spec.get("process"), spec.get("port"), unit]):
            raise HTTPException(400, "该服务未声明日志路径，声明里也没有 process/port/unit，"
                                     "Agent 无从定位进程——请在「改声明」里补一项")
        discover_block = {"service": s.name, "process": str(spec.get("process") or ""),
                          "port": spec.get("port") or 0, "unit": unit}

    anchor_dt = _parse_dt(body.anchor) or _parse_dt(body.since) or datetime.utcnow()
    since = _parse_dt(body.since) or (anchor_dt - timedelta(minutes=max(1, min(body.window_min, 120))))
    until = _parse_dt(body.until) or (anchor_dt + timedelta(minutes=2))

    payload = {"path": path, "unit": unit, "since": since.isoformat(), "until": until.isoformat(),
               "pattern": body.pattern, "level": "auto", "context": body.context,
               "service_type": s.type}
    if discover_block is not None:
        payload["discover"] = discover_block
    t = await enqueue(db, host, "log_fetch", payload, origin=body.origin or f"service:{sid}",
                      created_by=user.username)
    await audit(db=db, request=request, user=user, action="log_fetch_dispatch",
                resource=f"service:{sid}",
                detail={"task": t.id, "host": host.ident, "path": path, "unit": unit,
                        "discover": bool(discover_block),
                        "window": [since.isoformat(), until.isoformat()], "origin": body.origin})
    return {"code": 0, "msg": "ok", "data": {
        "task_id": t.id, "window": {"since": since.isoformat(), "until": until.isoformat()},
        "discover": bool(discover_block),
        "note": ("已下发（自动发现模式：Agent 定位进程打开的日志文件，"
                 "发现到的路径会回写为该服务的建议日志路径）" if discover_block
                else "已下发；Agent 约 15 秒内领取执行。轮询 GET /api/ops/tasks/{id} 取结果。")}}


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00").replace("+00:00", ""))
    except Exception:  # noqa: BLE001
        return None


# ── 自愈状态（P5c 前端用）────────────────────────────────────────
@router.get("/services/{sid}/heal-state")
async def heal_state(sid: int, days: int = Query(7, ge=1, le=30), db=Depends(get_db),
                     _: User = Depends(require("read"))):
    """某服务的自愈计数与熔断状态（FR-411：频繁自愈 = 缺陷信号，要看得见）。"""
    from app.models.svc import HealGuard

    since = datetime.utcnow() - timedelta(days=days)
    rows = (await db.execute(select(ServiceHeal).where(
        ServiceHeal.service_id == sid, ServiceHeal.created_at >= since)
        .order_by(ServiceHeal.id.desc()))).scalars().all()
    g = (await db.execute(select(HealGuard).where(HealGuard.service_id == sid))).scalar_one_or_none()
    ok_n = sum(1 for r in rows if r.ok)
    return {"code": 0, "msg": "ok", "data": {
        "days": days, "total": len(rows), "succeeded": ok_n, "failed": len(rows) - ok_n,
        "tripped": bool(g.tripped) if g else False,
        "tripped_reason": (g.tripped_reason if g else ""),
        "fail_streak": int(g.fail_streak) if g else 0,
        "frequent": len(rows) >= 3,        # ≥3 次/7 天 → 前端醒目标注"请人工修根因"
        "items": [{"action": r.action, "ok": r.ok, "detail": r.detail,
                   "at": str(r.created_at)} for r in rows[:20]],
    }}


@router.post("/services/{sid}/heal-reset")
async def heal_reset(sid: int, request: Request, db=Depends(get_db),
                     user: User = Depends(require("write"))):
    """人工重置熔断（确认为环境问题、修好之后允许恢复自动执行）。"""
    from app.models.svc import HealGuard

    g = (await db.execute(select(HealGuard).where(HealGuard.service_id == sid))).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "该服务暂无自愈记录")
    g.tripped, g.fail_streak, g.tripped_reason = False, 0, ""
    await db.commit()
    await audit(db=db, request=request, user=user, action="heal_reset", resource=f"service:{sid}")
    return {"code": 0, "msg": "ok", "data": {"id": sid, "tripped": False}}

# ── P5c：分级恢复（FR-411）────────────────────────────────────────
class HealIn(BaseModel):
    action: str
    mode: str = "suggest"        # suggest | manual | auto
    confirmed: bool = False      # manual 档需显式确认
    dry_run: bool = False        # 演练：只跑护栏判决，不下发


@router.post("/services/{sid}/heal")
async def heal_service(sid: int, body: HealIn, request: Request, db=Depends(get_db),
                       user: User = Depends(require("diag"))):
    """对服务发起一次恢复动作（**护栏准入后才下发**）。

    三档语义（沿用现有 execution_mode 口径）：
      · suggest（默认）：只跑护栏并给出建议，**不下发**
      · manual：需 `confirmed=true`（人工确认）才下发
      · auto：护栏全通过即下发（仍受限流/熔断/依赖/发布窗口约束）
    """
    from app.services import heal

    s = await db.get(Service, sid)
    if s is None:
        raise HTTPException(404, "服务不存在")
    host = await db.get(Host, s.host_id)
    if host is None:
        raise HTTPException(404, "服务所属主机不存在")

    if body.action not in heal.ACTIONS:
        raise HTTPException(400, f"动作不在白名单内：{body.action!r}（仅允许 {'/'.join(heal.ACTIONS)}）")
    if body.mode not in ("suggest", "manual", "auto"):
        raise HTTPException(400, "mode 只允许 suggest / manual / auto")

    upstream_ok = await heal.check_upstream(db, host.ident)
    facts = await heal.collect_facts(db, service_id=sid, host_id=host.id,
                                    kill_switch=getattr(settings, "heal_kill_switch", False),
                                    upstream_ok=upstream_ok)
    # 目标对象来自服务声明（unit / container），不回落显示名——见 heal.resolve_target
    target, target_err = heal.resolve_target(body.action, s.spec_json or {})
    verdict = heal.evaluate_guards(
        action=body.action, service=s, guard=facts["guard"],
        recent_heals=facts["recent_heals"], host_tasks_last_hour=facts["host_tasks_last_hour"],
        upstream_ok=facts["upstream_ok"], active_runbooks=facts["active_runbooks"],
        kill_switch=facts["kill_switch"], idle_confirmed=(s.status == "up"),
        dry_run=body.dry_run or body.mode == "suggest", target_error=target_err)

    if body.dry_run or body.mode == "suggest":
        return {"code": 0, "msg": "ok", "data": {
            "dispatched": False, "mode": body.mode, **verdict,
            "action_label": heal.ACTIONS[body.action]["label"],
            "note": ("仅建议：未下发执行" if body.mode == "suggest" else "演练：未下发执行")}}

    if not verdict["allow"]:
        await audit(db=db, request=request, user=user, action="heal_denied",
                    resource=f"service:{sid}", detail={"action": body.action, "rule": verdict["rule"],
                                                       "reason": verdict["reason"]})
        raise HTTPException(409, f"护栏拒绝（{verdict['rule']}）：{verdict['reason']}")

    if body.mode == "manual" and not body.confirmed:
        raise HTTPException(400, "「人工确认」档需显式确认（confirmed=true）后才执行")

    from app.routers.tasks import enqueue

    spec = s.spec_json or {}
    payload = {"action": body.action, "service": target,
               "service_id": sid, "mode": heal.ACTIONS[body.action]["mode"]}
    # 后置校验所需信息一并带上（Agent 侧执行后立刻校验：进程/端口/健康端点）
    payload["_post"] = {"mode": heal.ACTIONS[body.action]["mode"], "port": spec.get("port") or 0}
    t = await enqueue(db, host, "svc_action", payload, origin=f"heal:{sid}", created_by=user.username)
    await audit(db=db, request=request, user=user, action="heal_dispatch", resource=f"service:{sid}",
                detail={"task": t.id, "action": body.action, "mode": body.mode,
                        "approved_by": user.username, "warnings": verdict["warnings"]})
    return {"code": 0, "msg": "ok", "data": {
        "dispatched": True, "task_id": t.id, "mode": body.mode,
        "action_label": heal.ACTIONS[body.action]["label"],
        "post_check": heal.ACTIONS[body.action]["post"], "warnings": verdict["warnings"],
        "note": "已下发；Agent 执行后会做后置校验，未通过将计入熔断（连续 2 次即停并转人工）"}}


@router.get("/services/{sid}/heal/preview")
async def heal_preview(sid: int, action: str = "", db=Depends(get_db),
                       _: User = Depends(require("read"))):
    """**只跑护栏判决、不下发**（dry-run）。

    刻意做成 GET（无副作用）：① 语义正确；② 前端可以随时预览而不必加防重复提交守卫；
    ③ 演示时可以放心点它给客户看"平台会先判能不能动"。
    """
    from app.services import heal

    s = await db.get(Service, sid)
    if s is None:
        raise HTTPException(404, "服务不存在")
    if not action:
        raise HTTPException(400, "缺少 action 参数")
    host = await db.get(Host, s.host_id)
    if host is None:
        raise HTTPException(404, "服务所属主机不存在")
    upstream_ok = await heal.check_upstream(db, host.ident)
    facts = await heal.collect_facts(db, service_id=sid, host_id=host.id,
                                    kill_switch=bool(getattr(settings, "heal_kill_switch", False)),
                                    upstream_ok=upstream_ok)
    _target, _terr = heal.resolve_target(action, s.spec_json or {})
    verdict = heal.evaluate_guards(
        action=action, service=s, guard=facts["guard"], recent_heals=facts["recent_heals"],
        host_tasks_last_hour=facts["host_tasks_last_hour"], upstream_ok=facts["upstream_ok"],
        active_runbooks=facts["active_runbooks"], kill_switch=facts["kill_switch"],
        idle_confirmed=(s.status == "up"), dry_run=True, target_error=_terr)
    return {"code": 0, "msg": "ok", "data": {**verdict,
        "action_known": action in heal.ACTIONS,
        "target": _target,
        "requirement": (heal.ACTIONS.get(action) or {}).get("requirement", ""),
        "action_label": (heal.ACTIONS.get(action) or {}).get("label", action)}}


@router.get("/heal/actions")
async def heal_actions(_: User = Depends(require("read"))):
    """可用恢复动作清单（前端下拉用；也是"能力边界"的自解释）。"""
    from app.services import heal

    return {"code": 0, "msg": "ok", "data": {"items": [
        {"action": k, **{x: v[x] for x in ("label", "pre", "post", "requirement")}}
        for k, v in heal.ACTIONS.items()],
        "note": "「清理临时文件」等涉及删除的动作刻意不在首版清单（风险收益比待数据评估）",
        "limits": {"per_host_per_hour": heal.HEAL_PER_HOUR,
                   "fail_streak_trip": heal.FAIL_STREAK_TRIP,
                   "frequent_window_days": heal.FREQUENT_WINDOW_DAYS,
                   "frequent_trip": heal.FREQUENT_TRIP}}}


@router.post("/heal/kill-switch")
async def heal_kill_switch(request: Request, body: dict, db=Depends(get_db),
                           user: User = Depends(require("write"))):
    """全局自愈熔断开关（大故障期间一键停所有自动恢复）。"""
    on = bool((body or {}).get("enabled"))
    settings.heal_kill_switch = on
    await audit(db=db, request=request, user=user, action="heal_kill_switch",
                resource="global", detail={"enabled": on})
    return {"code": 0, "msg": "ok", "data": {"kill_switch": on,
      "note": "已开启全局熔断：所有自愈下发将被拒绝" if on else "已解除全局熔断"}}


@router.get("/heal/kill-switch")
async def heal_kill_switch_state(_: User = Depends(require("read"))):
    return {"code": 0, "msg": "ok", "data": {"kill_switch": bool(getattr(settings, "heal_kill_switch", False))}}
