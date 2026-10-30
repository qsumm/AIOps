"""Agent 上报与主机/指标查询路由（FR-B1/B5；05 §2/§3）。"""
import re
from datetime import datetime, timedelta

from fastapi import HTTPException, APIRouter, Depends, Header, Request, Query
from sqlalchemy import text

from app.core.security import require
from app.db.session import get_db
from app.models.collect import Probe
from app.models.host import Host
from app.models.shell import User
from app.services import ingest
from app.store import ring
from sqlalchemy import func, select

router = APIRouter(prefix="/api/ops", tags=["ops-collect"])


# ---------- Agent 协议（机器凭证，非用户 JWT）----------
def _peer_ip(request: Request | None) -> str:
    """Agent 上报的来源 IP：X-Forwarded-For 首段优先（反代场景），回落 TCP 对端。
    D120：agent 在 NAT/容器网段内时，本机探测拿不到公网 IP——平台看到的对端才是真实出口。"""
    if request is None:
        return ""
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return xff or ((request.client.host or "") if request.client else "")


@router.post("/agents/register")
async def register(payload: dict, request: Request, db=Depends(get_db)):
    data = await ingest.register_agent(db, payload, peer_ip=_peer_ip(request))
    return {"code": 0, "msg": "ok", "data": data}


class _RenameIn(dict):
    pass


