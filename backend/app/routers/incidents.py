"""问题看板 / 诊断 / ChatOps 路由（FR-D1~D4；05 §4）。

- incidents：列表/详情/报告/合并/拆分/关闭
- 诊断：POST 触发（SSE 流式 + 非流式两种入口），GET findings 回放工具瀑布
- ChatOps：会话 CRUD + 流式对话（只读工具）
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import HTTPException, APIRouter, Depends, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import audit, get_current_user, require, require_chat
from app.db.session import SessionLocal, get_db


# --- OpenAPI Schema (D52: 提前到 imports 之后, 让 list 接口的 response_model 解析不报 forward ref) ---


class ActiveService(BaseModel):
    rule_name: str
    metric: str
    metric_label: str | None = None      # D175-5：指标中文名（含维度）
    trigger_value: float | None = None   # D175-5：触发值（卡片直接亮读数）
    severity: int | None = None


class IncidentItem(BaseModel):
    """事件看板一条卡片的完整字段（D52 服务标签修复后；D175-5 增问题特征字段）。

    教训（D175-5 现场新增字段不生效）：response_model 是**白名单**——只改 `_inc_summary`
    的返回 dict、忘了同步这里，Pydantic 会**静默丢字段**（不报错、不告警，接口照常 200）。
    新增响应字段必须两处同步，守卫 test_d175 会锁。
    """
    id: int
    title: str
    severity: int
    status: str
    host_ident: str
    member_count: int
    active_services: list[ActiveService] = []
    confidence: str | None = None
    has_report: bool
    has_fixes: bool
    cluster_reason: str | None = None
    root_cause_head: str = ""            # D175-5：AI 结论一句话摘要（≤140 字）
    created_at: str
    updated_at: str
    resolved_at: str


class IncidentListData(BaseModel):
    """事件列表 data 字段——强类型让 OpenAPI 完整展示 items schema (含 active_services)。"""
    total: int
    items: list[IncidentItem]


class IncidentListOut(BaseModel):
    """事件列表响应 schema——给 OpenAPI 显示完整的 active_services 字段。"""
    code: int
    msg: str
    data: IncidentListData
from app.models.alert import AlertCurEvent, AlertHisEvent
from app.models.svc import AgentTask
from app.models.event import AIChatSession, ExecutionStep, Incident, IncidentThought, RCAFinding
from app.models.shell import User
from app.services import cluster as cluster_svc
from app.services import rca as rca_svc
from app.services import rca_tools
from app.services import prompts
from app.services.findings import _section
from app.services.runbook import has_fixes_section


def _has_fixes(md: str | None) -> bool:
    """报告是否含显式建议段（前端据此决定"生成变更单"是否可用）。"""
    return has_fixes_section(md or "")
from app.services.llm_gateway import call_llm

settings = get_settings()


def _now_iso() -> str:
    """消息时间戳（UTC ISO）。前端按本机时区渲染——存 UTC 是单一口径。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


router = APIRouter(prefix="/api/ops", tags=["ops-incident"])
log = logging.getLogger("incidents")

_running: set[int] = set()  # 同一 incident 同时只跑一次诊断

# 诊断兜底上限（秒）：长任务表里的 900s 只是保险丝，路由必须自己定 deadline。
# 多步 ReAct × 单次 LLM 最长 180s → 5 步就可能 900s，故取 840s 留出收尾余量。
DIAG_DEADLINE_S = 840


# ---------------- 问题看板 ----------------
@router.get("/incidents", response_model=IncidentListOut)
async def list_incidents(status: str = "", severity: int = 0,
                         page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                         db=Depends(get_db), user: User = Depends(require("read"))):
    from app.services import visibility

    q = select(Incident).order_by(Incident.severity, Incident.created_at.desc())
    qc = select(func.count()).select_from(Incident)
    vcond = await visibility.visible_fingerprints_filter(db, user, Incident.host_ident)
    if vcond is not None:
        q, qc = q.where(vcond), qc.where(vcond)
    if status:
        q, qc = q.where(Incident.status == status), qc.where(Incident.status == status)
    if severity:
        q, qc = q.where(Incident.severity == severity), qc.where(Incident.severity == severity)
    total = (await db.execute(qc)).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()

    # 本页所有事件的未恢复成员告警，聚合成 hash → (rule_name, metric) 映射，
    # 供卡片直接亮出「哪个服务出了什么问题」（避免只看到规则名+指纹、要点进去才知道）。
    active_map: dict[str, dict] = {}
    hashes = {h for r in rows for h in (r.alert_hashes or [])}
    if hashes:
        from app.models.alert import AlertHisEvent
        his = (await db.execute(
            select(AlertHisEvent).where(AlertHisEvent.hash.in_(hashes),
                                        AlertHisEvent.is_recovered.is_(False))
        )).scalars().all()
        for a in his:
            active_map.setdefault(a.hash, {"rule_name": a.rule_name, "metric": a.metric,
                                           "trigger_value": a.trigger_value,
                                           "severity": a.severity,
                                           "metric_label": _metric_label(a)})

    return IncidentListOut(
        code=0, msg="ok",
        data=IncidentListData(
            total=total,
            items=[_inc_summary(r, active_map) for r in rows],
        ),
    )  # noqa: ARC004


def _metric_label(ev) -> str:
    """指标中文名（含维度）——复用语音播报那套口径（同一个指标到处都该叫同一个名字）。"""
    from app.services.notify_format import _metric_info

    try:
        label, _ = _metric_info(ev)
    except Exception:  # noqa: BLE001 —— 展示层失败不该让列表 500
        return str(getattr(ev, "metric", "") or "")
    return label


def _root_cause_head(md: str, limit: int = 140) -> str:
    """从诊断报告里取「## 结论」首句（≤limit 字），供列表卡片一句话摘要。

    取不到就返回空串——**不编造**。报告可能是失败 stub（只有失败说明）或 INCONCLUSIVE。
    """
    from app.services.findings import _section

    try:
        # `_section` 的 title 不带 `##` 前缀（正则自己加）
        sec = _section(md or "", "结论") or ""
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join(x.strip() for x in sec.splitlines() if x.strip())
    text = re.sub(r"^[-*\d.、\s]+", "", text)
    text = re.sub(r"[\*\`#]", "", text)
    return text[:limit].strip()


