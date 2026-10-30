"""巡检 / 变更单 / 技能 / 案例卡路由（FR-D5/E1/E2/E3；05 §5）。"""
import asyncio
import json
from datetime import datetime

from fastapi import HTTPException, APIRouter, Depends, Request, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.security import audit, require
from app.db.session import SessionLocal, get_db
from app.models.ops import ActionAudit, Inspection, Runbook, RunbookVersion, Skill
from app.models.shell import User
from app.services import cases as cases_svc
from app.services import inspect as inspect_svc
from app.services import runbook as runbook_svc
from app.services import skills as skills_svc

router = APIRouter(prefix="/api/ops", tags=["ops-runbook"])


# ---------------- 巡检 ----------------
class InspectIn(BaseModel):
    name: str = "即时巡检"
    cron_expr: str = "0 9 * * *"
    target_scope: dict = {}
    enabled: bool = True


@router.get("/inspections")
async def list_inspections(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                          db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(Inspection).order_by(Inspection.id.desc()))).scalars().all()
    total = len(rows)                       # D128
    rows = rows[(page - 1) * page_size: page * page_size]
    return {"code": 0, "msg": "ok", "data": {"items": [
        {"id": r.id, "name": r.name, "cron_expr": r.cron_expr, "enabled": r.enabled,
         "last_run_at": str(r.last_run_at or ""), "last_status": r.last_status,
         "anomalies": len(r.findings or []), "incidents_created": r.incidents_created,
         "has_report": bool(r.report_md)} for r in rows], "total": total}}


def _validate_cron(expr: str) -> None:
    """cron 合法性校验（D39 随编辑接口补）：非法表达式直接 400，别等调度器静默跳过。"""
    e = (expr or "").strip()
    if not e:
        raise HTTPException(400, "cron 表达式不能为空（示例 '0 1 * * *'）")
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(e)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"cron 表达式不合法：{exc}（示例 '0 1 * * *'，按 UTC 解释）")


@router.post("/inspections")
async def create_inspection(body: InspectIn, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    _validate_cron(body.cron_expr)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "任务名称为必填项（1~128 字符）")
    if len(name) > 128:
        raise HTTPException(400, "任务名称过长（≤128 字符）")
    r = Inspection(name=name, cron_expr=body.cron_expr, target_scope=body.target_scope, enabled=body.enabled)
    db.add(r)
    await db.commit()
    await db.refresh(r)
    await audit(db=db, request=request, user=user, action="inspection_create", resource=f"inspection:{r.id}")
    return {"code": 0, "msg": "ok", "data": {"id": r.id}}


@router.put("/inspections/{iid}")
async def update_inspection(iid: int, body: InspectIn, request: Request,
                            db=Depends(get_db), user: User = Depends(require("write"))):
    """编辑巡检任务（D39）。

    缺口背景：手册 §8 写「cron 表达式改了会自动生效（对账式注册）」，但此前**既无编辑
    接口也无编辑入口**——用户根本改不了 cron（实测：想把它改成本地 9 点无处可改）。
    调度器每分钟对账一次，改完自动生效。
    """
    r = await db.get(Inspection, iid)
    if r is None:
        raise HTTPException(404, "巡检任务不存在或已被删除")
    _validate_cron(body.cron_expr)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "任务名称为必填项（1~128 字符）")
    if len(name) > 128:
        raise HTTPException(400, "任务名称过长（≤128 字符）")
    r.name = name
    r.cron_expr = body.cron_expr
    r.target_scope = body.target_scope
    r.enabled = body.enabled
    await db.commit()
    await audit(db=db, request=request, user=user, action="inspection_update", resource=f"inspection:{iid}")
    return {"code": 0, "msg": "ok", "data": {"id": iid}}


