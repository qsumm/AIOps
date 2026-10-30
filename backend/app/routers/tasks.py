"""任务通道：平台→Agent 的受控任务下发与回传（P5a，FR-409/411；详设 §1.28）。

三条硬约束（安全边界的全部）：
  ① **只有两种任务类型**：`log_fetch`（只读取证）/ `svc_action`（枚举动作 ID）；
  ② **平台侧不保存任何目标机凭据**（无 SSH 私钥）——复用 Agent 的 ident/token，通道由 Agent 主动拉取；
  ③ **每个动作都留痕**：下发与回填各写一条审计，任务本身落 `agent_tasks` 可回放。

限额与熔断（详设 §1.28）：
  · 每主机并发 1 个 pending（同一台机器不并行取日志，避免把目标机压垮）
  · `log_fetch` 每主机每时 ≤20 次；`svc_action` 每主机每时 ≤3 次
  · `svc_action` 连续 2 次校验失败 → 该服务熔断（转人工），需显式重置
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.security import audit, require
from app.db.session import get_db
from app.models.host import Host
from app.models.shell import User
from app.models.svc import AgentTask, HealGuard, Service, ServiceHeal

log = logging.getLogger("tasks")
router = APIRouter(prefix="/api/ops", tags=["ops-tasks"])

LOG_FETCH_PER_HOUR = 20
SVC_ACTION_PER_HOUR = 3
TASK_TTL_MIN = 30            # 任务过期时间（Agent 一直不来取则作废）
FAIL_STREAK_TRIP = 2         # 连续失败阈值（行业口径：2 次失败即停并转人工）
RECLAIM_MIN = 5              # 僵尸任务回收：picked 超过这么久未回填即视为丢失

# 关于 RECLAIM_MIN 的必要性（实测踩到）：拉取式通道里，Agent 可能领取后崩溃/机器重启，
# 那条任务会永远停在 picked —— 而"每主机并发 1"的限额就会把该主机的通道**永久堵死**，
# 且用户看到的是"已有任务在执行"这种解释不通的提示。故必须回收：超过窗口的 picked 视为
# 已丢失（expired），通道自动恢复。


# ── 鉴权：Agent 侧（X-Ident / X-Token）──────────────────────────────
async def _agent_host(db, ident: str, token: str) -> Host:
    from app.services import ingest

    if not ident or not token:
        raise HTTPException(401, "缺少 X-Ident / X-Token")
    return await ingest.auth_agent(db, ident, token)


# ── Agent 侧：领取任务 ──────────────────────────────────────────────
@router.get("/agents/config")
async def agent_config(db=Depends(get_db), x_ident: str = Header(default=""),
                       x_token: str = Header(default="")):
    """Agent 领取**平台侧声明**（当前只有服务清单）。

    为什么要有这个端点（验收时踩到的真缺口）：应用类服务无法自动发现，必须先声明——
    而声明此前只存在于"安装命令的 --services"，用户在界面/API 里登记一个应用类服务后，
    Agent **根本不知道要探测它**，服务就永远停在「未知」。这就是"创建了然后呢"那类毛病。
    现在改为：声明存在平台上，Agent 每次来拉一份（与任务通道同一套机器凭证、同一套拉取逻辑，
    不需要给目标机开入站端口），本地 `--services` 只作为"平台不可达时的兜底"。
    """
    host = await _agent_host(db, x_ident, x_token)
    rows = (await db.execute(select(Service).where(
        Service.host_id == host.id, Service.enabled.is_(True)).order_by(Service.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"services": [
        {"type": s.type, "name": s.name, **(s.spec_json or {})} for s in rows]}}


# ── Agent 侧：领取任务 ──────────────────────────────────────────────
@router.get("/agents/tasks")
async def pick_task(db=Depends(get_db), x_ident: str = Header(default=""),
                    x_token: str = Header(default="")):
    """Agent 领取一个任务（原子领取：同一条不会被取两次）。无任务回 204。"""
    host = await _agent_host(db, x_ident, x_token)
    now = datetime.utcnow()
    # ① 作废过期 pending（Agent 长期不来取）
    expired = (await db.execute(select(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.status == "pending",
        AgentTask.expires_at.is_not(None), AgentTask.expires_at < now))).scalars().all()
    for t in expired:
        t.status = "expired"
    # ② 回收僵尸 picked（Agent 领取后崩溃/重启 → 否则通道被"并发 1"永久堵死）
    stale = (await db.execute(select(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.status == "picked",
        AgentTask.picked_at.is_not(None),
        AgentTask.picked_at < now - timedelta(minutes=RECLAIM_MIN)))).scalars().all()
    for t in stale:
        t.status = "expired"
        t.error = (t.error or "") + f"（Agent 领取后 {RECLAIM_MIN} 分钟未回填，已回收）"
    if expired or stale:
        await db.commit()

    t = (await db.execute(select(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.status == "pending",
        (AgentTask.expires_at.is_(None)) | (AgentTask.expires_at > now),
    ).order_by(AgentTask.id).limit(1))).scalar_one_or_none()
    if t is None:
        from fastapi.responses import Response
        return Response(status_code=204)
    t.status, t.picked_at = "picked", now
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"task": {
        "id": t.id, "kind": t.kind, "payload": t.payload_json or {},
        "post": (t.payload_json or {}).get("_post") or {},
    }}}


class TaskResultIn(BaseModel):
    result: dict


@router.post("/agents/tasks/{tid}/result")
async def post_task_result(tid: int, body: TaskResultIn, db=Depends(get_db),
                           x_ident: str = Header(default=""), x_token: str = Header(default="")):
    """Agent 回填任务结果。**只接受属于本主机的任务**（对抗项：领取他人任务必须失败）。"""
    host = await _agent_host(db, x_ident, x_token)
    t = await db.get(AgentTask, tid)
    if t is None or t.host_id != host.id:
        raise HTTPException(404, "任务不存在或不属于本主机")

    res = _mask_result(body.result or {})
    t.result_json = res
    t.status = "done" if res.get("ok") else "failed"
    t.error = str(res.get("error") or "")[:2000]
    t.done_at = datetime.utcnow()
    await db.commit()

    # 自愈计量与熔断（仅 svc_action）
    if t.kind == "svc_action":
        await _record_heal(db, t, res)
    # P2：日志自动发现 → 回写"建议 log_path"（AI 取证与 UI「取日志」两条路径统一在这落）。
    # 只在"发现成功 + 该服务尚未声明 log_path"时写；不覆盖人工声明。
    if t.kind == "log_fetch":
        try:
            disc = res.get("discovered") if isinstance(res.get("discovered"), dict) else None
            chosen = str((disc or {}).get("chosen") or "")
            if chosen and (t.payload_json or {}).get("discover") and (t.origin or "").startswith("service:"):
                sid = int(t.origin.split(":", 1)[1])
                from app.models.svc import Service

                s = await db.get(Service, sid)
                if s is not None:
                    spec = dict(s.spec_json or {})
                    if not spec.get("log_path") and spec.get("suggested_log_path") != chosen:
                        spec["suggested_log_path"] = chosen
                        spec["suggested_log_path_at"] = datetime.utcnow().isoformat() + "Z"
                        s.spec_json = spec
                        await db.commit()
        except Exception:  # noqa: BLE001 —— 建议回写失败不影响任务结果
            pass
    await audit(db=db, request=None, user=None, action=f"task_result_{t.kind}",
                resource=f"task:{tid}", detail={"ok": bool(res.get("ok")),
                                                "origin": t.origin})
    return {"code": 0, "msg": "ok", "data": {"id": tid, "accepted": True}}


def _mask_result(res: dict) -> dict:
    """结果脱敏（FR-409：日志里可能含密码/Token/凭据）。

    口径与渠道配置一致：**保留可辨认的前缀与尾部，中段打码**——
    目的是让 AI 仍能读懂"这里有个凭据"，但平台上不留明文。
    """
    import re

    def _mask_text(s: str) -> str:
        """脱敏。**顺序有讲究**（实测踩过）：必须先处理 `Authorization: Bearer xxx`
        这类"带前缀的凭据"，否则通用 kv 规则会把 `Authorization` 当键吃掉前缀、
        把真正的 token 留在原地。"""
        out = str(s)
        # ① 认证头（先做）：`Bearer <token>` / `Basic <b64>`
        out = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{6,}", r"\1 ***", out)
        # ② 任意 scheme 的 URL 内嵌凭据（含 mysql://root:pw@host 这类 DSN）
        out = re.sub(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@", r"\1\2:***@", out)
        # ③ password=/pwd: 等显式键（token/secret/api_key 同理）
        out = re.sub(r"(?i)\b(pass(word|wd)?|secret|token|api[_-]?key)\b\s*[=:]\s*(\S+)",
                     lambda m: f"{m.group(1)}=***", out)
        # ④ 兜底：Authorization 头本身若还带值
        out = re.sub(r"(?i)\b(authorization)\b\s*[=:]\s*(\S+)", r"\1=***", out)
        return out

    def walk(v):
        if isinstance(v, str):
            return _mask_text(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: ("***" if str(k).lower() in ("password", "secret", "token", "api_key", "passwd")
                        else walk(x)) for k, x in v.items()}
        return v

    return walk(res)


async def _record_heal(db, task: AgentTask, res: dict) -> None:
    """记录自愈结果并维护熔断状态（FR-411）。"""
    payload = task.payload_json or {}
    pid = payload.get("service_id")
    if not pid:
        return
    post_ok = bool((res.get("post_check") or {}).get("ok"))
    ok = bool(res.get("ok")) and post_ok
    db.add(ServiceHeal(service_id=int(pid), host_ident="", action=str(payload.get("action") or ""),
                       ok=ok, detail=str(res.get("error") or "")[:250], task_id=task.id))
    g = (await db.execute(select(HealGuard).where(HealGuard.service_id == int(pid)))).scalar_one_or_none()
    if g is None:
        g = HealGuard(service_id=int(pid))
        db.add(g)
    g.last_action_at = datetime.utcnow()
    if ok:
        g.fail_streak = 0
    else:
        g.fail_streak = int(g.fail_streak or 0) + 1
        if g.fail_streak >= FAIL_STREAK_TRIP and not g.tripped:
            g.tripped = True
            g.tripped_reason = f"连续 {g.fail_streak} 次自愈校验未通过，已停止自动执行（需人工介入）"
            log.warning("服务 %s 自愈熔断：%s", pid, g.tripped_reason)
    await db.commit()


# ── 平台侧：下发任务 ────────────────────────────────────────────────
async def enqueue(db, host: Host, kind: str, payload: dict, origin: str = "",
                  created_by: str = "") -> AgentTask:
    """入队一个任务（含限额判定）。调用方负责给出可读的错误。"""
    if kind not in ("log_fetch", "svc_action"):
        raise HTTPException(400, f"任务类型只允许 log_fetch / svc_action，收到 {kind!r}")
    now = datetime.utcnow()
    # 只把"仍在回收窗口内"的 picked 算作在跑（僵尸任务不该堵住通道）
    running = (await db.execute(select(func.count()).select_from(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.status == "picked",
        AgentTask.picked_at > now - timedelta(minutes=RECLAIM_MIN)))).scalar() or 0
    if running:
        raise HTTPException(429, "该主机已有任务在执行（同一主机不并行取证，避免压垮目标机）；"
                                 f"若确认 Agent 已停止，{RECLAIM_MIN} 分钟后自动回收")
    pend = (await db.execute(select(func.count()).select_from(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.status == "pending"))).scalar() or 0
    if pend:
        raise HTTPException(429, "该主机已有待领取任务，请等待 Agent 领取（约 15 秒）")

    hour_ago = now - timedelta(hours=1)
    per_hour = LOG_FETCH_PER_HOUR if kind == "log_fetch" else SVC_ACTION_PER_HOUR
    used = (await db.execute(select(func.count()).select_from(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.kind == kind,
        AgentTask.created_at >= hour_ago))).scalar() or 0
    if used >= per_hour:
        raise HTTPException(429, f"该主机本小时 {kind} 已达上限（{per_hour} 次/时）——"
                                 "这是防滥用与保护目标机的硬限额")

    t = AgentTask(host_id=host.id, kind=kind, payload_json=payload, origin=origin,
                  created_by=created_by, expires_at=now + timedelta(minutes=TASK_TTL_MIN))
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


class LogFetchIn(BaseModel):
    ident: str
    path: str = ""
    unit: str = ""
    since: str = ""
    until: str = ""
    pattern: str = ""
    level: str = "auto"
    context: int = 3


def _check_log_path_shape(path: str) -> None:
    """平台侧的**形状校验**：绝对路径、不含 `..` 穿越。

    白名单（哪些目录可读）归 **Agent**——只有它知道本机允许的目录（AIOPS_LOG_DIRS）；
    平台瞎猜白名单会把客户自定目录误拒。但形状是通用契约，能在下发前拦住明显不对的输入，
    省掉一次白跑的任务与一次往返（实测：`/etc/shadow` 这类由 Agent 拒绝，`../../` 这类
    平台直接 400）。
    """
    p = (path or "").strip()
    if not p:
        return
    if not p.startswith("/"):
        raise HTTPException(400, "日志路径必须是绝对路径")
    if ".." in p.split("/"):
        raise HTTPException(400, "日志路径不得包含 `..` 穿越（平台侧拦截；白名单校验由目标机 Agent 执行）")


@router.post("/log-fetch")
async def create_log_fetch(body: LogFetchIn, request: Request, db=Depends(get_db),
                           user: User = Depends(require("diag"))):
    """下发一次日志取证（FR-409）。**以时间窗为主参数**，不提供"取 N 行"这种无信息量接口。"""
    _check_log_path_shape(body.path)
    host = (await db.execute(select(Host).where(Host.ident == body.ident))).scalar_one_or_none()
    if host is None:
        raise HTTPException(404, "主机不存在")
    payload = {"path": body.path, "unit": body.unit, "since": body.since, "until": body.until,
               "pattern": body.pattern, "level": body.level, "context": body.context}
    t = await enqueue(db, host, "log_fetch", payload, origin=body.ident, created_by=user.username)
    await audit(db=db, request=request, user=user, action="log_fetch_dispatch",
                resource=f"host:{body.ident}",
                detail={"task": t.id, "path": body.path, "unit": body.unit,
                        "window": [body.since, body.until]})
    return {"code": 0, "msg": "ok", "data": {"task_id": t.id,
                                             "note": "Agent 将在约 15 秒内领取并执行；"
                                                     "结果可从任务详情读取"}}


@router.get("/tasks")
async def list_tasks(ident: str = "", kind: str = "", limit: int = 20, db=Depends(get_db),
                     _: User = Depends(require("read"))):
    q = select(AgentTask).order_by(AgentTask.id.desc()).limit(max(1, min(limit, 100)))
    if ident:
        host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
        q = q.where(AgentTask.host_id == (host.id if host else -1))
    if kind:
        q = q.where(AgentTask.kind == kind)
    rows = (await db.execute(q)).scalars().all()
    hosts = {h.id: h for h in (await db.execute(select(Host))).scalars().all()}
    return {"code": 0, "msg": "ok", "data": {"items": [{
        "id": t.id, "kind": t.kind, "status": t.status, "origin": t.origin,
        "host": (hosts.get(t.host_id).name if hosts.get(t.host_id) else str(t.host_id)),
        "ident": (hosts.get(t.host_id).ident if hosts.get(t.host_id) else ""),
        "created_at": str(t.created_at), "done_at": str(t.done_at or ""),
        "payload": t.payload_json or {}, "result": t.result_json or {}, "error": t.error,
    } for t in rows]}}


@router.get("/tasks/recent")
async def recent_tasks(limit: int = Query(10, ge=1, le=50), db=Depends(get_db),
                       _: User = Depends(require("read"))):
    """最近任务（取证/自愈）—— 前端"最近取证"区块与故障报告复用。"""
    rows = (await db.execute(select(AgentTask).order_by(AgentTask.id.desc()).limit(limit))).scalars().all()
    hosts = {h.id: (h.name or h.ident) for h in (await db.execute(select(Host))).scalars().all()}
    return {"code": 0, "msg": "ok", "data": {"items": [{
        "id": t.id, "kind": t.kind, "status": t.status, "host": hosts.get(t.host_id, ""),
        "origin": t.origin, "created_at": str(t.created_at),
        "payload": t.payload_json or {}, "result_ok": bool((t.result_json or {}).get("ok")),
        "summary": _task_summary(t),
    } for t in rows]}}


@router.get("/tasks/{tid}")
async def task_detail(tid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    t = await db.get(AgentTask, tid)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return {"code": 0, "msg": "ok", "data": {
        "id": t.id, "kind": t.kind, "status": t.status, "payload": t.payload_json or {},
        "result": t.result_json or {}, "error": t.error, "origin": t.origin,
        "created_at": str(t.created_at), "picked_at": str(t.picked_at or ""),
        "done_at": str(t.done_at or ""),
    }}


def _task_summary(t: AgentTask) -> str:
    res = t.result_json or {}
    if t.status != "done":
        return t.error or t.status
    if t.kind == "log_fetch":
        return "取回 %d 段 / 折叠 %d / %d 字节" % (len(res.get("segments") or []),
                                                  res.get("deduped") or 0, res.get("bytes") or 0)
    return "动作 %s：%s" % ((t.payload_json or {}).get("action"), "成功" if res.get("ok") else "失败")