def _inc_summary(r, active_map: dict[str, dict]) -> dict:
    """事件列表摘要。

    额外带 `active_services`：本事件**未恢复**成员告警去重后的 [{rule_name, metric}]，
    供列表卡片亮出「哪台主机的哪个服务出了什么问题」。
    """
    services: list[dict] = []
    seen: set[str] = set()
    for h in (r.alert_hashes or []):
        m = active_map.get(h)
        if m and (m["rule_name"], m["metric"]) not in seen:
            seen.add((m["rule_name"], m["metric"]))
            services.append(dict(m))
    # D175-5：卡片要能回答"最关键的是什么"——已有 AI 结论时给一句话摘要（≤140 字）。
    # 只给**结论**不给过程：过程在详情抽屉里，这里要的是一眼可判读。
    head = ""
    if r.ai_report_md:
        head = _root_cause_head(r.ai_report_md)
    return {"id": r.id, "title": r.title, "severity": r.severity, "status": r.status,
            "host_ident": r.host_ident, "member_count": len(r.alert_hashes or []),
            "active_services": services,
            "confidence": r.confidence, "has_report": bool(r.ai_report_md),
            "has_fixes": has_fixes_section(r.ai_report_md or ""),
            "root_cause_head": head,
            "cluster_reason": r.cluster_reason,
            "created_at": str(r.created_at), "updated_at": str(r.updated_at),
            "resolved_at": str(r.resolved_at or "")}


@router.get("/incidents/by-alert/{alert_hash}")
async def incident_by_alert(alert_hash: str, db=Depends(get_db),
                            user: User = Depends(require("read"))):
    """按告警指纹反查所属事件（飞书告警卡深链用）。

    告警通知发生在聚类之前，卡片里没有 incident_id；给用户一个
    `#/incidents/alert/<hash>` 深链，点击时事件已生成，由本接口解析。
    优先返回 open 事件，其次最新事件；不可见主机一律 404。
    """
    from app.services import visibility

    h = (alert_hash or "").strip().lower()
    if len(h) != 32 or any(c not in "0123456789abcdef" for c in h):
        raise HTTPException(404, "对应事件尚未生成或不可见")
    rows = (await db.execute(select(Incident))).scalars().all()
    cands = [r for r in rows if h in (r.alert_hashes or [])]
    cands.sort(key=lambda r: (r.status != "open", -r.id))
    # D184 修（公网真机实测 500）：单事件路径此前漏传 active_map（D175 加参时只改了
    # 列表路径）——这里按事件成员告警现构建同款映射，卡片服务标签不缺失。
    from app.models.alert import AlertCurEvent

    member_hashes = [x for r in cands for x in (r.alert_hashes or [])]
    amap: dict[str, dict] = {}
    if member_hashes:
        alerts = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash.in_(member_hashes)))).scalars().all()
        for a in alerts:
            amap.setdefault(a.hash, {"rule_name": a.rule_name, "metric": a.metric,
                                     "metric_label": _metric_label(a)})
    for r in cands:
        if await visibility.host_visible(db, user, r.host_ident):
            return {"code": 0, "msg": "ok", "data": _inc_summary(r, amap)}
    raise HTTPException(404, "对应事件尚未生成或不可见")


class IncidentIn(BaseModel):
    title: str
    severity: int = 2
    host_ident: str = ""
    alert_hashes: list[str] = []


@router.post("/incidents")
async def create_incident(body: IncidentIn, request: Request, db=Depends(get_db),
                          user: User = Depends(require("alert_ops"))):
    """手动建事件（05 §4：`POST /ops/incidents (手动建)`）。

    用途：运维自己发现的问题没走告警链路（如用户反馈、巡检之外的风险），
    建个事件挂上诊断与处置闭环。source_key 用 `manual:<随机>`，避免与告警锚点冲突。
    """
    import secrets

    title = (body.title or "").strip()
    if not title:
        raise HTTPException(422, "title 不能为空")
    if body.severity not in (1, 2, 3):
        raise HTTPException(422, "severity 只能是 1/2/3")
    inc = Incident(title=title[:255], severity=body.severity, status="open",
                   alert_hashes=list(body.alert_hashes or []), host_ident=body.host_ident or "",
                   source_key=f"manual:{secrets.token_hex(12)}", cluster_reason="manual_create")
    db.add(inc)
    await db.commit()
    await db.refresh(inc)
    await audit(db=db, request=request, user=user, action="incident_create", resource=f"incident:{inc.id}")
    return {"code": 0, "msg": "ok", "data": {"id": inc.id, "title": inc.title}}