@router.delete("/inspections/{iid}")
async def delete_inspection(iid: int, request: Request,
                            db=Depends(get_db), user: User = Depends(require("write"))):
    """删除巡检任务（D39）。调度器对账时会把对应 job 一并摘除。"""
    r = await db.get(Inspection, iid)
    if r is None:
        raise HTTPException(404, "巡检任务不存在或已被删除")
    await db.delete(r)
    await db.commit()
    await audit(db=db, request=request, user=user, action="inspection_delete", resource=f"inspection:{iid}")
    return {"code": 0, "msg": "ok", "data": {"id": iid}}


# 巡检整体 deadline（D17 对抗测试补）：巡检是同步长任务（主机 × 8 项检查 + LLM 润色），
# 它已被豁免 25s DB 挂起兜底（否则会被误杀，见 main.py），因此必须自带超时保护——
# 否则 LLM 或 DB 挂住时这个请求会无限期占用。
INSPECT_DEADLINE_S = 600


@router.post("/inspections/{iid}/run")
async def run_inspection(iid: int, request: Request = None, user: User = Depends(require("diag"))):
    """手动触发一次巡检（同步执行；异常项自动收敛进问题看板）。"""
    # D19 穷举矩阵实测补：原先不校验 id 是否存在——传错 id 时 `job` 为 None、
    # scope 被当空 → **静默跑一次全量巡检**（~40s + LLM 额度 + 9 条事件），
    # 用户却以为在跑"那个任务"。改为 404 明确报错。
    async with SessionLocal() as db:
        if await db.get(Inspection, iid) is None:
            raise HTTPException(404, "巡检任务不存在或已被删除")
    try:
        r = await asyncio.wait_for(inspect_svc.run_inspection(iid, quota_role=user.role),
                                  timeout=INSPECT_DEADLINE_S)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"巡检超时（>{INSPECT_DEADLINE_S}s）：可能是主机数量多或 LLM 响应慢，"
                                 f"请稍后在「巡检报告」查看本轮是否有部分结果")
    return {"code": 0, "msg": "ok", "data": r}


@router.post("/inspections/run-once")
async def run_inspection_once(request: Request = None, user: User = Depends(require("diag"))):
    """即时巡检（不落任务表）。"""
    try:
        r = await asyncio.wait_for(inspect_svc.run_inspection(None, quota_role=user.role),
                                  timeout=INSPECT_DEADLINE_S)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"巡检超时（>{INSPECT_DEADLINE_S}s）：可能是主机数量多或 LLM 响应慢")
    return {"code": 0, "msg": "ok", "data": r}