@router.post("/hosts/{ident}/rename")
async def rename_host(ident: str, body: dict, request: Request, db=Depends(get_db),
                      user: User = Depends(require("write"))):
    """主机改名（D56）：install.sh 自动起名后，用户在『主机管理』里改成自己认得的名字。

    名字只存 Host.name，ident（机器指纹）不变——Agent 凭证与数据连续性不受影响。
    """
    from app.core.security import audit

    name = (body or {}).get("name", "").strip()
    if not name or len(name) > 64:
        raise HTTPException(422, "主机名需为 1~64 个字符")
    from sqlalchemy import select as _select
    host = (await db.execute(_select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if host is None:
        raise HTTPException(404, "主机不存在")
    old = host.name
    host.name = name
    await db.commit()
    await audit(db=db, request=request, user=user, action="host_rename",
                resource=f"host:{ident[:12]}")
    return {"code": 0, "msg": "ok", "data": {"ident": ident, "old": old, "name": name}}


@router.post("/hosts/{ident}/offline")
async def host_offline(ident: str, body: dict | None = None, request: Request = None,
                       db=Depends(get_db), user: User = Depends(require("write"))):
    """手动下线 / 恢复上线（主机管理）。下线置 status=down → 探活/告警/自愈按
    status=="up" 处理即自动跳过；Agent 上报被 ingest.auth_agent 拒。数据与历史保留。"""
    from app.core.security import audit

    offline = bool((body or {}).get("offline", True))
    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if host is None:
        raise HTTPException(404, "主机不存在")
    host.offline = offline
    host.status = "down" if offline else "up"
    await db.commit()
    await audit(db=db, request=request, user=user,
                action=f"host_{'offline' if offline else 'online'}",
                resource=f"host:{ident[:12]}")
    return {"code": 0, "msg": "ok", "data": {"ident": ident, "offline": host.offline,
                                             "removed": host.removed, "status": host.status}}


@router.post("/hosts/{ident}/remove")
async def host_remove(ident: str, body: dict | None = None, request: Request = None,
                      db=Depends(get_db), user: User = Depends(require("write"))):
    """移出 / 撤移出（主机管理）。移出含下线并从主机列表隐藏（退役），Agent 上报/重连被拒、
    需重新签发安装令牌才能复原；历史告警/事件/巡检保留（审计留痕，不做物理级联删除）。"""
    from app.core.security import audit

    removed = bool((body or {}).get("removed", True))
    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if host is None:
        raise HTTPException(404, "主机不存在")
    host.removed = removed
    # 移出 = 退役：必然下线；撤移出回到"下线待接入"态（agent 需重装/重注册才重新上线）
    host.offline = True
    host.status = "down"
    await db.commit()
    await audit(db=db, request=request, user=user,
                action=f"host_{'remove' if removed else 'unremove'}",
                resource=f"host:{ident[:12]}")
    return {"code": 0, "msg": "ok", "data": {"ident": ident, "offline": host.offline,
                                             "removed": host.removed, "status": host.status}}


@router.post("/agents/metrics", status_code=204)
async def metrics(
    request: Request,
    body: dict,
    db=Depends(get_db),
    x_ident: str = Header(default=""),
    x_token: str = Header(default=""),
):
    await ingest.ingest_metrics(db, x_ident, x_token, body, peer_ip=_peer_ip(request))


@router.get("/dev/engine-state")
async def engine_state(_: User = Depends(require("read"))):
    """引擎自检（只读）：对账心跳 / worker 清单（含 fires 数）/ 队列 / SSE 订阅数。

    用途：判断"规则改了没生效""告警不触发"这类问题到底是**对账没跑**还是**评估没命中**，
    不必翻日志猜。对账心跳超过 3 个周期即判定 unhealthy。
    """
    from app.engine import core

    return {"code": 0, "msg": "ok", "data": core.engine_state()}


@router.post("/agents/heartbeat")
async def agent_heartbeat(
    request: Request,
    body: dict = None,
    db=Depends(get_db),
    x_ident: str = Header(default=""),
    x_token: str = Header(default=""),
):
    """Agent 心跳（FR-B1/B5、05 §2）：只刷 last_beat，不上报数据；身份同 metrics。"""
    data = await ingest.heartbeat(db, x_ident, x_token, body or {}, peer_ip=_peer_ip(request))
    return {"code": 0, "msg": "ok", "data": data}


@router.get("/install-token")
async def install_token(_: User = Depends(require("write"))):
    """签发一次性 Agent 安装令牌（主机管理 → 接入新主机）。

    需要 write 权限；令牌单次有效（用后置 used）。D19 审计：原先唯一入口挂在 `/dev/mint-install-token`
    下、注释还写"正式入口在设置页"（并不存在）——交付版里出现 `/dev/` 前缀会被买家的安全评审
    直接当后门，故给出正式路径并保留旧路径做兼容别名。
    """
    return {"code": 0, "msg": "ok", "data": {"install_token": ingest.mint_install_token()}}


@router.get("/dev/mint-install-token", deprecated=True)  # 兼容别名（旧前端/脚本）
async def mint_install_token(_: User = Depends(require("write"))):
    return {"code": 0, "msg": "ok", "data": {"install_token": ingest.mint_install_token()}}


# ---------- 主机与指标（用户侧）----------
@router.get("/todo-counts")
async def todo_counts(db=Depends(get_db), _: User = Depends(require("read"))):
    """D113「待我处理」：大盘汇总卡的数据源。

    - pending_runbooks：含可执行步骤、manual 模式、尚未执行过的变更单
    - needs_human：最新诊断置信度 INCONCLUSIVE 且**尚无人工订正**的 open 事件
    - failed_tasks_today：今日（北京时间）失败的 Agent 任务
    """
    from app.models.event import RCAFinding
    from app.models.ops import ActionAudit, Runbook
    from app.models.svc import AgentTask
    from app.services.daytime import beijing_day_start_utc

    today = beijing_day_start_utc()

    rbs = (await db.execute(select(Runbook))).scalars().all()
    executed = set((await db.execute(select(ActionAudit.runbook_id).where(
        ActionAudit.status == "success"))).scalars().all())
    pending = 0
    for rb in rbs:
        steps = rb.steps or []
        if not any(isinstance(s, dict) and s.get("action") for s in steps):
            continue
        if (getattr(rb, "execution_mode", "manual") or "manual") == "auto":
            continue
        if rb.status in ("draft", "executing") and rb.id not in executed:
            pending += 1

    needs_human = 0
    incs = (await db.execute(text(
        "SELECT i.id FROM incidents i JOIN rca_findings f ON f.incident_id=i.id "
        "WHERE i.status='open' AND f.self_assessed_strength='inconclusive' "
        "AND f.id=(SELECT MAX(f2.id) FROM rca_findings f2 WHERE f2.incident_id=i.id)"
    ))).all()
    for (iid,) in incs:
        corrected = (await db.execute(text(
            "SELECT 1 FROM case_cards WHERE incident_id=:i AND tags LIKE '%human_corrected%' LIMIT 1"),
            {"i": iid})).first()
        if not corrected:
            needs_human += 1

    failed_tasks = (await db.execute(select(func.count()).select_from(AgentTask).where(
        AgentTask.status == "failed", AgentTask.done_at >= today))).scalar() or 0

    return {"code": 0, "msg": "ok", "data": {
        "pending_runbooks": pending, "needs_human": needs_human, "failed_tasks_today": failed_tasks}}


@router.get("/hosts")
async def list_hosts(status: str = "", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                   db=Depends(get_db), user: User = Depends(require("read"))):
    from app.services import visibility

    q = select(Host).order_by(Host.id)
    # 下线/移出筛选：`offline` = 已下线、`removed` = 已移出（退役）；其余按 status 匹配。
    # 默认不显示已移出主机（removed=False），除非显式筛 removed。
    if status == "offline":
        q = q.where(Host.offline.is_(True))
    elif status == "removed":
        q = q.where(Host.removed.is_(True))
    else:
        q = q.where(Host.removed.is_(False))
        if status:
            q = q.where(Host.status == status)
    cond = await visibility.visible_fingerprints_filter(db, user, Host.ident)
    if cond is not None:
        q = q.where(cond)
    rows = (await db.execute(q)).scalars().all()
    total = len(rows)                       # D128：全量取回后内存分页（主机量级小，胜在不动 SQL）
    rows = rows[(page - 1) * page_size: page * page_size]
    return {"code": 0, "msg": "ok", "data": {
        "items": [{"ident": h.ident, "name": h.name, "ip": h.ip, "os": h.os, "status": h.status,
                    "offline": bool(h.offline), "removed": bool(h.removed),
                    "last_beat": str(h.last_beat or ""), "agent_version": h.agent_version,
                    "auth_state": h.auth_state or "unknown",
                    "last_report_ok_at": str(h.last_report_ok_at or ""),
                    "cache_depth": h.cache_depth or 0,
                    "last_agent_error": h.last_agent_error or "",
                    "version_stale": ingest.is_agent_version_stale(h.agent_version, h.agent_code_sha256),
                    # D125：status=up 但 ring 里一个指标点都没有 = "在线但没在采集上报"——
                    # 最典型的静默失败（采集被跳过/配置错），列表需要能一眼看见。
                    "has_metrics": bool(ring.list_metric_names(h.ident)),
                    # D146：机器配置（几核几 G）——列表也能一眼看出规格
                    "cpu_cores": h.cpu_cores or 0,
                    "mem_total": h.mem_total or 0,
                    "latest_agent_version": ingest.AGENT_LATEST_VERSION} for h in rows],
        "total": total,
    }}


@router.get("/hosts/{ident}")
async def host_detail(ident: str, db=Depends(get_db), user: User = Depends(require("read"))):
    from app.services import visibility

    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if host is not None and not await visibility.host_visible(db, user, ident):
        host = None
    if host is None:
        raise HTTPException(404, "资源不存在或已被删除")
    probes = (await db.execute(select(Probe).where(Probe.host_id == host.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {
        **{k: getattr(host, k) for k in ("ident", "name", "ip", "os", "status", "offline", "removed", "agent_version")},
        # D146：机器硬件配置（几核几 G）——Agent 上报的 hostinfo 落库后的读出
        "cpu_cores": host.cpu_cores or 0,
        "cpu_cores_physical": host.cpu_cores_physical or 0,
        "cpu_model": host.cpu_model or "",
        "mem_total": host.mem_total or 0,
        "boot_time": str(host.boot_time or ""),
        "hostinfo_at": str(host.hostinfo_at or ""),
        "last_beat": str(host.last_beat or ""),
        "auth_state": host.auth_state or "unknown",
        "last_auth_ok_at": str(host.last_auth_ok_at or ""),
        "last_report_ok_at": str(host.last_report_ok_at or ""),
        "cache_depth": host.cache_depth or 0,
        "last_agent_error": host.last_agent_error or "",
        "version_stale": ingest.is_agent_version_stale(host.agent_version, host.agent_code_sha256),
        "latest_agent_version": ingest.AGENT_LATEST_VERSION,
        # D57：补 target——探活详情的「目标」列此前永远是空（序列化漏了字段）
        "probes": [{"name": p.name, "type": p.type, "target": p.target, "last_status": p.last_status, "last_latency_ms": p.last_latency_ms} for p in probes],
        "services": ring.get_services(ident),   # 端口与进程发现（Agent 随心跳上报）
        "disks": ring.get_disks(ident),         # 每个挂载点 df -h 明细（Agent 随心跳上报）
    }}


@router.get("/metrics/list")
async def metrics_list(ident: str = "", prefix: str = "", db=Depends(get_db), _: User = Depends(require("read"))):
    """该主机已有数据点的指标名（D118：前端多系列图「按前缀发现系列」用）。
    ring（最近 2h 明细）∪ metric_agg（历史聚合）去重；空返回 = 老 agent 未采集。"""
    names = set(ring.list_metric_names(ident, prefix))
    try:
        rows = (await db.execute(text(
            "SELECT DISTINCT metric FROM metric_agg WHERE ident = :i AND metric LIKE :p LIMIT 500"),
            {"i": ident, "p": f"{prefix}%"})).all()
        names |= {r[0] for r in rows}
    except Exception:  # noqa: BLE001 —— 聚合表查不到不影响 ring 侧发现
        pass
    return {"code": 0, "msg": "ok", "data": {"names": sorted(names)}}


# ---------- 健康诊断（D119，FR-C；规则诊断树） ----------
# 设计取舍：规则轨全部读本地数据（ring/agg/快照/DB），整体 <2s → POST 同步返回完整报告，
# 不走 SSE 事件流（相比 AI ReAct 诊断的分钟级时长，事件流是过度设计）。

@router.post("/hosts/{ident}/diagnose/health")
async def health_diagnose(ident: str, request: Request, db=Depends(get_db), user: User = Depends(require("diag"))):
    """一键健康诊断（FR-C1/C2）：跑规则诊断树，落库并返回完整报告。admin/member 可用。"""
    from app.core.security import audit
    from app.models.health import HealthReport
    from app.services import health_check

    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if host is None:
        raise HTTPException(404, "主机不存在或已被删除")
    running = (await db.execute(select(HealthReport).where(
        HealthReport.host_ident == ident, HealthReport.status == "running"))).scalar_one_or_none()
    if running is not None:
        raise HTTPException(409, "该主机已有诊断在进行中，请稍候")
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 —— 空 body 允许
        body = {}
    minutes = max(5, min(int((body or {}).get("minutes") or 60), 1440))
    data = await health_check.diagnose_host(db, host.id, ident, minutes=minutes)
    await audit(db=db, request=request, user=user, action="host_health_diagnose",
                resource=f"host:{ident[:12]}")
    return {"code": 0, "msg": "ok", "data": data}


@router.get("/hosts/{ident}/diagnose/health/reports")
async def health_reports(ident: str, db=Depends(get_db), _: User = Depends(require("read"))):
    """历史诊断报告列表（FR-C3，对齐阿里云「历史诊断」）。"""
    from app.models.health import HealthReport
    rows = (await db.execute(select(HealthReport).where(HealthReport.host_ident == ident)
                             .order_by(HealthReport.id.desc()).limit(20))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [{
        "id": r.id, "started_at": str(r.started_at or ""), "finished_at": str(r.finished_at or ""),
        "status": r.status, "source": r.source, "summary": r.summary or {}} for r in rows]}}


@router.get("/hosts/{ident}/diagnose/health/reports/{rid}")
async def health_report_detail(ident: str, rid: int, db=Depends(get_db), _: User = Depends(require("read"))):
    """报告详情：结论 + 分类检查项 + 证据 + 建议（诊断时间窗内可追溯）。"""
    from app.models.health import HealthReport
    r = (await db.execute(select(HealthReport).where(
        HealthReport.id == rid, HealthReport.host_ident == ident))).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "报告不存在")
    return {"code": 0, "msg": "ok", "data": {
        "id": r.id, "status": r.status, "source": r.source, "summary": r.summary or {},
        "items": r.items or [], "window_start": str(r.window_start or ""),
        "window_end": str(r.window_end or ""), "started_at": str(r.started_at or ""),
        "finished_at": str(r.finished_at or "")}}


@router.post("/hosts/{ident}/upgrade")
async def host_upgrade(ident: str, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    """平台侧一键升级 Agent（D121，方案 A）：下发 self_upgrade 任务走既有 AgentTask 拉模式。

    agent 下个心跳（≤15s）领取 → 下载平台 serving 的 agent_main.py → SHA256 校验
    → 原子替换 → 自触发服务重启。限流 2 次/时（防误点），全程审计。"""
    from app.core.security import audit
    from app.models.svc import AgentTask
    from app.routers.agent_install import agent_code_sha256

    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if host is None:
        raise HTTPException(404, "主机不存在或已被删除")
    if host.status != "up":
        raise HTTPException(409, "主机当前离线（Agent 不在拉取任务），请等恢复在线后再升级")
    sha = agent_code_sha256()
    if not sha:
        raise HTTPException(503, "AGENT_ARTIFACT_MISSING：平台未找到 agent_main.py")
    if host.agent_code_sha256 and host.agent_code_sha256 == sha:
        return {"code": 0, "msg": "ok", "data": {"note": "该主机的代码指纹与平台一致，无需升级"}}
    used = (await db.execute(select(func.count()).select_from(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.kind == "self_upgrade",
        AgentTask.created_at >= datetime.utcnow() - timedelta(hours=1)))).scalar() or 0
    if used >= 2:
        raise HTTPException(429, "该主机本小时升级任务已达上限（2 次/时）——防误点与保护目标机")
    already = (await db.execute(select(AgentTask).where(
        AgentTask.host_id == host.id, AgentTask.kind == "self_upgrade",
        AgentTask.status == "pending"))).scalars().first()
    if already is not None:
        return {"code": 0, "msg": "ok", "data": {"task_id": already.id, "note": "已有升级任务待执行，agent 下个心跳领取"}}
    # D125 兼容层：目标机若还是**旧版 agent**（只会按版本号比较），同版本号的新内容会被它
    # 判成"已是最新"而拒绝自我更新 → 死锁。故指纹不一致时，把下发的 version 抬到
    # **高于目标机当前版本**，让旧 agent 也有逃生通道；新版 agent 则按文件指纹精确判定。
    tgt_version = ingest.AGENT_LATEST_VERSION
    cur_v = tuple(int(x) for x in str(host.agent_version or "0.0.0").split(".") if x.isdigit())
    tgt_v = tuple(int(x) for x in str(tgt_version).split(".") if x.isdigit())
    if cur_v and tgt_v <= cur_v:
        tgt_version = ".".join(str(x) for x in (cur_v[0], cur_v[1] if len(cur_v) > 1 else 0, cur_v[2] + 1 if len(cur_v) > 2 else 1))
    now = datetime.utcnow()
    t = AgentTask(host_id=host.id, kind="self_upgrade",
                  payload_json={"version": tgt_version, "sha256": sha},
                  origin=f"ui:{user.username}", created_by=user.username,
                  expires_at=now + timedelta(minutes=30))
    db.add(t)
    await db.commit()
    await db.refresh(t)
    await audit(db=db, request=request, user=user, action="host_agent_upgrade",
                resource=f"host:{ident[:12]}", detail={"task_id": t.id, "sha256": sha[:12]})
    return {"code": 0, "msg": "ok", "data": {"task_id": t.id,
             "note": "升级任务已下发——agent 下个心跳（≤15s）领取并自动升级，完成后本页版本自动更新"}}


@router.get("/hosts/{ident}/alerts/markers")
async def alert_markers(ident: str, minutes: int = 1440, db=Depends(get_db), _: User = Depends(require("read"))):
    """该主机时间窗内的告警时间点（D119/M4.2，FR-D2 故障追溯）：详情页图表上画标记。"""
    from app.models.alert import AlertCurEvent, AlertHisEvent
    end = datetime.utcnow()
    start = end - timedelta(minutes=max(5, min(int(minutes or 1440), 43200)))
    out = []
    cur = (await db.execute(select(AlertCurEvent).where(
        AlertCurEvent.host_ident == ident, AlertCurEvent.first_trigger_time >= start))).scalars().all()
    for a in cur:
        out.append({"t": str(a.first_trigger_time or ""), "label": a.rule_name, "severity": a.severity})
    his = (await db.execute(select(AlertHisEvent).where(
        AlertHisEvent.host_ident == ident, AlertHisEvent.trigger_time >= start))).scalars().all()
    for a in his:
        out.append({"t": str(a.trigger_time or ""), "label": a.rule_name, "severity": a.severity})
    out.sort(key=lambda x: x["t"])
    return {"code": 0, "msg": "ok", "data": {"markers": out[:200]}}


@router.get("/metrics/query")
async def query_metrics(
    ident: str = "",
    metric: str = "",
    minutes: int = 60,
    source: str = "",
    datasource_id: int = 0,
    query: str = "",
    step_s: int = 15,
    db=Depends(get_db),
    _: User = Depends(require("read")),
):
    """明细窗口走内存环形；超窗走 metric_agg 聚合（FR-B4 验收）。

    source=prom（FR-B3 轨 B）：转发 Prometheus query_range，结果不写 rings（03 §1.6）。
    """
    if source == "prom":
        from app.models.prom import PromDatasource
        from app.services import prom as prom_svc

        ds = await db.get(PromDatasource, datasource_id) if datasource_id else (
            await db.execute(select(PromDatasource).where(PromDatasource.enabled.is_(True)))).scalars().first()
        if ds is None:
            raise HTTPException(404, "未找到该 Prometheus 数据源")
        end = datetime.utcnow()
        try:
            body = await prom_svc.query_range_raw(ds, query or metric, end - timedelta(minutes=minutes), end, step_s)
        except prom_svc.PromUnreachable as e:
            return {"code": 503, "msg": f"PROM_UNREACHABLE: {e}", "data": None}
        return {"code": 0, "msg": "ok", "data": {"source": "prom", "result": body}}
    end = datetime.utcnow()
    start = end - timedelta(minutes=minutes)

    async def _agg(s: datetime, e: datetime) -> list[tuple[datetime, float]]:
        rows = (await db.execute(
            text("SELECT ts, value_avg FROM metric_agg WHERE ident=:i AND metric=:m AND ts BETWEEN :s AND :e ORDER BY ts"),
            {"i": ident, "m": metric, "s": s, "e": e},
        )).all()
        return [(r[0], float(r[1])) for r in rows]

    pts = ring.query_points(ident, metric, start, end)
    src = "ring"
    if pts:
        # 夯实轮修（D15 审计发现的真缺陷）：ring 只覆盖**最近 maxlen 个点**（默认 2h），
        # 原实现"ring 有数据就整段返回"→ 查长窗口时**前段数据被静默丢掉**
        # （实测：查 1 天窗口只返回最近 13 分钟的 47 个点，且不查聚合表）。
        # 手册承诺的"超窗自动落 metric_agg"因此不成立。修：ring 覆盖最近一段，
        # 更早的部分从聚合表补齐并拼接（source 标为 ring+metric_agg）。
        # ring 覆盖到窗口起点之前 → 窗口内数据完整，不查聚合表（避免粒度混合）
        # 否则（ring 被 maxlen 挤过 / 进程刚重启 ring 为空）→ 前段从聚合表补
        if not ring.has_points_before(ident, metric, start):
            older = await _agg(start, pts[0][0])
            if older:
                pts = older + pts
                src = "ring+metric_agg"
    else:
        pts = await _agg(start, end)
        src = "metric_agg"
    return {"code": 0, "msg": "ok", "data": {
        "points": [[ts.isoformat(), round(v, 4)] for ts, v in pts], "source": src,
    }}


@router.get("/dashboard/overview")
async def overview(range: str = "1h", db=Depends(get_db), user: User = Depends(require("read"))):
    """大盘总览（FR-F ops-dashboard 必备要素：健康度/告警计数/核心指标图/活跃告警/巡检位）。

    统计口径随可见域走（demo + DEMO_DATA_PREFIXES 配置时只统计演示主机）。
    range 支持 `1h/6h/24h/7d/30d` 或任意 `12h/90d` —— 1h 走内存 ring（1m 桶），
    其余窗口走 metric_agg（5m 桶聚类）。
    """
    from app.models.alert import AlertCurEvent, AlertHisEvent
    from app.models.event import Incident
    from app.services import visibility

    _m = re.fullmatch(r"(\d+)([hd])", range or "")
    if not _m:
        range = "1h"
        window_min = 60
    else:
        n, unit = int(_m.group(1)), _m.group(2)
        window_min = n * 60 if unit == "h" else n * 1440
        range = f"{n}{unit}"

    vcond = await visibility.visible_fingerprints_filter(db, user, Host.ident)
    # D180：已移出（removed=1）主机退役，退出大盘全部聚合——健康率分母/宕机名单/
    # 磁盘榜/资源 Top5 同源于这一次查询，过滤必须挂在查询上而不是逐处补。
    rcond = Host.removed == False  # noqa: E712
    hq = select(Host).where(rcond) if vcond is None else select(Host).where(vcond, rcond)
    hosts = (await db.execute(hq)).scalars().all()
    up = sum(1 for h in hosts if h.status == "up")
    idents = [h.ident for h in hosts]
    # 磁盘分区明细（df -h 式）：Agent 上报 `disks` 快照（每个挂载点），
    # 从 ring 取各 up 主机最近一份；老 agent 未上报时回退根分区 disk.percent。
    disks: list[dict] = []
    if idents:
        _now = datetime.utcnow()
        for h in hosts:
            if h.status != "up":
                continue
            parts = ring.get_disks(h.ident)
            if parts:
                disks.append({"host": h.name or h.ident[:8], "ident": h.ident, "partitions": parts})
                continue
            latest = ring.query_points(h.ident, "disk.percent", _now - timedelta(minutes=5), _now)
            if latest:
                disks.append({"host": h.name or h.ident[:8], "ident": h.ident,
                              "partitions": [{"device": "", "fstype": "", "size": 0, "used": 0,
                                              "free": 0, "percent": round(latest[-1][1], 1), "mount": "/"}]})

    # 资源 Top5（D119/M4.1，FR-D1）：内存 / 磁盘 / 网络吞吐排行（大盘「资源 Top5」卡）。
    def _latest(ident: str, metric: str):
        pts = ring.query_points(ident, metric, _now - timedelta(minutes=5), _now)
        return pts[-1][1] if pts else None

    top5: dict = {"mem": [], "disk": [], "net": []}
    for h in hosts:
        if h.status != "up":
            continue
        label = h.name or h.ident[:8]
        mv = _latest(h.ident, "mem.percent")
        if mv is not None:
            top5["mem"].append({"ident": h.ident, "name": label, "value": round(mv, 1)})
        dv = max([_latest(h.ident, m) or 0 for m in ring.list_metric_names(h.ident, "disk.")
                  if not m.startswith("disk.io.") and not m.startswith("disk.inode.")], default=None)
        if dv:
            top5["disk"].append({"ident": h.ident, "name": label, "value": round(dv, 1)})
        nv = sum(_latest(h.ident, m) or 0 for m in ring.list_metric_names(h.ident, "net.")
                 if m.endswith(".in_bps") or m.endswith(".out_bps"))
        if nv > 0:
            top5["net"].append({"ident": h.ident, "name": label, "value": round(nv, 1)})
    for k in top5:
        top5[k].sort(key=lambda x: -x["value"])
        top5[k] = top5[k][:5]
    vcond_a = await visibility.visible_fingerprints_filter(db, user, AlertCurEvent.host_ident)

    # 告警计数对**全量**活跃告警统计（此前 limit(20) 截断后再分桶，>20 条时卡片计数偏小）
    # P0：陈旧告警不进入“活跃告警”统计/列表（它们仍可在告警中心查看，带陈旧标记）
    cnt_q = (select(AlertCurEvent.severity, func.count()).group_by(AlertCurEvent.severity)
             .where(AlertCurEvent.stale.is_(False)))
    list_q = (select(AlertCurEvent).where(AlertCurEvent.stale.is_(False))
              .order_by(AlertCurEvent.severity, AlertCurEvent.trigger_time.desc()).limit(20))
    if vcond_a is not None:
        cnt_q = cnt_q.where(vcond_a)
        list_q = list_q.where(vcond_a)
    by_sev = {str(sev): n for sev, n in (await db.execute(cnt_q)).all()}
    alerts_total = sum(by_sev.values())
    actives = (await db.execute(list_q)).scalars().all()

    # 24h 事件数 + 逐小时直方图（大盘 SLA 走势小柱图用）。
    # 必须随可见域（D26 前漏套过滤，demo 能看到全平台事件数）。
    # 计数直接由直方图行派生（单一数据源）：两条独立查询在共库场景下会被
    # 并发写方（如运行中的平台清理任务）插入时间差，出现 count=1 / hist=0 的
    # 瞬时不一致（D26 测试实测踩到）。
    vcond_h = await visibility.visible_fingerprints_filter(db, user, AlertHisEvent.host_ident)
    since24 = datetime.utcnow() - timedelta(hours=24)
    hist_q = select(AlertHisEvent.trigger_time).where(AlertHisEvent.trigger_time >= since24)
    if vcond_h is not None:
        hist_q = hist_q.where(vcond_h)
    hist_rows = (await db.execute(hist_q)).scalars().all()
    buckets = [0] * 24
    now = datetime.utcnow()
    for ts in hist_rows:
        hours_ago = int((now - ts).total_seconds() // 3600)
        # 时钟偏差/并发写入可能产生 ts 晚于 now（hours_ago<0）→ 归入当前小时，不丢计数
        if hours_ago < 0:
            hours_ago = 0
        if hours_ago < 24:
            buckets[23 - hours_ago] += 1
    # 计数与直方图**完全同源**（= 直方图之和）：两条独立统计在共库并发下会出现
    # 瞬时不一致（D26/D30 测试实测 count=1 而 hist=0），同源后不可能再漂移。
    events_24h = sum(buckets)

    now = datetime.utcnow()
    if range == "1h":
        series = ring.cluster_series(["cpu.percent", "mem.percent"], now - timedelta(minutes=60), now, bucket_s=60)
    else:
        # 超窗：内存 ring 只有 ~2h，走 metric_agg（1m 粒度）按 5m 桶聚类均值，限定可见主机
        series = []
        if idents:
            # seekdb 的 SQL 层对 `granularity='1m' AND ident IN (…)` 组合会报
            # 1210 Invalid argument（等值/绑定参数/时间 BETWEEN 各变体逐一实测排除，
            # 换 LIKE 后通过），故此处用 LIKE 写法，且时间窗过滤取回后在 Python 侧完成；
            # 24h×可见主机×2 指标的 1m 行数量级在万级，开销可接受。
            in_list = ",".join("'" + i.replace("'", "''") + "'" for i in idents)
            agg_sql = text(
                "SELECT ident, metric, ts, value_avg FROM metric_agg "
                f"WHERE granularity LIKE '1m' AND ident IN ({in_list}) ORDER BY ts"
            )
            rows = (await db.execute(agg_sql)).all()
            start = now - timedelta(minutes=window_min)
            for metric in ("cpu.percent", "mem.percent"):
                # ⚠ 变量名不能叫 buckets：上面直方图已用该名（D45 实测——重名导致
                #   events_by_hour 在 6h/24h 时被覆盖成 dict，前端 .map() 抛错、大盘半屏卡住）
                agg_buckets: dict[datetime, list[float]] = {}
                for ident, m, ts, v in rows:
                    if m != metric or not (start <= ts <= now):
                        continue
                    b = datetime.utcfromtimestamp(int(ts.timestamp() // 300) * 300)
                    agg_buckets.setdefault(b, []).append(float(v))
                if agg_buckets:
                    series.append({"name": metric, "points": [
                        [b.isoformat(), round(sum(vs) / len(vs), 4)] for b, vs in sorted(agg_buckets.items())]})

    # 接近阈值的主机（D41，替代原「CPU Top5 瞬时快照」）。
    # 原实现的价值问题：取最近一个采样点的瞬时值 + 前端硬编码 70/90 着色 ——
    # ① 瞬时值无判断价值（CPU 波动）② 阈值与用户配的告警规则**不同源**，
    # 会出现"卡片绿着但已经告警"的误导 ③ 不能下钻定位。
    # 改为：对每条 **enabled 的 metric 规则** × 每台主机，算当前值与阈值的接近度，
    # 按「已触发 → 接近阈值」排序 —— 直接回答值班运维的"谁快出问题了"。
    from app.engine.detect import _parse_expr
    from app.models.alert import AlertRule
    rule_rows = (await db.execute(
        select(AlertRule).where(AlertRule.scope == "metric", AlertRule.disabled.is_(False))
    )).scalars().all()
    name_by_ident = {h.ident: (h.name or h.ident[:8]) for h in hosts}
    watch: list[dict] = []
    for r in rule_rows:
        # 只有「阈值」算法才有可比的固定阈值；3σ / 孤立森林按统计判定，
        # expr 只是占位（常见写法 `>0`），拿它算"逼近度"毫无意义 → 跳过。
        if (r.algorithm or "threshold") != "threshold":
            continue
        try:
            op, thr = _parse_expr(r.expr)
        except Exception:  # noqa: BLE001 —— 表达式非法（历史数据）跳过，不拖垮大盘
            continue
        if thr == 0:   # 阈值为 0 时"逼近度"无定义，只用"是否触发"判断
            thr = 0.0
        for ident in idents:
            pts = ring.query_points(ident, r.metric, now - timedelta(minutes=5), now)
            if not pts:
                continue
            v = pts[-1][1]
            if op in (">", ">="):
                firing = v >= thr if op == ">=" else v > thr
                ratio = (v / thr) if thr else (1.0 if firing else 0.0)
            else:  # "<" / "<="：值越小越糟，逼近度取反向比
                firing = v <= thr if op == "<=" else v < thr
                ratio = (thr / v) if v else 999.0
            # 只关心"已触发"或"逼近阈值（≥80%）"的，避免把一堆健康主机塞进榜
            if firing or ratio >= 0.8:
                watch.append({
                    "ident": ident, "name": name_by_ident.get(ident, ident[:8]),
                    "metric": r.metric, "rule_name": r.name, "severity": r.severity,
                    "value": round(v, 1), "threshold": thr, "op": op,
                    "firing": bool(firing), "ratio": round(ratio, 3),
                })
    # 已触发的排前，其余按逼近度降序
    watch.sort(key=lambda x: (not x["firing"], -x["ratio"]))

    # 未闭环问题（open/ack），随可见域
    inc_q = select(func.count()).select_from(Incident).where(Incident.status.in_(("open", "ack")))
    vcond_i = await visibility.visible_fingerprints_filter(db, user, Incident.host_ident)
    if vcond_i is not None:
        inc_q = inc_q.where(vcond_i)
    incidents_open = (await db.execute(inc_q)).scalar() or 0

    # 近 7 天平均恢复时长（分钟）；无已恢复样本时返回 null（前端显示"—"）
    since7 = datetime.utcnow() - timedelta(days=7)
    mttr_q = select(AlertHisEvent.trigger_time, AlertHisEvent.recover_time).where(
        AlertHisEvent.is_recovered.is_(True), AlertHisEvent.recover_time.is_not(None),
        AlertHisEvent.trigger_time >= since7)
    if vcond_h is not None:
        mttr_q = mttr_q.where(vcond_h)
    mttr_rows = (await db.execute(mttr_q)).all()
    mttr_minutes = None if not mttr_rows else round(
        sum((r[1] - r[0]).total_seconds() for r in mttr_rows) / len(mttr_rows) / 60, 1)

    # 巡检状态（FR-D5 上线后接线；D7 前这里还是占位）
    from app.models.ops import Inspection

    insp = (await db.execute(select(Inspection).where(Inspection.enabled.is_(True)).order_by(Inspection.id))).scalars().first()
    inspection = None if insp is None else {
        "name": insp.name, "last_run_at": str(insp.last_run_at or ""), "status": insp.last_status,
        "anomalies": len(insp.findings or []), "incidents_created": insp.incidents_created,
        "top_findings": [
            {"host": f.get("host", ""), "item": f.get("item", ""), "label": f.get("label", "")}
            for f in (insp.findings or [])[:3]
        ],
    }

    down_hosts = [h.ident for h in hosts if h.status != "up"]
    return {"code": 0, "msg": "ok", "data": {
        "hosts": {"total": len(hosts), "up": up, "down": len(hosts) - up, "down_hosts": down_hosts},
        "alerts": {"total": alerts_total, "by_severity": by_sev},
        "events_24h": events_24h,
        "events_by_hour": buckets,
        "series": series,
        "disks": disks,
        "top5": top5,
        "range": range,
        "inspection": inspection,
        "watch_hosts": watch[:6],
        "incidents_open": incidents_open,
        "mttr_minutes": mttr_minutes,
        "active_alerts": [{
            "hash": a.hash, "rule_id": a.rule_id, "rule_name": a.rule_name, "host_ident": a.host_ident,
            "metric": a.metric, "severity": a.severity, "status": a.status, "trigger_value": a.trigger_value,
            "trigger_time": str(a.trigger_time), "ai_summary": a.ai_summary,
        } for a in actives],
        "ring": ring.ring_stats(),
    }}