@router.get("/incidents/{iid}")
async def incident_detail(iid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    inc = await db.get(Incident, iid)
    if inc is None:
        raise HTTPException(404, "资源不存在或已被删除")
    hashes = list(inc.alert_hashes or [])
    his = (await db.execute(select(AlertHisEvent).where(AlertHisEvent.hash.in_(hashes)))).scalars().all() if hashes else []
    findings = (await db.execute(select(RCAFinding).where(RCAFinding.incident_id == iid)
                                 .order_by(RCAFinding.id.desc()))).scalars().all()
    # D170（FR-426）：发现分诊——每份报告的最近人工处置 + 同主机忽略历史提示
    from app.models.event import FindingTriage

    fids = [f.id for f in findings]
    triage_map: dict[int, dict] = {}
    muted_note = ""
    if fids:
        tri_rows = (await db.execute(
            select(FindingTriage).where(FindingTriage.finding_id.in_(fids))
            .order_by(FindingTriage.created_at))).scalars().all()
        for t in tri_rows:      # created_at 升序遍历 → 后写覆盖 = 最新处置
            triage_map[t.finding_id] = {"action": t.action, "reason": t.reason,
                                        "decided_by": t.decided_by, "at": str(t.created_at)}
        if inc.host_ident:
            last_dismiss = (await db.execute(
                select(FindingTriage).where(
                    FindingTriage.host_ident == inc.host_ident,
                    FindingTriage.action == "dismissed")
                .order_by(FindingTriage.id.desc()).limit(1))).scalar_one_or_none()
            if last_dismiss:
                muted_note = (f"该主机此前有被忽略的诊断记录（{str(last_dismiss.created_at)[:16]}，"
                              f"理由：{last_dismiss.reason[:60]}）——同类发现请结合理由复核后再处置。")
    return {"code": 0, "msg": "ok", "data": {
        "id": inc.id, "title": inc.title, "severity": inc.severity, "status": inc.status,
        "host_ident": inc.host_ident, "ai_report_md": inc.ai_report_md, "root_cause": inc.root_cause,
        "confidence": inc.confidence, "cluster_reason": inc.cluster_reason,
        "triage_muted_note": muted_note,
        "has_fixes": _has_fixes(inc.ai_report_md),
        "created_at": str(inc.created_at), "resolved_at": str(inc.resolved_at or ""),
        "members": [{"hash": a.hash, "rule_name": a.rule_name, "metric": a.metric, "severity": a.severity,
                     "trigger_value": a.trigger_value, "trigger_time": str(a.trigger_time),
                     "is_recovered": a.is_recovered, "tags": a.tags} for a in his],
        "findings": [{"id": f.id, "status": f.status, "confidence": _conf_of(f), "strength": f.self_assessed_strength,
                      "tools_used": f.tools_used, "model": f.model, "total_tokens": f.total_tokens,
                      "triage": _triage_of(f, triage_map),
                      "started_at": str(f.started_at), "completed_at": str(f.completed_at or "")} for f in findings],
    }}


def _triage_of(f: RCAFinding, triage_map: dict) -> dict | None:
    t = triage_map.get(f.id)
    if not t:
        return None
    # strength → 建议档位（D170：分级只是初筛，最终以人工处置为准）
    sug = {"strong": "建议必跟", "moderate": "建议跟进",
           "weak": "可忽略区", "inconclusive": "可忽略区"}.get(f.self_assessed_strength or "", "")
    return {**t, "suggested": sug}


@router.post("/incidents/{iid}/findings/{fid}/triage")
async def triage_finding(iid: int, fid: int, body: dict, request: Request,
                         db=Depends(get_db), user: User = Depends(require("diag"))):
    """D170（FR-426）：诊断报告的人工分诊——发现分诊闭环的写入口。

    action：followup（跟进）/ observing（观察）/ dismissed（忽略）。
    dismissed 必填理由——忽略必须留痕，理由用于后续同类发现的降噪提示。
    """
    from app.models.event import FindingTriage

    action = str(body.get("action") or "").strip()
    if action not in ("followup", "observing", "dismissed"):
        raise HTTPException(400, "action 只允许 followup / observing / dismissed")
    reason = str(body.get("reason") or "").strip()
    if action == "dismissed" and not reason:
        raise HTTPException(400, "忽略必须填写理由——这是后续降噪与经验复用的依据")
    if len(reason) > 500:
        raise HTTPException(400, "理由过长（≤500 字符）")

    inc = await db.get(Incident, iid)
    if inc is None:
        raise HTTPException(404, "事件不存在或已被删除")
    f = await db.get(RCAFinding, fid)
    if f is None or f.incident_id != iid:
        raise HTTPException(404, "诊断报告不存在或不属于该事件")

    t = FindingTriage(finding_id=fid, incident_id=iid,
                      host_ident=inc.host_ident or "", metric="",
                      action=action, reason=reason, decided_by=user.username or "")
    db.add(t)
    await db.commit()
    await audit(db=db, request=request, user=user, action="finding_triage",
                detail=f"{action} finding={fid} reason={reason[:60]}")
    return {"code": 0, "msg": "ok", "data": {"finding_id": fid, "action": action}}


def _conf_of(f: RCAFinding) -> str | None:
    import re

    m = re.search(r"^confidence:\s*(\w+)", f.findings_md or "", re.M)
    return m.group(1) if m else None


@router.get("/incidents/{iid}/context")
async def incident_context(iid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    """P5b（FR-410）：该事件的**错误上下文** —— 错误簇 + 多源时间线 + 关联指标。

    用途：① 前端在事件详情里展示"有几类错、从哪台机器、什么时候开始"；
          ② 诊断 Agent 收尾时写入报告的「错误上下文」段（原文可核对）。
    """
    from app.services import error_context as ctx

    inc = await db.get(Incident, iid)
    if inc is None:
        raise HTTPException(404, "事件不存在或已被删除")
    hashes = list(inc.alert_hashes or [])
    his = (await db.execute(select(AlertHisEvent).where(AlertHisEvent.hash.in_(hashes)))).scalars().all() if hashes else []
    cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash.in_(hashes)))).scalars().all() if hashes else []

    # 日志簇：取该事件相关的取证任务结果（origin 里带本事件 id 的）
    tasks = (await db.execute(select(AgentTask).where(
        AgentTask.origin.like(f"incident:{iid}%")).order_by(AgentTask.id))).scalars().all()
    if not tasks:
        tasks = (await db.execute(select(AgentTask).where(
            AgentTask.kind == "log_fetch").order_by(AgentTask.id.desc()).limit(3))).scalars().all()
    segs: list[dict] = []
    for t in tasks:
        for seg in ((t.result_json or {}).get("segments") or []):
            s2 = dict(seg)
            s2["source_file"] = (t.payload_json or {}).get("path") or (t.payload_json or {}).get("unit") or ""
            segs.append(s2)

    clusters = ctx.cluster_segments(segs)
    # 关联指标：以最早的错误簇时间为中心（没有就退化成事件创建时间）
    center = clusters[0]["first_ts"] if clusters else str(inc.created_at)
    metrics: list[dict] = []
    if inc.host_ident:
        metrics = ctx.related_metrics_from_ring(
            inc.host_ident, ["cpu.percent", "mem.percent", "disk.percent", "load.1"], center, 10)

    timeline = ctx.build_timeline(
        incident={"title": inc.title, "created_at": inc.created_at, "resolved_at": inc.resolved_at},
        # 注意：`first_trigger_time` 只存在于**当前告警**表；历史告警（已恢复的成员）没有这一列，
        # 直接取属性会 AttributeError → 整个「错误上下文」卡片 500（实测：事件里一旦含有
        # 已恢复成员就整卡打不开）。故用 getattr 兜底到 trigger_time。
        alerts=[{"first_trigger_time": getattr(a, "first_trigger_time", None) or a.trigger_time,
                 "trigger_time": a.trigger_time,
                 "severity": a.severity, "rule_name": a.rule_name, "metric": a.metric,
                 "trigger_value": a.trigger_value, "host_ident": a.host_ident} for a in (his + cur)],
        logs=[{**c, "source_file": c.get("source_file", "")} for c in clusters],
        tasks=[{"created_at": t.created_at, "kind": t.kind, "status": t.status,
                "summary": f"{len((t.result_json or {}).get('segments') or [])} 段"} for t in tasks])

    return {"code": 0, "msg": "ok", "data": {
        "clusters": clusters, "timeline": timeline, "related_metrics": metrics,
        "log_tasks": [{"id": t.id, "status": t.status, "path": (t.payload_json or {}).get("path", ""),
                       "created_at": str(t.created_at)} for t in tasks],
        "report_md": ctx.error_context_section(clusters, timeline, metrics),
    }}