@router.get("/inspections/{iid}")
async def inspection_detail(iid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    r = await db.get(Inspection, iid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    return {"code": 0, "msg": "ok", "data": {
        "id": r.id, "name": r.name, "cron_expr": r.cron_expr, "enabled": r.enabled,
        "last_run_at": str(r.last_run_at or ""), "last_status": r.last_status,
        "report_md": r.report_md, "findings": r.findings, "incidents_created": r.incidents_created}}


# ---------------- 变更单 ----------------
class RunbookIn(BaseModel):
    name: str
    host_ident: str = ""
    incident_id: int | None = None
    risk_level: str = "mid"
    execution_mode: str = "manual"          # suggest|manual|auto（FR-E2 三选）
    risk_assessment: str = ""
    pre_checks: list = []
    steps: list = []                        # [{idx, desc, cmd|check, rollback, mode}]
    rollback_plan: str = ""
    verification: str = ""


@router.post("/runbooks")
async def create_runbook(body: RunbookIn, request: Request, db=Depends(get_db),
                         user: User = Depends(require("write"))):
    """手动建变更单（FR-E1「手动创建或从诊断建议一键生成」的后半句）。

    诊断建议那条路走 `POST /runbooks/generate/{incident_id}`；本接口供"运维自己发现的问题"
    或"预制变更模板"使用。步骤里 `mode` 决定是否需审批（readonly 免确认 / approval 需确认）。
    """
    from app.models.ops import Runbook, RunbookVersion

    if body.risk_level not in ("low", "mid", "high"):
        raise HTTPException(422, "risk_level 只能是 low/mid/high")
    if body.execution_mode not in ("suggest", "manual", "auto"):
        raise HTTPException(422, "execution_mode 只能是 suggest/manual/auto")
    if not (body.name or "").strip():
        raise HTTPException(422, "name 不能为空")
    steps = []
    for i, st in enumerate(body.steps or [], 1):
        if not isinstance(st, dict):
            raise HTTPException(422, "steps 每项必须是对象 {desc, cmd|check, rollback, mode}")
        s = dict(st)
        s["idx"] = int(s.get("idx") or i)
        s.setdefault("mode", "approval")
        s.setdefault("desc", "")
        steps.append(s)
    rb = Runbook(name=body.name.strip()[:128], host_ident=body.host_ident, incident_id=body.incident_id,
                 risk_level=body.risk_level, execution_mode=body.execution_mode,
                 whitelist_enabled=(body.execution_mode == "auto"),
                 risk_assessment=body.risk_assessment, pre_checks=body.pre_checks or [],
                 steps=steps, rollback_plan=body.rollback_plan, verification=body.verification,
                 version=1, status="draft")
    db.add(rb)
    await db.commit()
    await db.refresh(rb)
    db.add(RunbookVersion(runbook_id=rb.id, version=1, summary="手动创建", snapshot_json={}))
    await db.commit()
    await audit(db=db, request=request, user=user, action="runbook_create", resource=f"runbook:{rb.id}")
    return {"code": 0, "msg": "ok", "data": {"id": rb.id, "name": rb.name, "steps": len(steps)}}


@router.get("/runbooks")
async def list_runbooks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                       db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(Runbook).order_by(Runbook.id.desc()))).scalars().all()
    total = len(rows)                       # D128
    rows = rows[(page - 1) * page_size: page * page_size]
    return {"code": 0, "msg": "ok", "data": {"items": [
        {"id": r.id, "name": r.name, "incident_id": r.incident_id, "host_ident": r.host_ident,
         "risk_level": r.risk_level, "version": r.version, "status": r.status,
         "steps": len(r.steps or []), "whitelist_enabled": r.whitelist_enabled,
         "execution_mode": getattr(r, "execution_mode", "manual"),
         "created_at": str(r.created_at)} for r in rows], "total": total}}


@router.post("/runbooks/generate/{incident_id}")
async def generate_runbook(incident_id: int, request: Request = None, user: User = Depends(require("diag"))):
    """由事件诊断结论生成变更单（无 VALIDATED FIX 时拒绝生成）。"""
    r = await runbook_svc.generate_runbook(incident_id)
    return {"code": 0 if r.get("ok") else 400, "msg": "ok" if r.get("ok") else r.get("msg"), "data": r}


@router.get("/runbooks/{rid}")
async def runbook_detail(rid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    r = await db.get(Runbook, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    versions = (await db.execute(select(RunbookVersion).where(RunbookVersion.runbook_id == rid)
                                 .order_by(RunbookVersion.version))).scalars().all()
    audits = (await db.execute(select(ActionAudit).where(ActionAudit.runbook_id == rid)
                               .order_by(ActionAudit.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {
        "id": r.id, "name": r.name, "incident_id": r.incident_id, "host_ident": r.host_ident,
        "risk_level": r.risk_level, "risk_assessment": r.risk_assessment, "pre_checks": r.pre_checks,
        "steps": r.steps, "rollback_plan": r.rollback_plan, "verification": r.verification,
        "version": r.version, "status": r.status, "whitelist_enabled": r.whitelist_enabled,
        "execution_mode": getattr(r, "execution_mode", "manual"),
        "created_at": str(r.created_at), "updated_at": str(r.updated_at),
        "versions": [{"version": v.version, "summary": v.summary, "created_at": str(v.created_at)} for v in versions],
        "audits": [{"id": a.id, "action": a.action, "status": a.status, "operator": a.operator,
                    "guardrail": a.guardrail_verdict, "result": a.result[:300],
                    "created_at": str(a.created_at)} for a in audits]}}


class RunbookPatchIn(BaseModel):
    name: str | None = None
    steps: list | None = None
    pre_checks: list | None = None
    rollback_plan: str | None = None
    verification: str | None = None
    risk_level: str | None = None
    risk_assessment: str | None = None
    whitelist_enabled: bool | None = None   # legacy：等价 execution_mode=="auto"
    execution_mode: str | None = None        # suggest|manual|auto（FR-E2 的"三选"）
    status: str | None = None
    summary: str = ""


def _svc(r: dict, resource: str = "处置单"):
    """把服务层 {ok,msg} 结果翻成 HTTP 语义（测试阶段统一，见 D8 报告）。

    原先失败一律 HTTP 200 + body 里的 code，HTTP 状态码与业务结果脱节：
    不存在的处置单返回 200，前端/网关/监控都看不出异常。现在：
      NOT_FOUND                           → 404
      GUARDRAIL_BLOCKED* / HITL_REQUIRED* → 403（护栏拦下，前端 toast）
      其余（NO_FINDINGS/STEP_NOT_FOUND/…） → 400
    """
    if r.get("ok"):
        return {"code": 0, "msg": "ok", "data": r}
    msg = str(r.get("msg") or "failed")
    if msg in ("NOT_FOUND", "INCIDENT_NOT_FOUND"):
        raise HTTPException(404, f"{resource}不存在或已删除")
    if msg.startswith(("GUARDRAIL_BLOCKED", "HITL_REQUIRED")):
        raise HTTPException(403, msg)
    raise HTTPException(400, msg)


@router.put("/runbooks/{rid}")
async def update_runbook(rid: int, body: RunbookPatchIn, request: Request = None,
                         db=Depends(get_db), user: User = Depends(require("write"))):
    patch = body.model_dump(exclude={"summary"})
    if body.execution_mode:
        if body.execution_mode not in ("suggest", "manual", "auto"):
            raise HTTPException(422, "execution_mode 只能是 suggest/manual/auto")
        patch["whitelist_enabled"] = body.execution_mode == "auto"   # legacy 列同步（老客户端兼容）
    elif body.whitelist_enabled is not None:
        patch["execution_mode"] = "auto" if body.whitelist_enabled else "manual"
    r = await runbook_svc.update_runbook(rid, patch, body.summary)
    if not r.get("ok"):
        return _svc(r)
    await audit(db=db, request=request, user=user, action="runbook_update", resource=f"runbook:{rid}")
    return _svc(r)


class ExecIn(BaseModel):
    step_idx: int
    approved: bool = False        # 保留兼容；**不再作为放行依据**（客户端自报不可信）
    confirm_token: str = ""       # 人工确认模式必须出示一次性凭据


class ConfirmIn(BaseModel):
    pass


@router.post("/runbooks/{rid}/steps/{step_idx}/confirm")
async def confirm_step(rid: int, step_idx: int, request: Request = None, db=Depends(get_db),
                       user: User = Depends(require("write"))):
    """为执行某一步签发一次性确认凭据（FR-E2 HITL）。

    前端应先弹"确认卡"展示动作/命令/影响/回滚，用户点确认后再调本接口拿凭据，
    最后带着凭据调 execute。审计里因此能查到"谁在何时批准了哪一步"。
    """
    from app.services import runbook as runbook_svc

    res = await runbook_svc.issue_confirm(rid, step_idx, operator=user.username,
                                          ip=(request.client.host if request and request.client else ""))
    if not res.get("ok"):
        await audit(db=db, request=request, user=user, action="runbook_confirm_denied",
                    resource=f"runbook:{rid}", detail={"step": step_idx, "msg": res.get("msg")})
        return _svc(res, "处置单步骤")
    await audit(db=db, request=request, user=user, action="runbook_confirm",
                resource=f"runbook:{rid}", detail={"step": step_idx, "allowlisted": res.get("allowlisted")})
    return {"code": 0, "msg": "ok", "data": res}


@router.get("/action-allowlist")
async def list_action_allowlist(_: User = Depends(require("read"))):
    """可自动执行的动作清单（FR-E2「脚本级白名单」）：只有命中它的动作在 auto 模式下免确认。"""
    from app.services import runbook as runbook_svc

    return {"code": 0, "msg": "ok", "data": {"items": runbook_svc.action_allowlist(),
                                             "env_override": bool(__import__("os").getenv("ACTION_ALLOWLIST"))}}


@router.post("/runbooks/{rid}/execute")
async def execute_runbook_step(rid: int, body: ExecIn, request: Request = None,
                               db=Depends(get_db), user: User = Depends(require("write"))):
    """执行单步（护栏分层：denylist → 只读放行 → 白名单 → HITL）。"""
    r = await runbook_svc.execute_step(rid, body.step_idx, operator="user", approved=body.approved,
                                       confirm_token=body.confirm_token, operator_name=user.username)
    await audit(db=db, request=request, user=user, action="runbook_execute", resource=f"runbook:{rid}",
                detail={"step": body.step_idx, "ok": r.get("ok"),
                        "approval_mode": r.get("approval_mode", ""), "guardrail": r.get("guardrail", "")})
    return _svc(r)


@router.get("/action-audit")
async def list_action_audit(runbook_id: int = 0, host_ident: str = "", operator: str = "",
                            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                            db=Depends(get_db), _: User = Depends(require("read"))):
    """全局处置动作审计（FR §5 `GET /ops/action-audit`）：跨变更单查"谁在什么时候执行了什么、护栏怎么判的"。"""
    q = select(ActionAudit).order_by(ActionAudit.id.desc())
    qc = select(func.count()).select_from(ActionAudit)
    if runbook_id:
        q, qc = q.where(ActionAudit.runbook_id == runbook_id), qc.where(ActionAudit.runbook_id == runbook_id)
    if host_ident:
        q, qc = q.where(ActionAudit.host_ident == host_ident), qc.where(ActionAudit.host_ident == host_ident)
    if operator:
        q, qc = q.where(ActionAudit.operator == operator), qc.where(ActionAudit.operator == operator)
    total = (await db.execute(qc)).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"total": total, "items": [
        {"id": r.id, "runbook_id": r.runbook_id, "host_ident": r.host_ident, "action": r.action,
         "params": r.params, "operator": r.operator, "guardrail": r.guardrail_verdict,
         "status": r.status, "result": (r.result or "")[:300], "created_at": str(r.created_at),
         "approval_mode": getattr(r, "approval_mode", ""), "approved_by": getattr(r, "approved_by", ""),
         "approved_at": str(r.approved_at or "")}
        for r in rows]}}


# ---------------- 技能 ----------------
@router.get("/skills")
async def list_skills(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(Skill).order_by(Skill.rca_priority, Skill.name))).scalars().all()
    # P4 效果：一次批量算完所有技能（真实流水复算；样本不足如实显示）
    try:
        effects = await skills_svc.skill_effects()
    except Exception:  # noqa: BLE001 —— 效果列算不出来不应让技能页打不开
        effects = {}
    return {"code": 0, "msg": "ok", "data": {"items": [
        {"id": r.id, "name": r.name, "description": r.description, "source": r.source,
         "enabled": r.enabled, "rca_priority": r.rca_priority, "alerts_scope": r.alerts_scope,
         "commands": r.commands or [], "tools": r.tools, "body_len": len(r.body_md or ""),
         "connection_check": r.connection_check or {},
         "requires": ((r.connection_check or {}).get("spec") or {}).get("requires") or [],
         "effect": effects.get(r.name) or {"skill": r.name, "loaded": 0, "insufficient": True}}
        for r in rows]}}


@router.get("/skills/{sid}")
async def skill_detail(sid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    r = await db.get(Skill, sid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    try:
        effect = await skills_svc.skill_effect(r.name)
    except Exception:  # noqa: BLE001
        effect = {"skill": r.name, "loaded": 0, "insufficient": True}
    return {"code": 0, "msg": "ok", "data": {
        "id": r.id, "name": r.name, "description": r.description, "body_md": r.body_md,
        "source": r.source, "enabled": r.enabled, "rca_priority": r.rca_priority,
        "alerts_scope": r.alerts_scope or [], "commands": r.commands or [],
        "effect": effect,
        "connection_check": r.connection_check or {}}}


@router.get("/skills/{sid}/effect")
async def skill_effect_api(sid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    """P4：单个技能的效果指标（可从 execution_steps/finding_triage/rca_findings 复算）。"""
    r = await db.get(Skill, sid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    return {"code": 0, "msg": "ok", "data": await skills_svc.skill_effect(r.name)}


class SkillIn(BaseModel):
    name: str
    description: str
    body_md: str = ""
    rca_priority: int = 10
    enabled: bool = True
    alerts_scope: list[str] = []      # 适用告警模式（fnmatch），空=不参与自动路由
    commands: list[str] = []          # 授权的只读命令（会话内白名单扩展）


def _clean_list(items, limit: int, maxlen: int) -> list[str]:
    """清洗字符串列表（去空白、去重、限量）——用户输入进库前的统一口径。"""
    out: list[str] = []
    for x in (items or []):
        s = str(x or "").strip()
        if not s or s in out:
            continue
        out.append(s[:maxlen])
        if len(out) >= limit:
            break
    return out


@router.post("/skills")
async def create_skill(body: SkillIn, request: Request = None, user: User = Depends(require("write"))):
    from fastapi import HTTPException

    if not (body.name or "").strip():
        raise HTTPException(422, "技能名为必填项（1~64 字符，建议小写连字符，如 check-disk）")
    if len(body.name.strip()) > 64:
        raise HTTPException(422, "技能名过长（≤64 字符）")
    if not (body.description or "").strip():
        raise HTTPException(422, "description 必填：缺失会导致技能失配")
    if len(body.description.strip()) > 512:
        raise HTTPException(422, "description 过长（≤512 字符）")
    scope = _clean_list(body.alerts_scope, 32, 64)
    cmds = _clean_list(body.commands, 32, 200)
    body = body.model_copy(update={"name": body.name.strip(), "description": body.description.strip(),
                                   "alerts_scope": scope, "commands": cmds})
    async with SessionLocal() as db:
        row = (await db.execute(select(Skill).where(Skill.name == body.name))).scalar_one_or_none()
        if row:
            row.description, row.body_md, row.rca_priority, row.enabled = (
                body.description, body.body_md, body.rca_priority, body.enabled)
            row.alerts_scope, row.commands = scope, cmds
            row.index_text = f"- {row.name}: {row.description}"[:512]
            await db.commit()
            return {"code": 0, "msg": "ok", "data": {"id": row.id, "updated": True}}
        s = Skill(name=body.name, description=body.description, body_md=body.body_md,
                  rca_priority=body.rca_priority, enabled=body.enabled, source="user",
                  alerts_scope=scope, commands=cmds,
                  index_text=f"- {body.name}: {body.description}"[:512])
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return {"code": 0, "msg": "ok", "data": {"id": s.id}}


@router.post("/skills/{sid}/toggle")
async def toggle_skill(sid: int, db=Depends(get_db), _: User = Depends(require("write"))):
    r = await db.get(Skill, sid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    r.enabled = not r.enabled
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"id": sid, "enabled": r.enabled}}


@router.post("/skills/{sid}/check")
async def check_skill(sid: int, request: Request = None, db=Depends(get_db),
                      user: User = Depends(require("write"))):
    """执行技能连接检查（fail-closed：检查不过的技能不会被诊断 Agent 加载）。"""
    r = await db.get(Skill, sid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    from app.services import skills as skills_svc

    res = await skills_svc.check_skill(r.name, persist=True)
    await audit(db=db, request=request, user=user, action="skill_check", resource=f"skill:{sid}",
                detail={"ok": res.get("ok")})
    return {"code": 0, "msg": "ok", "data": {**res, "name": r.name}}


@router.post("/skills/import")
async def import_skills(_: User = Depends(require("write"))):
    """扫描 skills 目录导入 SKILL.md（含内置核心技能同步）。"""
    builtin = await skills_svc.sync_builtin_skills()
    r = await skills_svc.import_skill_files()
    return {"code": 0, "msg": "ok", "data": {"builtin_synced": builtin, **r}}


class GithubImportIn(BaseModel):
    url: str


@router.post("/skills/import-github")
async def import_skill_from_github(body: GithubImportIn, request: Request = None,
                                   db=Depends(get_db), user: User = Depends(require("write"))):
    """从 GitHub 导入 SKILL.md（P3/FR-427c）。

    安全边界：仅 github.com / raw.githubusercontent.com，≤1MB，10s 超时；
    重名冲突返回 409（不静默覆盖）。
    """
    try:
        res = await skills_svc.import_from_github(body.url, user=getattr(user, "username", "") or "")
    except ValueError as e:
        msg = str(e)
        if "已存在" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(400, msg)
    except Exception as e:  # noqa: BLE001 —— 网络/解析异常都给人话
        raise HTTPException(400, f"GitHub 导入失败：{type(e).__name__}: {str(e)[:160]}")
    await audit(db=db, request=request, user=user, action="skill_import_github",
                resource=f"skill:{res.get('name')}", detail={"url": body.url})
    return {"code": 0, "msg": "ok", "data": res}


# ---------------- 案例卡 ----------------
def _as_list(v) -> list:
    """把 JSON 列的取值统一成数组。

    必须做（用户实测报障：案例库「查看 / 编辑」点了没反应）：case_cards 走 **raw SQL** 查询，
    MySQL 的 JSON 列由驱动返回的是**字符串**（如 `'["demo","证书"]'`），而前端把它当数组用
    （`tags.join(...)`）→ 抛 TypeError → 按钮静默失效。契约上 API 必须保证"tags 永远是数组"。
    """
    import json as _json

    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = _json.loads(v or "[]")
            return parsed if isinstance(parsed, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


@router.get("/case-cards")
async def list_case_cards(q: str = "", enabled: str = "", page: int = Query(1, ge=1),
                          page_size: int = Query(20, ge=1, le=200),
                          db=Depends(get_db), _: User = Depends(require("read"))):
    """案例卡列表（raw SQL：表含 VECTOR 列，不走 ORM）。

    支持关键词搜索（症状/根因/处置）与启用状态筛选——案例库页要能"找到那一张并纠正它"，
    而不是只能一页页翻（FR-416）。
    """
    from sqlalchemy import text

    where, params = [], {"l": page_size, "o": (page - 1) * page_size}
    if (q or "").strip():
        where.append("(symptom LIKE :kw OR root_cause LIKE :kw OR resolution_md LIKE :kw)")
        params["kw"] = f"%{q.strip()[:50]}%"
    if enabled in ("0", "1"):
        where.append("enabled = :en")
        params["en"] = int(enabled)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = (await db.execute(text(
        "SELECT id, incident_id, symptom, root_cause, resolution_md, tags, "
        "(embedding IS NOT NULL) AS has_vec, enabled, created_at FROM case_cards"
        + w + " ORDER BY id DESC LIMIT :l OFFSET :o"), params)).all()
    total = int((await db.execute(text("SELECT COUNT(*) FROM case_cards" + w), params)).scalar() or 0)
    return {"code": 0, "msg": "ok", "data": {"total": total, "items": [
        {"id": r[0], "incident_id": r[1], "symptom": r[2], "root_cause": r[3],
         "resolution_md": r[4], "tags": _as_list(r[5]), "has_embedding": bool(r[6]),
         "enabled": bool(r[7]), "created_at": str(r[8])}
        for r in rows]}}

    # 注意：COUNT 也必须带同样的 WHERE，否则分页数与筛选结果对不上（看着像"翻页丢失条目"）


@router.get("/case-cards/recall")
async def recall_cases(q: str, top_k: int = 3, user: User = Depends(require("read"))):
    return {"code": 0, "msg": "ok", "data": await cases_svc.recall(q, top_k, quota_role=user.role)}


class CaseIn(BaseModel):
    symptom: str
    root_cause: str
    resolution_md: str = ""
    tags: list = []
    incident_id: int | None = None


@router.post("/case-cards")
async def create_case_card(body: CaseIn, request: Request = None, user: User = Depends(require("write"))):
    from fastapi import HTTPException

    if not (body.symptom or "").strip():
        raise HTTPException(422, "symptom（现象）为必填项")
    if not (body.root_cause or "").strip():
        raise HTTPException(422, "root_cause（根因）为必填项")
    cid = await cases_svc.create_case_card(body.incident_id, body.symptom.strip(), body.root_cause.strip(),
                                           body.resolution_md, body.tags)
    return {"code": 0, "msg": "ok", "data": {"id": cid}}


class CasePatchIn(BaseModel):
    symptom: str | None = None
    root_cause: str | None = None
    resolution_md: str | None = None
    tags: list | None = None
    enabled: bool | None = None


@router.patch("/case-cards/{cid}")
async def patch_case_card(cid: int, body: CasePatchIn, request: Request = None,
                          db=Depends(get_db), user: User = Depends(require("write"))):
    """编辑案例卡（症状/根因/处置/标签/启用）。改症状或根因会**重算向量**。

    为什么需要编辑（FR-416）：案例卡是"平台学到的东西"，但它可能是错的——某次根因判断偏差、
    或那次处置其实无效。没有纠正手段时，错卡会一直参与向量召回、持续污染后续诊断。
    """
    from fastapi import HTTPException

    from app.services import cases as cases_svc

    if body.symptom is not None and not body.symptom.strip():
        raise HTTPException(422, "症状不能为空（要停用请用「停用」开关，而不是清空内容）")
    if body.root_cause is not None and not body.root_cause.strip():
        raise HTTPException(422, "根因不能为空（要停用请用「停用」开关）")
    changed = await cases_svc.update_case_card(
        cid, symptom=body.symptom, root_cause=body.root_cause, resolution_md=body.resolution_md,
        tags=body.tags, enabled=body.enabled)
    if not changed:
        raise HTTPException(404, "案例卡不存在或没有任何改动")
    await audit(db=db, request=request, user=user, action="case_card_update", resource=f"case:{cid}",
                detail={k: v for k, v in body.model_dump().items() if v is not None})
    return {"code": 0, "msg": "ok", "data": {"id": cid}}


@router.delete("/case-cards/{cid}")
async def delete_case_card(cid: int, request: Request = None, db=Depends(get_db),
                           user: User = Depends(require("write"))):
    """删除案例卡（无法纠正的废卡直接清掉，避免污染召回）。"""
    from fastapi import HTTPException

    from app.services import cases as cases_svc

    if not await cases_svc.delete_case_card(cid):
        raise HTTPException(404, "案例卡不存在")
    await audit(db=db, request=request, user=user, action="case_card_delete", resource=f"case:{cid}")
    return {"code": 0, "msg": "ok", "data": {"id": cid}}


@router.get("/case-cards/stats")
async def case_stats(_: User = Depends(require("read"))):
    from app.services.cases import VECTOR_ENABLED

    return {"code": 0, "msg": "ok", "data": {"total": await cases_svc.case_count(),
                                            "vector_enabled": VECTOR_ENABLED}}