@router.get("/incidents/{iid}/findings")
async def incident_findings(iid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    """报告 + 工具瀑布回放（thoughts / execution_steps 三层留痕）。"""
    f = (await db.execute(select(RCAFinding).where(RCAFinding.incident_id == iid)
                          .order_by(RCAFinding.id.desc()))).scalars().first()
    if f is None:
        return {"code": 0, "msg": "ok", "data": None}
    inc = await db.get(Incident, iid)   # D170：降噪键需要 host_ident
    steps = (await db.execute(select(ExecutionStep).where(ExecutionStep.incident_id == iid)
                              .order_by(ExecutionStep.step_index))).scalars().all()
    thoughts = (await db.execute(select(IncidentThought).where(IncidentThought.incident_id == iid)
                                 .order_by(IncidentThought.step_index))).scalars().all()
    # D170（FR-426）：本报告的人工处置 + 同主机忽略历史提示（发现分诊闭环）
    from app.models.event import FindingTriage

    tri = (await db.execute(select(FindingTriage).where(FindingTriage.finding_id == f.id)
                            .order_by(FindingTriage.created_at))).scalars().all()
    triage = None
    for t in tri:      # 最新处置为准
        triage = {"action": t.action, "reason": t.reason, "decided_by": t.decided_by, "at": str(t.created_at)}
    sug = {"strong": "建议必跟", "moderate": "建议跟进",
           "weak": "可忽略区", "inconclusive": "可忽略区"}.get(f.self_assessed_strength or "", "")
    if triage and sug:
        triage["suggested"] = sug
    muted_note = ""
    if inc.host_ident:
        last_dismiss = (await db.execute(
            select(FindingTriage).where(
                FindingTriage.host_ident == inc.host_ident,
                FindingTriage.action == "dismissed")
            .order_by(FindingTriage.id.desc()).limit(1))).scalar_one_or_none()
        if last_dismiss:
            muted_note = (f"该主机此前有被忽略的诊断记录（{str(last_dismiss.created_at)[:16]}，"
                          f"理由：{last_dismiss.reason[:60]}）——同类发现请结合理由复核后再处置。")
    return {"code": 0, "msg": "ok", "data": {
        "finding_id": f.id, "status": f.status, "findings_md": f.findings_md,
        "self_assessed_strength": f.self_assessed_strength, "tools_used": f.tools_used,
        "tool_call_history": f.tool_call_history, "total_tokens": f.total_tokens, "model": f.model,
        "triage": triage, "triage_muted_note": muted_note,
        "steps": [{"step_index": s.step_index, "tool_name": s.tool_name, "tool_input": s.tool_input,
                   "tool_output": s.tool_output[:4000], "status": s.status, "duration_ms": s.duration_ms}
                  for s in steps],
        "thoughts": [{"step_index": t.step_index, "content": t.content} for t in thoughts],
    }}


class MergeIn(BaseModel):
    keep_id: int
    merge_id: int


def _cluster_result(r: dict) -> dict:
    """服务层 {ok,msg} → HTTP 语义（与 runbooks._svc 一致，见 D8 报告）。"""
    if r.get("ok"):
        return {"code": 0, "msg": "ok", "data": r}
    msg = str(r.get("msg") or "failed")
    if msg in ("NOT_FOUND", "INCIDENT_NOT_FOUND"):
        raise HTTPException(404, "事件不存在或已删除")
    raise HTTPException(400, msg)


class CorrectIn(BaseModel):
    root_cause: str
    resolution: str = ""
    note: str = ""


_CORR_ROOT_MAX = 4000
_CORR_RES_MAX = 4000
_CORR_NOTE_MAX = 500


@router.post("/incidents/{iid}/correct")
async def correct_finding(iid: int, body: CorrectIn, request: Request = None,
                          db=Depends(get_db), user: User = Depends(require("write"))):
    """人工订正诊断结论（D113）：保留 AI 原文不动，另建**人工案例卡**沉淀。

    订正不是覆盖留痕，而是把"人的判断"作为新证据入库：
      · case_cards 打标 human_corrected 并关联事件 → 后续诊断 search_cases 可召回
      · 审计 incident_correct 记录订正人/理由
    """
    from app.services import cases as cases_svc
    from app.services import visibility

    inc = await db.get(Incident, iid)
    if inc is None:
        raise HTTPException(404, "事件不存在")
    if not await visibility.host_visible(db, user, inc.host_ident):
        raise HTTPException(404, "事件不存在")
    if not (body.root_cause or "").strip():
        raise HTTPException(422, "真实根因必填")
    root_cause = body.root_cause.strip()[:_CORR_ROOT_MAX]
    resolution = (body.resolution or "").strip()[:_CORR_RES_MAX]
    note = (body.note or "").strip()[:_CORR_NOTE_MAX]
    case_id = await cases_svc.create_case_card(
        incident_id=iid,
        symptom=(inc.title or "")[:200],
        root_cause=root_cause,
        resolution_md=("**人工订正**\n\n" + (resolution or "（未填写处置）") +
                       ("\n\n订正理由：" + note if note else "")),
        tags=["human_corrected", f"incident:{iid}"],
        embed_text=f"{inc.title} {root_cause} {resolution}",
    )
    await audit(db=db, request=request, user=user, action="incident_correct",
                resource=f"incident:{iid}",
                detail={"root_cause": root_cause[:200], "case_id": case_id, "note": note[:200]})
    return {"code": 0, "msg": "ok", "data": {"case_id": case_id}}


@router.get("/incidents/{iid}/corrections")
async def list_corrections(iid: int, db=Depends(get_db), user: User = Depends(require("read"))):
    from app.services import cases as cases_svc
    from app.services import visibility
    inc = await db.get(Incident, iid)
    if inc is None or not await visibility.host_visible(db, user, inc.host_ident):
        raise HTTPException(404, "事件不存在")
    items = await cases_svc.list_for_incident(iid)
    human = [x for x in items if any("human_corrected" in str(t) for t in (x.get("tags") or []))]
    return {"code": 0, "msg": "ok", "data": {"items": human}}


@router.post("/incidents/merge")
async def merge_incident(body: MergeIn, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    r = await cluster_svc.manual_merge(body.keep_id, body.merge_id)
    if not r.get("ok"):
        return _cluster_result(r)
    await audit(db=db, request=request, user=user, action="incident_merge", resource=f"incident:{body.keep_id}")
    return _cluster_result(r)


class SplitIn(BaseModel):
    alert_hash: str


@router.post("/incidents/{iid}/split")
async def split_incident(iid: int, body: SplitIn, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    r = await cluster_svc.manual_split(iid, body.alert_hash)
    if not r.get("ok"):
        return _cluster_result(r)
    await audit(db=db, request=request, user=user, action="incident_split", resource=f"incident:{iid}")
    return _cluster_result(r)


@router.post("/incidents/{iid}/close")
async def close_incident(iid: int, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    inc = await db.get(Incident, iid)
    if inc is None:
        raise HTTPException(404, "资源不存在或已被删除")
    inc.status = "resolved"
    inc.resolved_at = datetime.utcnow()
    await db.commit()
    await audit(db=db, request=request, user=user, action="incident_close", resource=f"incident:{iid}")
    # FR-E3：事件关闭时自动沉淀案例卡（现象/根因/处置摘要 + 向量）
    case_id = None
    try:
        from app.services import cases as cases_svc

        # D69：向量文本用「标题 + 根因 + 报告摘要」——自动沉淀的 symptom 是事件标题
        # （短、含主机名/规则名），直接拿它算向量会退化成泛化文本、拖累整个案例库的
        # 检索质量（实测：一张 5 字泛卡能把 Top-1 命中率从 78% 拉到 67%）。
        # 展示字段仍旧简洁，只有**参与向量的文本**变丰富。
        _embed_text = " ".join(x for x in (
            inc.title or "", inc.root_cause or "", (inc.ai_report_md or "")[:800]) if x.strip())
        case_id = await cases_svc.create_case_card(
            iid, symptom=inc.title, root_cause=inc.root_cause or "（未定论）",
            resolution_md=inc.ai_report_md or "", tags=["auto"], embed_text=_embed_text)
    except Exception:  # noqa: BLE001 —— 案例沉淀失败不影响关闭
        log.exception("案例卡沉淀失败 incident=%s", iid)
    return {"code": 0, "msg": "ok", "data": {"id": iid, "case_card_id": case_id}}


@router.post("/incidents/{iid}/ack")
async def ack_incident(iid: int, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    inc = await db.get(Incident, iid)
    if inc is None:
        raise HTTPException(404, "资源不存在或已被删除")
    inc.status = "ack"
    await db.commit()
    await audit(db=db, request=request, user=user, action="incident_ack", resource=f"incident:{iid}")
    return {"code": 0, "msg": "ok", "data": {"id": iid}}


# ---------------- 诊断（SSE 流式） ----------------
@router.get("/incidents/{iid}/diagnose/stream")
async def diagnose_stream(iid: int, max_steps: int = rca_svc.MAX_STEPS, request: Request = None,
                          ticket: str = "", db=Depends(get_db)):
    """SSE 诊断流：thinking / tool_call / tool_result / hint / validate_retry / diag_done。

    鉴权（照 alerts/stream 模式，03 §1.22）：EventSource 无法自定义 header，所以
    前端先用 Bearer 换一次性 ticket 再以 `?ticket=` 建流；也兼容带 Bearer 的直连（curl 调试）。

    ⚠️ 夯实轮修（P0 真 bug）：此前本路由只挂 `Depends(require("diag"))`（只认 Bearer header），
    而前端用 `new EventSource(...?ticket=)` 建流 → **401，一键诊断在前端完全不可用**。
    之前之所以没暴露：验证都用 curl 带 Bearer 打 POST /diagnose（绕过了前端 EventSource 路径）。
    """
    from app.core.security import get_current_user
    from app.services import sse_ticket as tickets

    role = None
    if ticket:
        rec = tickets.consume(ticket)
        if rec is None:
            return JSONResponse({"code": 401, "msg": "TICKET_INVALID_OR_EXPIRED", "data": None}, status_code=401)
        role = rec["role"]
    else:  # 兼容带 header 的直连（curl 调试）
        import jwt as _jwt

        from app.core.config import get_settings

        auth = (request.headers.get("authorization", "") if request is not None else "")
        tok = auth[7:] if auth.lower().startswith("bearer ") else ""
        try:
            payload = _jwt.decode(tok, get_settings().jwt_secret, algorithms=["HS256"])
            role = payload.get("role", "member")
        except Exception:  # noqa: BLE001
            return JSONResponse({"code": 401, "msg": "未认证", "data": None}, status_code=401)
    # 诊断属"重活"（消耗 LLM token）：admin/member 可用，demo 角色不可（与 require("diag") 同口径）
    if role not in ("admin", "member"):
        return JSONResponse({"code": 403, "msg": "无诊断权限（需 admin/member）", "data": None}, status_code=403)

    if iid in _running:
        return JSONResponse({"code": 409, "msg": "DIAG_ALREADY_RUNNING", "data": None}, status_code=409)
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def sink(kind, payload):
        await q.put({"kind": kind, "payload": payload})

    async def runner():
        _running.add(iid)
        try:
            await rca_svc.diagnose(iid, emitter=rca_svc.DiagEmitter(sink),
                                  max_steps=max(2, min(max_steps, 60)), quota_role=role or "")
        except Exception as e:  # noqa: BLE001
            await sink("error", {"msg": f"{type(e).__name__}: {e}"})
        finally:
            _running.discard(iid)
            await q.put(None)

    async def gen():
        task = asyncio.create_task(runner())
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    yield "event: end\ndata: {}\n\n"
                    break
                yield f"event: {item['kind']}\ndata: {json.dumps(item['payload'], ensure_ascii=False)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/incidents/{iid}/diagnose")
async def diagnose_sync(iid: int, max_steps: int = rca_svc.MAX_STEPS, request: Request = None,
                        db=Depends(get_db), user: User = Depends(require("diag"))):
    """非流式诊断（脚本/CI 用；耗时较长，前端请用 stream 入口）。

    D20 修复：本接口是**长任务**（多步 ReAct，每步一次 LLM，单次最长 180s），
    原先未登记进 main.py 的 LONG_TASK_ROUTES → 被 25s DB 兜底当"数据库不可达"杀掉 → 500。
    实测（LLM 恢复可用后）`POST /api/ops/incidents/1000033/diagnose` → 500 @25.0s。
    现已在长任务表登记（兜底 900s），并在此自带 deadline 防真挂死。
    """
    if iid in _running:
        return {"code": 409, "msg": "DIAG_ALREADY_RUNNING", "data": None}
    _running.add(iid)
    try:
        try:
            r = await asyncio.wait_for(
                rca_svc.diagnose(iid, max_steps=max(2, min(max_steps, 60)),
                                 quota_role=user.role),
                timeout=DIAG_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            log.warning("诊断超时 %ss: incident=%s", DIAG_DEADLINE_S, iid)
            raise HTTPException(504, f"诊断超时（>{DIAG_DEADLINE_S}s），已中止；可减少 max_steps 后重试")
    finally:
        _running.discard(iid)
    await audit(db=db, request=request, user=user, action="incident_diagnose", resource=f"incident:{iid}")
    return {"code": 0 if r.get("ok") else 400, "msg": "ok" if r.get("ok") else r.get("msg", "failed"), "data": r}


# ---------------- ChatOps ----------------
class ChatIn(BaseModel):
    message: str
    session_id: int | None = None
    target_hosts: list[str] = []   # D122：会话聚焦主机（ident 列表；空=全局对话）


@router.get("/chat/suggestions")
async def chat_suggestions(user: User = Depends(require_chat())):
    """AI 助手的建议问题（D47 实时生成 → D49 定时预生成 + 缓存）。

    用户要求：「打开对话框不能让用户等模型」——本接口**只读缓存**（毫秒级返回）。
    预生成由调度器定时执行（白天每小时；北京 23:00–08:00 每 3 小时），见
    services/chat_suggest.py；缓存为空（刚重启）时先返回规则版并后台补生成。
    """
    from app.db.session import SessionLocal
    from app.services import chat_suggest

    async with SessionLocal() as db:
        data = await chat_suggest.snapshot(db, user)
    return {"code": 0, "msg": "ok", "data": data}


@router.post("/chat/suggestions/refresh")
async def chat_suggestions_refresh(user: User = Depends(require_chat())):
    """手动刷新建议问题（D49）：立即按当前状态调一次模型；30s 冷却防连点。"""
    from app.db.session import SessionLocal
    from app.services import chat_suggest

    async with SessionLocal() as db:
        data = await chat_suggest.refresh_manual(db, user)
    return {"code": 0, "msg": "ok", "data": data}

@router.get("/chat/sessions")
async def chat_sessions(db=Depends(get_db), user: User = Depends(require("read"))):
    rows = (await db.execute(select(AIChatSession).where(AIChatSession.user_id == user.id)
                             .order_by(AIChatSession.updated_at.desc()).limit(50))).scalars().all()
    # D148：0 条消息的空会话不展示。它们只会在「请求中途失败」时产生（会话先建、消息后存），
    # 用户点进去是一片空白——见下方 chat_stream 的落库时机修复。历史里不该出现空壳。
    items = [r for r in rows if r.messages]
    return {"code": 0, "msg": "ok", "data": {"items": [
        {"id": r.id, "title": r.title, "updated_at": str(r.updated_at), "messages": len(r.messages or []),
         "folded": int(r.summary_upto or 0)} for r in items]}}


@router.get("/chat/sessions/{sid}")
async def chat_session_detail(sid: int, db=Depends(get_db), user: User = Depends(require("read"))):
    s = await db.get(AIChatSession, sid)
    if s is None or s.user_id != user.id:
        raise HTTPException(404, "资源不存在或已被删除")
    # D87：回传摘要与折叠边界 —— 前端据此显示"更早 N 轮已压缩"，与后端实际发给模型的窗口一致
    return {"code": 0, "msg": "ok", "data": {"id": s.id, "title": s.title, "messages": s.messages,
                                            "summary": s.summary, "summary_upto": int(s.summary_upto or 0)}}


@router.delete("/chat/sessions/{sid}")
async def chat_session_delete(sid: int, db=Depends(get_db), user: User = Depends(get_current_user)):
    """删自己的会话。自服务操作（只动 user_id 命中的行），故不设 write 门禁——
    否则 demo 能建会话却删不掉，侧栏的 ✕ 会一直 403。"""
    s = await db.get(AIChatSession, sid)
    if s and s.user_id == user.id:
        await db.delete(s)
        await db.commit()
    return {"code": 0, "msg": "ok", "data": {"id": sid}}


@router.post("/chat/sessions/{sid}/compact")
async def chat_compact(sid: int, db=Depends(get_db), user: User = Depends(get_current_user)):
    """**手动压缩**当前会话历史（D88，用户要求加按钮）。

    自动压缩只在触发阈值时发生（省钱）；用户想"现在就收一收、给模型腾地方"时，
    显式压缩有两点价值：① 主动裁剪 → 后续轮次的窗口能装下更多最近内容；
    ② 下次回答不用带上全部旧历史 → 更快更省。
    只动**自己**的会话（会话隔离，与删除同纪律）。
    """
    from app.services import chat_context as ctx

    s = await db.get(AIChatSession, sid)
    if s is None or s.user_id != user.id:
        raise HTTPException(404, "资源不存在或已被删除")
    msgs = list(s.messages or [])
    budget = int(settings.chat_context_budget or 8000)
    summary, upto, stats = await ctx.maybe_compact(
        db, msgs, s.summary or "", int(s.summary_upto or 0),
        budget, threshold=0, quota_role=user.role)   # threshold=0 → 强制压缩
    s.summary = summary or None
    s.summary_upto = upto
    await db.commit()
    if not stats["compacted"]:
        return {"code": 0, "msg": "ok", "data": {"compacted": False,
                "note": "当前没有需要折叠的历史回合（最近窗口内都放得下）。对话继续变长后它会自动压缩。"}}
    return {"code": 0, "msg": "ok", "data": {
        "compacted": True, "summary_upto": upto,
        "folded": upto, "kept": stats["kept"], "note": f"已把更早的 {upto} 条回合折叠为摘要。（{stats['overflow_tokens']} token）"}}


async def _persist_chat_failure(sess_id: int | None, err_text: str) -> None:
    """把失败原因作为一条助手消息落库（D148）。

    为什么要落：用户问题已在开始时落库，如果只弹个 toast，历史里就是"有问无答"；
    把错误写进会话，回看时能明确知道"这轮为什么没答上"，而不是以为助手没理人。
    用**独立会话**写：主事务可能已因异常处于不可用状态，不能复用。
    """
    if not sess_id:
        return
    try:
        async with SessionLocal() as db2:
            s = await db2.get(AIChatSession, sess_id)
            if s is None:
                return
            msgs = list(s.messages or [])
            if msgs and msgs[-1].get("role") == "assistant":
                return          # 已经答完了（异常发生在后处理）→ 不重复追加
            msgs.append({"role": "assistant", "content": err_text, "ts": _now_iso(), "error": True})
            s.messages = msgs
            s.updated_at = datetime.utcnow()
            await db2.commit()
    except Exception:  # noqa: BLE001 —— 失败留痕本身不得再抛
        pass


@router.post("/chat/stream")
async def chat_stream(body: ChatIn, user: User = Depends(require_chat())):
    """ChatOps 流式对话（只读工具）。SSE 事件：thinking / tool_call / tool_result / token / meta / end。

    门禁：admin/member 恒可用；demo 由 DEMO_CHAT_ENABLED 决定（默认可只读问答）。

    上下文管理（D87，用户要求"调研通用对话类怎么管理上下文"）：
    历史不再整段发给模型，改走 **Hybrid 记忆**——滚动摘要（`session.summary`）+ 最近窗口，
    按 token 预算触发**增量**压缩。详见 services/chat_context.py 的模块注释（含调研结论与三条纪律）。
    """
    # 入参护栏（D87）：空消息与超长消息在**进入模型前**就挡掉，避免白烧一次调用。
    # 超长不清空、只拒绝并给出可操作提示（截断会静默篡改用户的输入，比拒绝更糟）。
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "消息不能为空")
    max_chars = int(settings.chat_msg_max_chars or 0)
    if max_chars and len(text) > max_chars:
        raise HTTPException(400, f"消息过长（{len(text)} 字，上限 {max_chars} 字）。"
                                 "请拆成几条发送，或只贴关键段落——助手一次看太多反而抓不住重点。")

    q: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def runner():
        sess = None
        sess_id = None
        try:
            from app.services import chat_context as ctx

            budget = int(settings.chat_context_budget or 8000)
            threshold = int(settings.chat_compact_threshold or 1500)
            async with SessionLocal() as db:
                sess = await db.get(AIChatSession, body.session_id) if body.session_id else None
                if sess is None or sess.user_id != user.id:
                    # 会话隔离：查不到或**不属于自己**都新建（绝不接管别人的会话，D87 回归锁在测）
                    sess = AIChatSession(user_id=user.id, title=text[:40] or "新会话",
                                         messages=[], summary=None, summary_upto=0,
                                         target_hosts=(body.target_hosts or [])[:5])
                    db.add(sess)
                    await db.commit()
                    await db.refresh(sess)
                elif body.target_hosts is not None and (body.target_hosts or []) != (sess.target_hosts or []):
                    # 用户中途改选了聚焦主机 → 覆盖会话记录（下一轮对话即生效）
                    sess.target_hosts = (body.target_hosts or [])[:5]
                    await db.commit()
                    await db.refresh(sess)
                sess_id = sess.id
                msgs = list(sess.messages or [])
                msgs.append({"role": "user", "content": text, "ts": _now_iso()})
                # D148：用户问题**立即落库**。此前只在整轮回答成功后才存——
                # 模型报错/客户端断开时，会话里连"我问过什么"都没有，历史点开一片空白
                # （公网实测：MiniMax 欠费期间留下一串「0 条」空会话）。
                sess.messages = msgs
                sess.updated_at = datetime.utcnow()
                await db.commit()
                await db.refresh(sess)   # commit 会 expire 属性；异步会话里懒加载会炸

                # ① 按预算决定是否压缩（绝大多数轮次不触发，不额外花钱）
                summary, upto, stats = await ctx.maybe_compact(
                    db, msgs, sess.summary or "", int(sess.summary_upto or 0),
                    budget, threshold, quota_role=user.role)
                # ② 用窗口拼上下文：system + 聚焦主机上下文 + 摘要 + 最近窗口
                focus_block = ""
                th = (body.target_hosts or [])[:5] or (sess.target_hosts or [])[:5]
                if th:
                    from app.models.host import Host as _Host
                    rows = (await db.execute(select(_Host).where(_Host.ident.in_(th)))).scalars().all()
                    by_id = {h.ident: h for h in rows}
                    lines = []
                    for i in th:
                        h = by_id.get(i)
                        if h is None:
                            continue
                        lines.append(f"- {h.name}（ident {h.ident[:12]}…，IP {h.ip or '-'}，{h.os or '-'}，"
                                     f"状态 {h.status}，Agent {h.agent_version or '-'}）")
                    if lines:
                        focus_block = (
                            "\n\n【用户当前聚焦的主机】（用户未点名机器时，查询与工具调用优先针对以下主机）\n"
                            + "\n".join(lines))
                history = ctx.build_history(prompts.CHAT_PROMPT + focus_block, summary, msgs, upto, budget)
                round_tokens = ctx.total_tokens(history)
                tools = [t for t in rca_tools.tool_specs() if t["function"]["name"] != "write_findings"]
                seen: set[str] = set()
                answer = ""
                # D173/P2：本次对话会话内技能授权的只读命令（load_skill 拉到的技能）
                chat_cmds: set[str] = set()
                for _ in range(8):
                    r = await call_llm(db, "chat", history, tools=tools, model_role="main", quota_role=user.role)
                    calls = rca_svc._extract_tool_calls(r)
                    if not calls:
                        answer = r.content
                        break
                    history.append({"role": "assistant", "content": r.content or "", "tool_calls": calls})
                    for tc in calls:
                        name = tc["function"]["name"]
                        try:
                            args = json.loads(tc["function"].get("arguments") or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        key = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                        await q.put({"kind": "tool_call", "payload": {"tool": name, "params": args}})
                        if key in seen:
                            tool_text = f"[{name}] 已拒绝重复调用（相同参数）"
                        else:
                            seen.add(key)
                            if name == "load_skill":
                                try:
                                    from app.services import skills as _sk

                                    chat_cmds.update(await _sk.skill_commands(str(args.get("name") or "")))
                                except Exception:  # noqa: BLE001 —— 授权命令读取失败不影响对话
                                    log.warning("读取技能授权命令失败", exc_info=True)
                            res = await rca_tools.invoke(name, args, allowed=sorted(chat_cmds))
                            tool_text = rca_tools.tool_result_text(name, res, 20000)
                        await q.put({"kind": "tool_result", "payload": {"tool": name, "preview": tool_text[:600]}})
                        history.append({"role": "tool", "tool_call_id": tc.get("id", name), "content": tool_text})
                if not answer:
                    # ③ 孤儿轮次修复（D87）：工具轮次用尽仍没结论时，以前**什么都不存**——
                    # 用户的问题留在库里却没有回答，回看历史像"助手没理我"。
                    # 现在如实说明并落库（这也符合"取不到就说取不到"的纪律）。
                    read_tools = sorted({c['function']['name'] for m in history
                                         if m.get('role') == 'assistant'
                                         for c in (m.get('tool_calls') or [])})
                    answer = ("（本轮连续调用工具已达上限（8 轮）仍未得出确定结论。）\n\n"
                              "已查阅：" + ("、".join(read_tools) or "无") +
                              "\n建议把问题拆小一点再问，例如指定主机、时间范围或具体指标。")
                msgs.append({"role": "assistant", "content": answer, "ts": _now_iso()})
                await q.put({"kind": "token", "payload": {"text": answer}})

                # ④ 存储上限：防止单行 JSON 无限增长（更早的已归档进摘要）
                msgs, upto = ctx.trim_stored(msgs, upto, int(settings.chat_max_stored_messages or 0))
                sess.messages = msgs
                sess.summary = summary or None
                sess.summary_upto = upto
                sess.updated_at = datetime.utcnow()
                await db.commit()
                await q.put({"kind": "meta", "payload": {
                    "session_id": sess.id, "title": sess.title,
                    # 上下文用量（前端展示"记得多少"）：folded=已压缩轮数，kept=窗口内条数
                    "context": {"kept": stats["kept"], "folded": stats["folded_candidates"] if stats["compacted"] else 0,
                                "folded_total": upto, "tokens": round_tokens, "budget": budget,
                                "compacted": bool(stats["compacted"])},
                }})
        except HTTPException as he:
            # 入参/配额类错误：把 API 契约里那句人话原样带出去（前端直接显示 msg）
            await _persist_chat_failure(sess_id, str(he.detail))
            await q.put({"kind": "error", "payload": {"msg": he.detail, "status": he.status_code}})
        except Exception as e:  # noqa: BLE001
            await _persist_chat_failure(sess_id, f"本次回答失败（{type(e).__name__}），请稍后重试。")
            await q.put({"kind": "error", "payload": {"msg": f"{type(e).__name__}: {e}"}})
        finally:
            await q.put(None)


    async def gen():
        task = asyncio.create_task(runner())
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    yield "event: end\ndata: {}\n\n"
                    break
                yield f"event: {item['kind']}\ndata: {json.dumps(item['payload'], ensure_ascii=False)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
