# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""诊断 Agent 的只读工具集（FR-D2；03 §1.13）。

工具契约（对齐 Holmes 的 StructuredToolResult 思路）：
  {"status": "SUCCESS"|"NO_DATA"|"ERROR", "data": ..., "note": "...", "elapsed_ms": n}

只读纪律：全部工具不产生任何写入/变更；不可读取时返回 NO_DATA 并说明原因，
**不允许编造**——LLM 拿不到数据就必须说"没有数据"。
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import subprocess
import time
from datetime import datetime, timedelta

_td = timedelta  # D119 工具内使用的别名

from fastapi import HTTPException
from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.models.collect import Probe
from app.models.host import Host
from app.models.svc import AgentTask
from app.store import ring

# 工具输出落盘目录（超出 token 预算时 spill，Holmes §2.5）
SPILL_DIR = "/tmp/aiops_tool_cache"


def _ok(data, note: str = "", t0: float | None = None) -> dict:
    return {"status": "SUCCESS", "data": data, "note": note,
            "elapsed_ms": int((time.time() - t0) * 1000) if t0 else 0}


def _no_data(note: str, t0: float | None = None) -> dict:
    return {"status": "NO_DATA", "data": None, "note": note,
            "elapsed_ms": int((time.time() - t0) * 1000) if t0 else 0}


def _err(note: str, t0: float | None = None) -> dict:
    return {"status": "ERROR", "data": None, "note": note,
            "elapsed_ms": int((time.time() - t0) * 1000) if t0 else 0}


async def _host_by_ident(db, ident: str) -> Host | None:
    """按 ident 或主机名解析主机（D8 修正）。

    为什么：ident 是 Agent 指纹哈希（sha256 前 32 位），人/AI 说的是主机名
    （`mock-web-01`、`demo-db-01`）。实测：AI 助手答"现在有哪些主机"后，用户追问
    「mock-db-01 的 CPU」→ 工具按 ident 查 → 返回"主机未注册"，看起来像平台坏了，
    实际只是标识口径不一致。这里统一解析：ident 精确 → name 精确（忽略大小写）
    → name 唯一前缀匹配 → ident 前缀匹配。
    """
    ident = (ident or "").strip()
    if not ident:
        return None
    rows = (await db.execute(select(Host))).scalars().all()
    for h in rows:
        if h.ident == ident:
            return h
    low = ident.lower()
    for h in rows:
        if (h.name or "").lower() == low:
            return h
    cand = [h for h in rows if (h.name or "").lower().startswith(low) or h.ident.startswith(ident)]
    if len(cand) == 1:
        return cand[0]
    return None


def _is_platform_host(host: Host) -> bool:
    """平台自身主机：Agent 上报时用 hostname 标识；此处以本机 hostname/ip 匹配。"""
    name = (host.name or "").lower()
    ip = host.ip or ""
    return name in (platform.node().lower(), "localhost") or ip in ("127.0.0.1", "::1")


# ---------------- 1) query_metrics ----------------
async def query_metrics(ident: str, metric: str, minutes: int = 60, tags: dict | None = None) -> dict:
    """查指标窗口（内存环形优先，超窗走 1m 聚合）。

    D154：可选 `tags` 维度（如 {"server":"db01"}）——带维度时查**带维度序列**
    （多实例的中间件指标各算各的）；不传则老行为不变。
    """
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
        end = datetime.utcnow()
        start = end - timedelta(minutes=max(1, min(int(minutes), 24 * 60)))
        if tags:
            # 带维度：查该维度组合的序列（ring 侧），再回落 metric_series 落库表
            spts = ring.query_series(host.ident, metric, tags, start, end)
            pts = spts[0][1] if spts else []
            source = "ring_series"
            if not pts:
                from sqlalchemy import text as _text
                from app.store.ring import tags_key as _tk
                rows = (await db.execute(
                    _text("SELECT ts, value FROM metric_series WHERE ident=:i AND metric=:m "
                          "AND tags_key=:k AND ts BETWEEN :s AND :e ORDER BY ts"),
                    {"i": host.ident, "m": metric, "k": _tk(tags), "s": start, "e": end})).all()
                pts = [(r[0], float(r[1])) for r in rows]
                source = "metric_series"
        else:
            pts = ring.query_points(host.ident, metric, start, end)
            source = "ring"
        if not pts:
            rows = (await db.execute(
                text("SELECT ts, value_avg FROM metric_agg WHERE ident=:i AND metric=:m AND ts BETWEEN :s AND :e ORDER BY ts"),
                {"i": host.ident, "m": metric, "s": start, "e": end},
            )).all()
            pts = [(r[0], float(r[1])) for r in rows]
            source = "metric_agg"
    if not pts:
        return _no_data(f"{host.name or host.ident} 的 {metric} 在最近 {minutes} 分钟无数据（可能未采集该指标）", t0)
    vals = [v for _, v in pts]
    return _ok({
        "metric": metric, "host": host.ident, "host_name": host.name, "source": source, "points": len(vals),
        "first": str(pts[0][0]), "last": str(pts[-1][0]),
        "min": round(min(vals), 3), "max": round(max(vals), 3),
        "avg": round(sum(vals) / len(vals), 3), "latest": round(vals[-1], 3),
        "series_tail": [[str(ts), round(v, 3)] for ts, v in pts[-30:]],
    }, t0=t0)


# ---------------- 2) check_probe ----------------
async def check_probe(ident: str, name: str = "") -> dict:
    """查服务探活状态/延迟/证书剩余天数（含最近状态）。"""
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
        q = select(Probe).where(Probe.host_id == host.id)
        if name:
            q = q.where(Probe.name == name)
        rows = (await db.execute(q)).scalars().all()
    if not rows:
        return _no_data(f"{host.name or host.ident} 无{'该名称的' if name else ''}探活配置", t0)
    return _ok([{
        "name": p.name, "type": p.type, "target": p.target, "status": p.last_status,
        "value": p.last_latency_ms if p.type == "https-cert" else f"{p.last_latency_ms}ms",
        "value_meaning": "证书剩余天数" if p.type == "https-cert" else "探活延迟",
        "enabled": p.enabled,
    } for p in rows], t0=t0)


# ---------------- 3) read_log_tail ----------------
async def read_log_tail(ident: str, path: str, lines: int = 200) -> dict:
    """读取日志尾部。

    诚实边界：V1 的 Agent 不上传日志，**只有平台自身主机**可读本地文件；
    远程主机返回 NO_DATA（不编造日志）。远程日志能力列入 D5/D6（Agent 侧新增只读通道）。
    """
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
    if host is None:
        return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
    if not _is_platform_host(host):
        return _no_data(
            f"{host.name or host.ident} 为远程主机：V1 Agent 不上传日志，无法读取 {path}。"
            "可用 query_metrics / check_probe / host_info 取证，或请运维在目标机手工执行。", t0)
    safe = os.path.realpath(path)
    if not os.path.isfile(safe):
        return _no_data(f"平台主机上文件不存在：{path}", t0)
    if not os.access(safe, os.R_OK):
        return _err(f"平台主机上文件不可读（权限不足）：{path}", t0)
    try:
        out = subprocess.run(["tail", "-n", str(max(1, min(int(lines), 2000))), safe],
                             capture_output=True, text=True, timeout=10)
        return _ok({"path": path, "lines": out.stdout[-40000:]}, t0=t0)
    except Exception as e:  # noqa: BLE001
        return _err(f"读取失败：{type(e).__name__}: {e}", t0)


# ---------------- 4) get_process_list ----------------
async def get_process_list(ident: str, top_n: int = 15) -> dict:
    """取占用最高的进程清单。

    平台自身主机：实时 ps。远程主机：用 Agent 最近一次服务发现里的 Top 进程
    （约 1 分钟一次，只含 CPU Top10；不再直接返回“V1 不上报进程”）。
    """
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
    if host is None:
        return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
    if not _is_platform_host(host):
        from app.store import ring

        svc = ring.get_services(ident)
        procs = (svc.get("processes") or [])[:max(1, min(top_n, 50))]
        if procs:
            return _ok([{"name": p.get("name"), "cpu_pct": p.get("cpu")} for p in procs],
                       note=f"来自 Agent 最近一次服务发现（{svc.get('at') or '时间未知'}）；"
                            "只含 CPU Top，非完整 ps，若需完整进程请用远程命令或让 Agent 升级", t0=t0)
        return _no_data(f"{host.name or host.ident} 暂无进程快照——Agent 约每分钟上报一次，"
                        "请等 1 分钟重试；若长期为空，检查 Agent 是否在运行", t0)
    try:
        out = subprocess.run(["ps", "-Ao", "pid,pcpu,pmem,rss,comm", "-r"],
                             capture_output=True, text=True, timeout=10)
        rows = [l.split(None, 4) for l in out.stdout.strip().splitlines()[1:][:max(1, min(top_n, 50))]]
        return _ok([{"pid": r[0], "cpu_pct": r[1], "mem_pct": r[2], "rss_kb": r[3], "cmd": r[4] if len(r) > 4 else ""}
                    for r in rows if len(r) >= 4], t0=t0)
    except Exception as e:  # noqa: BLE001
        return _err(f"取进程失败：{type(e).__name__}: {e}", t0)


# ---------------- 5) host_info ----------------
async def host_info(ident: str) -> dict:
    """主机静态信息 + 活跃告警数 + 探活汇总。"""
    t0 = time.time()
    from app.models.alert import AlertCurEvent

    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
        probes = (await db.execute(select(Probe).where(Probe.host_id == host.id))).scalars().all()
        all_cur = (await db.execute(select(AlertCurEvent).where(
            AlertCurEvent.host_ident == host.ident))).scalars().all()
    cur = [a for a in all_cur if not a.stale]
    stale = [a for a in all_cur if a.stale]
    # D161：硬件画像与 df 明细——「这个机器多少内存/磁盘/几核」是最高频问题之一，
    # 数据本来就在库里（Agent 随心跳上报），此前工具没带出来 → 模型只能拿使用率反推。
    from app.store import ring as _ring
    disks = _ring.get_disks(host.ident) or []
    disk_rows = [{"device": d.get("device"), "mount": d.get("mount"), "fstype": d.get("fstype"),
                  "total_bytes": d.get("size"), "used_bytes": d.get("used"), "free_bytes": d.get("free"),
                  "use_percent": d.get("percent")}
                 for d in disks if isinstance(d, dict)]
    return _ok({
        "ident": host.ident, "name": host.name, "ip": host.ip, "os": host.os,
        "status": host.status, "last_beat": str(host.last_beat or ""),
        "agent_version": host.agent_version, "tags": host.tags or {},
        "hardware": {
            "cpu_cores": host.cpu_cores or 0,
            "cpu_cores_physical": host.cpu_cores_physical or 0,
            "cpu_model": host.cpu_model or "",
            "mem_total_bytes": host.mem_total or 0,
            "boot_time": str(host.boot_time or ""),
            "hostinfo_at": str(host.hostinfo_at or ""),
            "arch": "", "kernel": host.os or "",
        },
        "disks": disk_rows,       # 每挂载点 total/used/avail ——「磁盘大小/可用」直接读这里
        "agent_health": {"auth_state": host.auth_state or "unknown",
                         "last_report_ok_at": str(host.last_report_ok_at or ""),
                         "cache_depth": host.cache_depth or 0,
                         "last_error": host.last_agent_error or ""},
        "probes": {"total": len(probes), "down": sum(1 for p in probes if p.last_status == "down")},
        "active_alerts": [{"rule": a.rule_name, "metric": a.metric, "value": a.trigger_value,
                           "severity": a.severity, "stale": False} for a in cur],
        "stale_alerts": [{"rule": a.rule_name, "metric": a.metric, "reason": a.stale_reason or ""}
                         for a in stale],
    }, t0=t0)


# ---------------- 5b) service_health（P1：服务级诊断信号） ----------------
async def service_health(ident: str, service: str = "", status: str = "") -> dict:
    """查服务级健康：状态/说明/最近检查/服务内部指标/日志来源。

    P1 背景：平台已经在 svc_monitor 里采集 MySQL threads、Redis INFO、Nginx stub_status、
    MQ 队列等指标，但 RCA 之前只能看主机 CPU/内存，模型只能猜“可能是数据库连接数”。
    这个工具把已有信号直接给模型，不新增采集。
    """
    t0 = time.time()
    from app.models.svc import Service

    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
        q = select(Service).where(Service.host_id == host.id, Service.enabled.is_(True))
        if service:
            q = q.where(Service.name == service)
        if status:
            q = q.where(Service.status == status)
        rows = (await db.execute(q.order_by(Service.id))).scalars().all()
    if not rows:
        return _no_data(f"{host.name or host.ident} 没有匹配的服务"
                        + (f"（name={service!r}, status={status!r}）" if service or status else "（尚未声明或未探测）"), t0)
    items = []
    for s in rows:
        spec = s.spec_json or {}
        metrics = s.metrics_json or {}
        items.append({
            "name": s.name, "type": s.type, "status": s.status,
            "note": s.note or "", "last_check_at": str(s.last_check_at or ""),
            "metrics": dict(list(metrics.items())[:40]),
            "unit": spec.get("unit") or "", "log_path": spec.get("log_path") or "",
            "probe": {k: spec.get(k) for k in ("port", "process", "health_url") if spec.get(k)},
        })
    return _ok(items, note=f"{len(items)} 个服务；metrics 是平台最近一次服务探测的解析结果", t0=t0)


# ---------------- 5c) discovered_services（P1：主机发现信号） ----------------
async def discovered_services(ident: str) -> dict:
    """查 Agent 最近一次服务发现（监听端口 + CPU Top 进程）。

    数据来自 Agent 每约 1 分钟上报的 services 快照（只存内存）。它回答“这台机器上
    跑着什么、哪个进程在吃 CPU”，不替代进程级监控，但足以让诊断不再凭空猜。
    """
    t0 = time.time()
    from app.store import ring

    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
    svc = ring.get_services(ident)
    ports = svc.get("ports") or []
    procs = svc.get("processes") or []
    if not ports and not procs:
        return _no_data(f"{host.name or host.ident} 暂无服务发现快照——Agent 约每分钟上报一次，"
                        "刚接入或平台刚重启请等 1 分钟；若长期为空，检查 Agent 是否在运行", t0)
    return _ok({"at": svc.get("at") or "", "ports": ports, "processes": procs},
               note="来自 Agent 最近一次服务发现（约 1 分钟内）；不是历史趋势", t0=t0)


# ---------------- 6) search_cases ----------------
async def search_cases(query: str, top_k: int = 3) -> dict:
    """历史案例召回（FR-E3）。标准路径 = SeekDB 向量召回（l2_distance）；不可用则降级文本匹配。"""
    t0 = time.time()
    from app.services import cases as cases_svc

    r = await cases_svc.recall(query, top_k=top_k)
    if not r["items"]:
        return _no_data(r.get("note") or f"案例库中未找到与「{query[:30]}」相似的历史案例", t0)
    return _ok([{"id": c["id"], "symptom": c["symptom"], "root_cause": c["root_cause"],
                 "resolution": (c.get("resolution_md") or "")[:1200],
                 "distance": c.get("distance")} for c in r["items"]],
               note=f"召回方式：{r['mode']}（{r.get('note','')}）", t0=t0)


# ---------------- 6b) load_skill ----------------
async def load_skill(name: str) -> dict:
    """按需拉取技能全文（SKILL.md）。只在技能明确匹配当前场景时调用，不要投机。"""
    t0 = time.time()
    from app.services import skills as skills_svc

    s = await skills_svc.load_skill(name)
    if s is None:
        listing = await skills_svc.skill_index()
        return _no_data(f"没有名为「{name}」的启用技能。可用技能：" +
                        ", ".join(x["name"] for x in listing) or "（无）", t0)
    return _ok({"name": s["name"], "description": s["description"], "body_md": s["body_md"],
                "commands": s.get("commands") or []},
               note="技能内容优先于通用调查步骤", t0=t0)



# ---------------- 9) fetch_remote_log（P5：按需取证，FR-409）----------------
# 预算按"总次数 + 总字节"计，**不按单次行数**——因为要支持迭代：第一轮不够就换窗/换文件再取。
# 单次诊断上限：5 次调用、累计 1MB（超限返回提示，引导收窄或结束）。
_LOG_BUDGET_CALLS = 5
_LOG_BUDGET_BYTES = 1024 * 1024
_log_budget: dict[str, dict] = {}          # incident/会话键 → 已用预算（进程内，随诊断生命周期）


def reset_log_budget(key: str) -> None:
    """诊断开始时重置预算（每个诊断会话独立计数）。"""
    _log_budget.pop(key, None)


async def fetch_remote_log(ident: str, path: str = "", since: str = "", until: str = "",
                           unit: str = "", pattern: str = "", context: int = 3,
                           budget_key: str = "default", service_type: str = "app",
                           service: str = "") -> dict:
    """按时间窗取远程日志（**相关性优先，不是行数优先**）。

    设计要点（对齐方案 §3.1）：
      · **时间窗为主**：`since/until` 由调用方按"告警首次触发时刻"给出；不给则由平台按
        [now-5min, now+2min] 兜底（前多后少：成因在触发之前）；
      · **Agent 侧本地筛选**后只回传相关片段（级别/错误签名/重复折叠/堆栈成块/首错优先），
        故回传体积通常只有原文的百分之几；
      · **可迭代**：证据不足可再次调用（换窗/换文件/换关键词）——预算按总次数与总字节计；
      · **覆盖报告**随结果返回，AI 据此判断"是窗口选错了还是内容不够"。
    """
    t0 = time.time()
    from app.routers.tasks import enqueue
    from app.services import svc_monitor

    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看）", t0)

        # P2 修补：主机离线时 Agent 收不到任务——下发前就短路，不烧 45s 等待（用户实测报障）。
        beat_age = (datetime.utcnow() - host.last_beat).total_seconds() if host.last_beat else None
        if host.status == "down" or beat_age is None or beat_age > 45:
            return _no_data(f"{host.name or ident} 当前离线（最近心跳 "
                            f"{int(beat_age) if beat_age is not None else '未知'} 秒前）——"
                            "Agent 收不到取证任务（未下发）；等主机恢复在线后再试，"
                            "或在报告里把「日志未获取」写入 not_checked", t0)

        # 预算（总次数）
        used = _log_budget.setdefault(budget_key, {"calls": 0, "bytes": 0})
        if used["calls"] >= _LOG_BUDGET_CALLS:
            return _no_data(f"本次诊断的日志取证次数已达上限（{_LOG_BUDGET_CALLS} 次）——"
                            "请基于已取回的日志下结论，或把不确定项写进 not_checked", t0)
        if used["bytes"] >= _LOG_BUDGET_BYTES:
            return _no_data(f"本次诊断的日志取证累计已达 {_LOG_BUDGET_BYTES // 1024}KB 上限——"
                            "请收窄时间窗或关键词后再试", t0)

        # 目标机能力协商（P5 起 Agent ≥0.3.0 才有通道）
        ver = str(getattr(host, "agent_version", "") or "")
        try:
            major, minor = (int(x) for x in ver.split(".")[:2])
        except Exception:  # noqa: BLE001
            major, minor = 0, 0
        if (major, minor) < (0, 3):
            return _no_data(f"{host.name or ident} 的 Agent 版本为 {ver or '未知'}，"
                            "不支持远程日志取证（需 ≥0.3.0）——请在目标机重跑安装命令升级后重试。"
                            "当前可用 query_metrics / check_probe / host_info 取证，"
                            "或在报告里把「日志未获取」写入 not_checked", t0)

        now = datetime.utcnow()
        since_dt = _parse_dt(since) or (now - timedelta(minutes=5))
        until_dt = _parse_dt(until) or (now + timedelta(minutes=2))
        discover_block: dict | None = None
        svc_row = None
        if not path and not unit:
            # P2：没给 path/unit → 按服务声明自动发现日志文件（Agent ≥0.3.2）。
            try:
                vmajor, vminor, vpatch = (int(x) for x in ver.split(".")[:3])
            except Exception:  # noqa: BLE001
                vmajor, vminor, vpatch = 0, 0, 0
            if (vmajor, vminor, vpatch) < (0, 3, 2):
                return _no_data(f"{host.name or ident} 的 Agent 版本为 {ver or '未知'}，"
                                "不支持日志文件自动发现（需 ≥0.3.2）——请重跑安装命令升级 Agent，"
                                "或直接提供 path / unit", t0)
            from app.models.svc import Service as SvcModel
            q = select(SvcModel).where(SvcModel.host_id == host.id, SvcModel.enabled.is_(True))
            if service:
                q = q.where(SvcModel.name == service)
            svc_rows = (await db.execute(q.order_by(SvcModel.id))).scalars().all()
            if not svc_rows:
                return _no_data(("该主机没有匹配的已声明服务" + (f"（name={service}）" if service else ""))
                                + "——自动发现需要服务声明来定位进程（process/port/unit）；"
                                  "请在服务级监控登记服务，或直接提供 path / unit", t0)
            svc_row = svc_rows[0]
            spec = svc_row.spec_json or {}
            discover_block = {"service": svc_row.name,
                              "process": str(spec.get("process") or ""),
                              "port": spec.get("port") or 0,
                              "unit": str(spec.get("unit") or "")}
            if not any(discover_block.get(k) for k in ("process", "port", "unit")):
                return _no_data(f"服务「{svc_row.name}」的声明里没有 process/port/unit，"
                                "Agent 无从定位进程——请在服务声明里补一项，或直接提供 path / unit", t0)
        payload = {"path": path, "unit": unit, "since": since_dt.isoformat(),
                   "until": until_dt.isoformat(), "pattern": pattern, "level": "auto",
                   "context": max(0, min(int(context), 10)), "service_type": service_type}
        if discover_block is not None:
            payload["discover"] = discover_block
        try:
            task = await enqueue(db, host, "log_fetch", payload, origin=f"diag:{budget_key}",
                                 created_by="ai")
        except HTTPException as e:
            return _no_data(f"取证任务未能下发：{e.detail}", t0)

    # 等 Agent 领取并回填（拉取式通道，Agent 15s 一拍；最多等 45s）
    task_id = task.id
    host_pk = host.id
    deadline = time.time() + 45
    while time.time() < deadline:
        await asyncio.sleep(2)
        async with SessionLocal() as db:
            t = await db.get(AgentTask, task_id)
            if t is None:
                return _err("任务丢失", t0)
            if t.status in ("done", "failed", "expired"):
                break
            # P2 修补：等待期间主机掉线（心跳超时）→ 立即早退，不等满 45s
            h2 = await db.get(Host, host_pk)
            if h2 is not None and (h2.status == "down" or h2.last_beat is None
                                   or (datetime.utcnow() - h2.last_beat).total_seconds() > 45):
                return _no_data("主机在取证等待期间离线（心跳超时）——本次等待已取消；"
                                "残留任务会在 5 分钟后自动回收，主机恢复后可重新取证", t0)
    else:
        return _no_data("Agent 未在 45 秒内完成取证（可能离线或重启中）——"
                        "注意：残留任务会在 5 分钟后自动回收，可稍后重试", t0)

    res = (t.result_json or {}) if t else {}
    used["calls"] += 1
    used["bytes"] += int(res.get("bytes") or 0)
    disc = res.get("discovered") if isinstance(res.get("discovered"), dict) else None

    # P2：发现到了真实日志路径 → 回写为服务声明的"建议 log_path"（不覆盖已声明值，
    # 用户在服务详情一键采纳后转正）。回写失败不影响取证结果。
    if disc and disc.get("chosen") and svc_row is not None:
        try:
            async with SessionLocal() as db:
                from app.models.svc import Service as SvcModel
                row = await db.get(SvcModel, svc_row.id)
                if row is not None:
                    spec = dict(row.spec_json or {})
                    if not spec.get("log_path") and spec.get("suggested_log_path") != disc["chosen"]:
                        spec["suggested_log_path"] = disc["chosen"]
                        spec["suggested_log_path_at"] = datetime.utcnow().isoformat() + "Z"
                        row.spec_json = spec
                        await db.commit()
        except Exception:  # noqa: BLE001 —— 建议回写失败不影响取证
            pass

    if not res.get("ok"):
        extra = ""
        if disc and disc.get("candidates"):
            tops = [c.get("path", "") for c in disc["candidates"][:3]]
            extra = f"（发现候选：{'、'.join(tops)}）"
        return _no_data(f"取证失败：{res.get('error') or t.error or '未知原因'}{extra}", t0)
    if not (res.get("segments") or []):
        # 空结果也要"可行动"：把 Agent 给的 hint 原样带回，并提示可用手段
        return _no_data(f"时间窗内没有取到可用的错误日志。{res.get('hint') or ''}"
                        f"（覆盖报告：扫描 {res.get('scanned')} 行 / 窗口内 {res.get('matched')} 行；"
                        f"本诊断已用取证 {used['calls'] + 1}/{_LOG_BUDGET_CALLS} 次——"
                        "建议换更宽的时间窗、换文件或加 pattern 再试一轮）", t0)

    # 组织给 AI 看的形态：覆盖报告 + 分段正文
    lines_out = []
    for seg in (res.get("segments") or [])[:12]:
        head = f"[{seg.get('signature')}] 命中 {seg.get('count')} 次"
        if seg.get("repeats"):
            head += f"（重复已折叠 {seg['repeats']} 条，首次 {str(seg.get('first_ts') or '')[:19]}）"
        lines_out.append(head)
        lines_out.extend(str(x) for x in (seg.get("lines") or [])[:80])
    text = "\n".join(lines_out)[:40000]
    src = path or (f"journalctl -u {unit}" if unit else "")
    if disc and disc.get("chosen"):
        src = f"{disc['chosen']}（自动发现：来自进程打开的文件，非人工声明）"
    note = (f"定位档={res.get('window', {}).get('source')}；扫描 {res.get('scanned')} 行 → "
            f"窗口内 {res.get('matched')} → 折叠 {res.get('deduped')} 条 → 回传 {res.get('bytes')} 字节"
            + ("（已截断）" if res.get("truncated") else "")
            + f"；本诊断已用取证 {used['calls']}/{_LOG_BUDGET_CALLS} 次。"
            "若证据不足可再次调用（换更宽时间窗 / 换文件 / 换关键词）")
    return _ok({"path": src, "discovered": disc, "window": res.get("window"),
                "covering_report": {k: res.get(k) for k in
                                    ("scanned", "matched", "dropped_by_level", "deduped", "truncated")},
                "segments": res.get("segments") or [], "text": text}, note=note, t0=t0)


def _parse_dt(v: str):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


# ---------------- 7) list_probes ----------------
async def list_probes(status: str = "", limit_n: int = 50) -> dict:
    """跨主机探活清单（默认只列异常：down / 证书临期），支撑"哪些探活失败了"这类聚合问题。"""
    t0 = time.time()
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Probe, Host.name).join(Host, Probe.host_id == Host.id).where(Probe.enabled.is_(True))
        )).all()
    items = []
    for p, hname in rows:
        entry = {"host": hname, "name": p.name, "type": p.type, "target": p.target,
                 "status": p.last_status, "value": p.last_latency_ms,
                 "value_meaning": "证书剩余天数" if p.type == "https-cert" else "延迟ms"}
        items.append(entry)
    if status:
        items = [x for x in items if x["status"] == status]
    else:  # 默认优先暴露异常项（down 与证书 <30 天），正常项折叠计数
        bad = [x for x in items if x["status"] == "down" or (x["type"] == "https-cert" and (x["value"] or 999) < 30)]
        items = bad
    if not items:
        return _no_data(f"没有符合条件的探活（status={status or '异常项'}）", t0)
    return _ok(items[: max(1, min(limit_n, 200))], t0=t0)


# ---------------- 8) list_hosts ----------------
async def list_hosts(status: str = "", limit_n: int = 50) -> dict:
    """跨主机清单（名称/IP/状态/最近心跳/活跃告警数），用于"哪些机器受影响"这类影响面问题。"""
    t0 = time.time()
    from app.models.alert import AlertCurEvent

    async with SessionLocal() as db:
        q = select(Host)
        if status:
            q = q.where(Host.status == status)
        hosts = (await db.execute(q.order_by(Host.id))).scalars().all()
        cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.stale.is_(False)))).scalars().all()
    by_host: dict[str, int] = {}
    for a in cur:
        by_host[a.host_ident] = by_host.get(a.host_ident, 0) + 1
    if not hosts:
        return _no_data(f"没有符合条件的主机（status={status or '全部'}）", t0)
    return _ok([{"ident": h.ident, "name": h.name, "ip": h.ip, "os": h.os, "status": h.status,
                 "last_beat": str(h.last_beat or ""), "active_alerts": by_host.get(h.ident, 0)}
                for h in hosts[: max(1, min(limit_n, 200))]], t0=t0)


# ---------------- 8b) alert_history ----------------
async def alert_history(hours: int = 24, only_recovered: bool = True, host: str = "",
                        top_slowest: int = 10, limit_n: int = 100) -> dict:
    """查告警历史（D89）。默认最近 24h、**只看已恢复**的，返回触发/恢复时间与**恢复耗时**，可按耗时倒序取最慢。

    为什么要有它（用户实测）：此前助手回答不了"过去 24 小时恢复了哪些告警、最慢是哪条"——不是数据不在，
    而是这个表没有暴露成给 LLM 的工具。`alert_his_events` 里每行是一次已结束的告警（is_recovered + recover_time）。
    恢复耗时 = `recover_time - trigger_time`（秒）。注意：**耗时是平台观测口径**（触发→过期/恢复），
    不等于"真实消化时长"，向用户说明时别把两者混为一谈。
    """
    t0 = time.time()
    from app.models.alert import AlertHisEvent

    hours = max(1, min(int(hours), 7 * 24))          # 合法窗口 1h ~ 7d
    top_slowest = max(0, min(int(top_slowest), 50))
    limit_n = max(1, min(int(limit_n), 500))
    since = datetime.utcnow() - timedelta(hours=hours)
    async with SessionLocal() as db:
        q = select(AlertHisEvent).where(AlertHisEvent.trigger_time >= since)
        if only_recovered:
            q = q.where(AlertHisEvent.is_recovered == True)  # noqa: E712
        if host:
            q = q.where(AlertHisEvent.host_ident == host)
        rows = (await db.execute(q.order_by(AlertHisEvent.trigger_time.desc()).limit(limit_n))).scalars().all()
    if not rows:
        return _no_data(f"最近 {hours}h 内没有{'已恢复' if only_recovered else '全部'}告警记录"
                        + (f"（主机 {host}）" if host else ""), t0)

    items = []
    for r in rows:
        durs = None
        if r.is_recovered and r.recover_time:
            durs = int((r.recover_time - r.trigger_time).total_seconds())
        items.append({
            "rule": r.rule_name, "host": r.host_ident, "metric": r.metric,
            "severity": r.severity, "trigger": str(r.trigger_time),
            "recover": str(r.recover_time) if r.recover_time else None,
            "recovered": bool(r.is_recovered), "duration_s": durs,
            "trigger_value": r.trigger_value,
        })
    summary = {
        "window_hours": hours, "only_recovered": only_recovered,
        "count_found": len(items),
        "recovered_in_window": sum(1 for i in items if i["recovered"]),
        "with_duration": sum(1 for i in items if i["duration_s"] is not None),
        "recent_last": str(rows[0].trigger_time),
    }
    if top_slowest and any(i["duration_s"] is not None for i in items):
        slowest = sorted([i for i in items if i["duration_s"] is not None],
                         key=lambda i: i["duration_s"], reverse=True)[:top_slowest]
        summary["slowest_by_duration"] = [{"rule": i["rule"], "host": i["host"],
                                           "duration_s": i["duration_s"],
                                           "trigger": i["trigger"], "recover": i["recover"]} for i in slowest]
    return _ok({"summary": summary, "items": items}, t0=t0)


# ---------------- 8c) 事件 / 变更单 / 巡检 / 通知投递 历史 ----------------
# D90：既然 alert_history（D89）暴露了"历史可查"，就把 AI 助手缺的其它"历史/状态"能力一并补齐，
# 避免用户问"最近有没有事件/做了哪些变更/巡检怎么样/通知发出去了吗"时又得到"没这个能力"。
def _sev_label(v) -> str:
    return {3: "S1", 2: "S2", 1: "S3"}.get(int(v or 0), f"S{int(v or 0)}")


async def event_history(hours: int = 24, status: str = "", limit_n: int = 50) -> dict:
    """查**收敛事件**历史（incidents）。事件的成员是一条条告警，回答"最近有哪些故障/事件、解决了没"用这个。"""
    t0 = time.time()
    from app.models.event import Incident as IncidentModel

    hours = max(1, min(int(hours), 7 * 24))
    limit_n = max(1, min(int(limit_n), 200))
    since = datetime.utcnow() - timedelta(hours=hours)
    async with SessionLocal() as db:
        q = select(IncidentModel).where(IncidentModel.created_at >= since)
        if status:
            q = q.where(IncidentModel.status == status)
        rows = (await db.execute(q.order_by(IncidentModel.created_at.desc()).limit(limit_n))).scalars().all()
    if not rows:
        return _no_data(f"最近 {hours}h 内没有事件记录" + (f"（状态={status}）" if status else ""), t0)
    items = []
    for r in rows:
        dur = None
        if r.status == "resolved" and r.resolved_at:
            dur = int((r.resolved_at - r.created_at).total_seconds())
        items.append({
            "id": r.id, "title": r.title[:80], "severity": _sev_label(r.severity),
            "status": r.status, "host": r.host_ident, "created": str(r.created_at),
            "resolved": str(r.resolved_at) if r.resolved_at else None,
            "resolution_s": dur, "root_cause": (r.root_cause or "")[:200],
            "confidence": r.confidence or "",
        })
    return _ok({"summary": {"window_hours": hours, "count": len(items),
                            "open": sum(1 for i in items if i["status"] != "resolved"),
                            "resolved": sum(1 for i in items if i["status"] == "resolved")},
                "items": items}, t0=t0)


async def runbook_history(hours: int = 24, status: str = "", limit_n: int = 30) -> dict:
    """查**变更单**（处置/runbook）历史。回答"最近做了什么变更/处置"用这个。"""
    t0 = time.time()
    from app.models.ops import Runbook

    hours = max(1, min(int(hours), 7 * 24))
    limit_n = max(1, min(int(limit_n), 100))
    since = datetime.utcnow() - timedelta(hours=hours)
    async with SessionLocal() as db:
        q = select(Runbook).where(Runbook.created_at >= since)
        if status:
            q = q.where(Runbook.status == status)
        rows = (await db.execute(q.order_by(Runbook.created_at.desc()).limit(limit_n))).scalars().all()
    if not rows:
        return _no_data(f"最近 {hours}h 内没有变更单" + (f"（状态={status}）" if status else ""), t0)
    return _ok({"summary": {"window_hours": hours, "count": len(rows)},
                "items": [{"id": r.id, "name": r.name[:80], "status": r.status,
                           "host": r.host_ident, "risk": r.risk_level,
                           "execution_mode": r.execution_mode,
                           "created": str(r.created_at), "incident_id": r.incident_id}
                          for r in rows]}, t0=t0)


async def inspection_history(hours: int = 24, limit_n: int = 10) -> dict:
    """查**巡检**执行历史（最近一次运行 + 结果/异常项）。回答"最近巡检做得怎么样/有没有异常"用这个。"""
    t0 = time.time()
    from app.models.ops import Inspection

    hours = max(1, min(int(hours), 7 * 24))
    limit_n = max(1, min(int(limit_n), 50))
    since = datetime.utcnow() - timedelta(hours=hours)
    async with SessionLocal() as db:
        rows = (await db.execute(select(Inspection)
                                 .where(Inspection.last_run_at.isnot(None))
                                 .where(Inspection.last_run_at >= since)
                                 .order_by(Inspection.last_run_at.desc()).limit(limit_n))).scalars().all()
    if not rows:
        return _no_data(f"最近 {hours}h 内没有巡检记录（或巡检未到运行时间）", t0)
    return _ok({"summary": {"window_hours": hours, "ran": len(rows)},
                "items": [{"id": r.id, "name": r.name[:80], "status": r.last_status,
                           "last_run": str(r.last_run_at), "findings": len(r.findings or []),
                           "incidents_created": r.incidents_created or 0,
                           "report_preview": (r.report_md or "")[:400]}
                          for r in rows]}, t0=t0)


async def notification_history(hours: int = 24, status: str = "", limit_n: int = 30) -> dict:
    """查**告警通知投递**记录。回答"通知发出去没有 / 有多少失败 / 哪个渠道"用这个。"""
    t0 = time.time()
    from app.models.alert import NotificationRecord

    hours = max(1, min(int(hours), 7 * 24))
    limit_n = max(1, min(int(limit_n), 100))
    since = datetime.utcnow() - timedelta(hours=hours)
    async with SessionLocal() as db:
        q = select(NotificationRecord).where(NotificationRecord.created_at >= since)
        if status:
            q = q.where(NotificationRecord.status == status)
        rows = (await db.execute(q.order_by(NotificationRecord.created_at.desc()).limit(limit_n))).scalars().all()
    if not rows:
        return _no_data(f"最近 {hours}h 内没有通知投递记录" + (f"（状态={status}）" if status else ""), t0)
    total = len(rows)
    failed = sum(1 for r_ in rows if r_.status == "failed")
    return _ok({"summary": {"window_hours": hours, "count": total, "failed": failed,
                            "ok_rate": round(100 * (1 - failed / total), 1) if total else 100},
                "items": [{"channel": r.channel, "target": (r.target or "")[:40],
                           "status": r.status, "created": str(r.created_at),
                           "err": (r.error_detail or "")[:120]} for r in rows]}, t0=t0)


# ---------------- 工具注册表 ----------------
async def recent_changes(ident: str = "", hours: int = 24, limit_n: int = 50) -> dict:
    """查**近期变更**时间线（平台侧）：变更单、处置审计、与主机相关的操作审计。

    P1 借鉴 Aurora 的 recent-change investigator：故障调查必须先问“最近改了什么”。
    诚实边界：平台**没有接部署/发布系统**，所以 deployment_data=unavailable；
    不要用“可能是发布导致的”当证据，没查到就写进 not_checked。
    """
    t0 = time.time()
    from app.models.ops import ActionAudit, Runbook
    from app.models.shell import AuditLog

    hours = max(1, min(int(hours), 7 * 24))
    limit_n = max(1, min(int(limit_n), 200))
    since = datetime.utcnow() - timedelta(hours=hours)
    host = None
    async with SessionLocal() as db:
        if ident:
            host = await _host_by_ident(db, ident)
            if host is None:
                return _no_data(f"主机 {ident} 未注册（可用 list_hosts 查看已纳管主机的 ident/name）", t0)
        qa = select(ActionAudit).where(ActionAudit.created_at >= since)
        qr = select(Runbook).where(Runbook.created_at >= since)
        if host is not None:
            qa = qa.where(ActionAudit.host_ident == host.ident)
            qr = qr.where(Runbook.host_ident == host.ident)
        actions = (await db.execute(qa.order_by(ActionAudit.created_at.desc()).limit(limit_n))).scalars().all()
        runbooks = (await db.execute(qr.order_by(Runbook.created_at.desc()).limit(limit_n))).scalars().all()
        # 用户审计没有 host_ident 列：取最近窗口后在 Python 侧按 ident/主机名过滤，
        # 避免把全平台的规则变更都塞给一次主机诊断。
        audits = (await db.execute(
            select(AuditLog).where(AuditLog.created_at >= since)
            .order_by(AuditLog.created_at.desc()).limit(limit_n * 4))).scalars().all()
    items: list[dict] = []
    for a in actions:
        items.append({"time": a.created_at, "source": "action_audit", "type": a.action,
                      "summary": f"{a.action} · {a.status} · {a.operator}",
                      "status": a.status, "ref": f"action:{a.id}"})
    for r in runbooks:
        items.append({"time": r.created_at, "source": "runbook", "type": "runbook",
                      "summary": f"变更单 #{r.id} {r.name[:60]} · {r.status} · 风险 {r.risk_level}",
                      "status": r.status, "ref": f"runbook:{r.id}"})
    if host is not None:
        needle = host.ident[:12]
        name = host.name or ""
        for a in audits:
            blob = f"{a.resource} {json.dumps(a.detail or {}, ensure_ascii=False, default=str)}"
            if needle and needle in blob or (name and name in blob):
                items.append({"time": a.created_at, "source": "audit", "type": a.action,
                              "summary": f"{a.action} · {a.resource[:80]}", "status": "",
                              "ref": f"audit:{a.id}"})
    else:
        for a in audits[:limit_n]:
            items.append({"time": a.created_at, "source": "audit", "type": a.action,
                          "summary": f"{a.action} · {a.resource[:80]}", "status": "",
                          "ref": f"audit:{a.id}"})
    items.sort(key=lambda x: x["time"], reverse=True)
    items = items[:limit_n]
    note = ("deployment_data=unavailable：平台未接部署/发布系统；不要把“可能是发布导致”当证据，"
            "没查到就写进 not_checked。")
    if not items:
        return _no_data(f"最近 {hours}h 内没有与{'这台主机' if host is not None else '平台'}相关的变更/处置记录；{note}", t0)
    for it in items:
        it["time"] = str(it["time"])
    return _ok({"summary": {"window_hours": hours, "count": len(items),
                            "host": host.ident if host is not None else "",
                            "deployment_data": "unavailable"},
                "items": items}, note=note, t0=t0)


def tool_specs() -> list[dict]:
    """OpenAI function-calling 工具声明（只读 8 件 + 终态 write_findings）。"""
    p = lambda **kw: kw  # noqa: E731
    return [
        {"type": "function", "function": {
            "name": "run_readonly",
            "description": "在目标机执行一条只读白名单内的命令并回传输出（ls/stat/df/free/du/cat/tail/"
                           "grep/nproc/journalctl/systemctl status…）。用于：文件/目录是否存在与大小、"
                           "配置位置、进程与端口、系统规格等时序指标覆盖不到的问题。命令必须是只读的，"
                           "写操作会被目标机拒绝。",
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机 ident"),
                "cmd": p(type="string", description="只读命令，如 ls -la /etc/nginx/conf.d"),
            }, required=["ident", "cmd"])}},
        {"type": "function", "function": {
            "name": "query_metrics",
            "description": "查询某主机某指标的时间窗口（内存环形/1m 聚合）。返回 min/max/avg/latest 与最近 30 个点。",
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机标识：ident（fingerprint）或主机名 name 都可以"),
                "tags": p(type="object", description="可选维度（如 {\"server\":\"db01\"}）——带维度时查该实例自己的序列"),
                "metric": p(type="string", description="指标名，如 cpu.percent / mem.percent / disk.percent / load.1"),
                "minutes": p(type="integer", description="回看分钟数，默认 60，上限 1440"),
            }, required=["ident", "metric"])}},
        {"type": "function", "function": {
            "name": "check_probe",
            "description": "查询主机探活状态（http/tcp/process 的 up|down 与延迟；https-cert 返回证书剩余天数）。",
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机标识：ident 或主机名"), "name": p(type="string", description="探活名，留空查全部"),
            }, required=["ident"])}},
        {"type": "function", "function": {
            "name": "fetch_remote_log",
            "description": ("按**时间窗**取远程主机日志（远程取证；Agent ≥0.3.0）。"
                            "path/unit **可留空**（Agent ≥0.3.2）：只给 ident（+可选 service 名），"
                            "Agent 会按服务声明定位进程、发现它打开的日志文件并拉取——"
                            "发现路径回写为该服务的「建议日志路径」，仍受目标机日志白名单约束。"
                            "声明了 log_path/unit 时按声明取。"
                            "相关性优先：Agent 侧按级别/错误签名筛选、折叠重复行、保留堆栈与首错，只回传相关片段。"
                            "**证据不足可再次调用**：向前放宽 since（成因常在触发之前）、向后延伸 until、"
                            "换 path（如 error.log→slow.log）、加 pattern 聚焦。"
                            "单次诊断最多 5 次、累计 1MB。返回含覆盖报告（扫描/命中/丢弃/折叠/截断/实际窗口）"
                            "——据此判断是窗口选错了还是内容不够。"),
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机标识：ident 或主机名"),
                "path": p(type="string", description="日志文件绝对路径（与 unit 可二选一；留空则自动发现）"),
                "unit": p(type="string", description="systemd 单元名（如 mysql/nginx；留空则自动发现）"),
                "service": p(type="string", description="服务名（声明里的 name）；留空且主机只登记了一个服务时自动用那个"),
                "since": p(type="string", description="窗口起点 ISO8601（建议用告警首次触发时刻减几分钟）"),
                "until": p(type="string", description="窗口终点 ISO8601（建议触发时刻加 2 分钟）"),
                "pattern": p(type="string", description="可选关键词/正则，用于进一步聚焦"),
                "context": p(type="integer", description="每条命中保留的前后行数，默认 3（上限 10）"),
                "service_type": p(type="string", description="mysql/redis/nginx/mq/app，用于选用错误签名库"),
            }, required=["ident"])}},
        {"type": "function", "function": {
            "name": "read_log_tail",
            "description": "读取日志文件尾部。注意：V1 仅平台自身主机可读，远程主机会返回 NO_DATA。",
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机标识：ident 或主机名"), "path": p(type="string"), "lines": p(type="integer"),
            }, required=["ident", "path"])}},
        {"type": "function", "function": {
            "name": "get_process_list",
            "description": ("取 CPU 占用最高的进程清单。平台自身主机实时 ps；远程主机返回 Agent "
                            "最近一次服务发现里的 Top 进程（约 1 分钟一次，只含 CPU Top）。"),
            "parameters": p(type="object", properties={
                "ident": p(type="string"), "top_n": p(type="integer"),
            }, required=["ident"])}},
        {"type": "function", "function": {
            "name": "host_info",
            "description": "主机静态信息（OS/状态/最近心跳/标签/Agent 健康）+ 探活汇总 + 当前新鲜告警 + 陈旧告警。",
            "parameters": p(type="object", properties={"ident": p(type="string")}, required=["ident"])}},
        {"type": "function", "function": {
            "name": "service_health",
            "description": ("查主机上已声明服务的**服务级健康**：状态/说明/最近检查时间/服务内部指标"
                            "（MySQL threads、Redis INFO、Nginx stub_status、MQ 队列等）/日志路径。"
                            "诊断“数据库连接满/缓存异常/网关 5xx”时先查这个，不要只看主机 CPU。"),
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机标识：ident 或主机名"),
                "service": p(type="string", description="服务名，留空查全部"),
                "status": p(type="string", description="按状态过滤：up/degraded/down/unknown；留空=全部"),
            }, required=["ident"])}},
        {"type": "function", "function": {
            "name": "discovered_services",
            "description": ("查 Agent 最近一次**服务发现**：正在监听的端口 + CPU Top 进程（约 1 分钟一份）。"
                            "回答“这台机器上跑着什么/谁在吃 CPU/端口有没有起来”用这个；它不是历史趋势。"),
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机标识：ident 或主机名"),
            }, required=["ident"])}},
        {"type": "function", "function": {
            "name": "search_cases",
            "description": "按现象/根因关键词检索历史案例卡（同类型故障第二次发生时可复用处置）。",
            "parameters": p(type="object", properties={
                "query": p(type="string"), "top_k": p(type="integer"),
            }, required=["query"])}},
        {"type": "function", "function": {
            "name": "load_skill",
            "description": "按名称加载技能全文（SKILL.md）。系统提示里给了技能索引；仅当技能明确匹配当前场景时调用，不要投机性加载。",
            "parameters": p(type="object", properties={
                "name": p(type="string", description="技能名，如 core-investigation / core-safety"),
            }, required=["name"])}},
        {"type": "function", "function": {
            "name": "list_probes",
            "description": "跨主机探活清单。缺省只返回异常项（down 与证书 <30 天）——回答「哪些探活失败了」用这个。",
            "parameters": p(type="object", properties={
                "status": p(type="string", description="按状态过滤：up / down / unknown；留空=只列异常项"),
                "limit_n": p(type="integer"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "list_hosts",
            "description": "跨主机清单（名称/IP/状态/最近心跳/活跃告警数）——回答「哪些机器受影响/有多少台离线」用这个。",
            "parameters": p(type="object", properties={
                "status": p(type="string", description="up / down / pending；留空=全部"),
                "limit_n": p(type="integer"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "recent_changes",
            "description": ("查**近期变更**时间线：变更单、处置审计、与主机相关的操作审计。"
                            "回答“最近改了什么/有没有发布/谁动过这台机器”用这个。"
                            "注意：平台未接部署系统（deployment_data=unavailable），没查到发布记录就写 not_checked，不要编造。"),
            "parameters": p(type="object", properties={
                "ident": p(type="string", description="主机标识；留空=平台全量"),
                "hours": p(type="integer", description="回看小时数，默认 24，上限 168"),
                "limit_n": p(type="integer", description="最多返回条数，默认 50，上限 200"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "alert_history",
            "description": ("查告警历史（默认最近 24h、只看**已恢复**的）。回答「过去 N 小时恢复了哪些告警 / "
                            "最慢/最快的是哪条、为什么」用这个。返回每条：规则名 / 主机 / 指标 / 触发与恢复时间 / "
                            "恢复耗时秒（recover_time − trigger_time）；并按耗时倒序给最慢的前若干条。"
                            "注意：耗时为平台观测口径，不等同真实消化时长。"),
            "parameters": p(type="object", properties={
                "hours": p(type="integer", description="回看小时数，默认 24，上限 168（7 天）"),
                "only_recovered": p(type="boolean", description="只看已恢复的告警，默认 true"),
                "host": p(type="string", description="只查某台主机（ident 或主机名）；留空查全部"),
                "top_slowest": p(type="integer", description="按恢复耗时倒序取最慢的前 N 条，默认 10；传 0 不单独列"),
                "limit_n": p(type="integer", description="最多返回条数，默认 100，上限 500"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "event_history",
            "description": ("查**收敛事件**（incidents）历史——一个事件是若干条相关告警收敛成的故障。"
                            "回答「最近有哪些故障/事件、解决了没、最久多久」用这个。返回 事件号/标题/严重度/状态/主机/创建与解决时间/解决耗时/根因。"),
            "parameters": p(type="object", properties={
                "hours": p(type="integer", description="回看小时数，默认 24，上限 168"),
                "status": p(type="string", description="按状态过滤：open / ack / resolved；留空=全部"),
                "limit_n": p(type="integer", description="最多返回条数，默认 50，上限 200"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "runbook_history",
            "description": ("查**变更单（处置）**历史。回答「最近做了什么变更/处置、哪些在跑/做完了」用这个。"
                            "返回 单号/名称/状态/目标主机/风险级别/执行方式/创建时间/关联事件号。"),
            "parameters": p(type="object", properties={
                "hours": p(type="integer", description="回看小时数，默认 24，上限 168"),
                "status": p(type="string", description="按状态过滤：draft/approved/executing/done/aborted；留空=全部"),
                "limit_n": p(type="integer", description="最多返回条数，默认 30，上限 100"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "inspection_history",
            "description": ("查**巡检**执行历史。回答「最近巡检做了没有、结果怎么样、有没有发现异常」用这个。"
                            "返回 巡检名/上次运行时间/状态/命中异常项数/新建事件数/报告预览。"),
            "parameters": p(type="object", properties={
                "hours": p(type="integer", description="回看小时数，默认 24，上限 168"),
                "limit_n": p(type="integer", description="最多返回条数，默认 10，上限 50"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "notification_history",
            "description": ("查**告警通知投递**记录。回答「告警通知发出去没有 / 失败了多少 / 哪个渠道」用这个。"
                            "返回 渠道/目标/状态/时间/失败原因，并给成功率。"),
            "parameters": p(type="object", properties={
                "hours": p(type="integer", description="回看小时数，默认 24，上限 168"),
                "status": p(type="string", description="按状态过滤：sent / failed；留空=全部"),
                "limit_n": p(type="integer", description="最多返回条数，默认 30，上限 100"),
            }, required=[])}},
        {"type": "function", "function": {
            "name": "tcp_snapshot",
            "description": "TCP 连接状态最近读数（ESTABLISHED/TIME_WAIT/LISTEN/CLOSE_WAIT/SYN_SENT/总数）。排查连接泄漏、端口耗尽。",
            "parameters": p(type="object", properties={"ident": p(type="string")}, required=["ident"])}},
        {"type": "function", "function": {
            "name": "disk_io_top",
            "description": "磁盘 IO 各分区近 60 分钟 p95 排序 TopN（BPS/IOPS）。定位 IO 打满的分区。",
            "parameters": p(type="object", properties={"ident": p(type="string"), "top_n": p(type="integer")}, required=["ident"])}},
        {"type": "function", "function": {
            "name": "oom_history",
            "description": "近 N 小时内存类告警中提及 OOM 的记录（平台侧追溯）。判断内存高是否已实际触发 OOM-killer。",
            "parameters": p(type="object", properties={"ident": p(type="string"), "hours": p(type="integer")}, required=["ident"])}},
        {"type": "function", "function": {
            "name": "journal_errors",
            "description": "近 N 小时该主机的严重告警事件（severity≤2，S1/S2），即系统错误第一现场的时间线。",
            "parameters": p(type="object", properties={"ident": p(type="string"), "hours": p(type="integer")}, required=["ident"])}},
        {"type": "function", "function": {
            "name": "proc_top_mem",
            "description": "进程快照按内存占用 TopN（含 RSS/打开文件数/用户）。get_process_list 只看 CPU，内存泄漏用它。",
            "parameters": p(type="object", properties={"ident": p(type="string"), "top_n": p(type="integer")}, required=["ident"])}},
        {"type": "function", "function": {
            "name": "write_findings",
            "description": "【终态工具】提交最终诊断报告（Markdown + YAML frontmatter）。调用它即结束诊断。",
            "parameters": p(type="object", properties={
                "findings_md": p(type="string", description=(
                    "完整 Markdown。必须包含 YAML frontmatter（--- 包裹）字段：agent_id、purpose、status、"
                    "tools_used、citations（非空）、self_assessed_strength、confidence、impact"
                    "（affected/checked_healthy/changed_other_reason/not_checked）；"
                    "正文必须含四个二级标题：## 结论、## 证据、## 推理、## 已排除的假设。"
                    "impact.affected 每项须带括号证据 token。")),
            }, required=["findings_md"])}},
    ]


# ---------- D119 健康诊断融合工具（FR-C5）：5 个只读工具，全部读平台侧已有数据 ----------
# （ring 明细 / 进程快照 / 告警历史），不下发 Agent 命令——与诊断树同源的只读纪律。

async def tcp_snapshot(ident: str) -> dict:
    """TCP 连接状态最近值（ring）。"""
    from datetime import datetime as _dt
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
    if host is None:
        return _err("主机不存在", t0)
    now = _dt.utcnow()
    out = {}
    for st in ("established", "time_wait", "listen", "close_wait", "syn_sent", "total"):
        pts = ring.query_points(host.ident, f"net.tcp.{st}", now - _td(minutes=10), now)
        if pts:
            out[st] = pts[-1][1]
    if not out:
        return _no_data("该主机暂无 net.tcp.* 指标（Agent 需升级到支持 D118 采集）", t0)
    return _ok(out, note="最近 10 分钟内最新读数", t0=t0)


async def disk_io_top(ident: str, top_n: int = 5) -> dict:
    """磁盘 IO 各分区指标近 60 分钟 p95 排序 TopN。"""
    from datetime import datetime as _dt
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
    if host is None:
        return _err("主机不存在", t0)
    now = _dt.utcnow()
    rows = []
    for m in ring.list_metric_names(host.ident, "disk.io."):
        pts = ring.query_points(host.ident, m, now - _td(minutes=60), now)
        if len(pts) < 3:
            continue
        vals = sorted(v for _, v in pts)
        rows.append({"metric": m, "p95": round(vals[min(len(vals) - 1, int(len(vals) * 0.95))], 1),
                     "peak": round(vals[-1], 1)})
    if not rows:
        return _no_data("该主机暂无 disk.io.* 指标", t0)
    rows.sort(key=lambda r: -r["p95"])
    return _ok(rows[: max(1, min(int(top_n or 5), 10))], note="按 60 分钟窗口 p95 排序", t0=t0)


async def oom_history(ident: str, hours: int = 24) -> dict:
    """近 N 小时内存类告警且规则名/AI 摘要提及 OOM（平台侧追溯，不下发命令）。"""
    from datetime import datetime as _dt
    from sqlalchemy import text as _text
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _err("主机不存在", t0)
        rows = (await db.execute(_text(
            "SELECT id, rule_name, metric, trigger_value, trigger_time, ai_summary FROM alert_his_events "
            "WHERE host_ident=:i AND trigger_time > :t AND (metric LIKE 'mem%' OR rule_name LIKE '%内存%') "
            "ORDER BY trigger_time DESC LIMIT 50"),
            {"i": host.ident, "t": _dt.utcnow() - _td(hours=min(max(int(hours or 24), 1), 168))})).all()
        hits = [{"id": r[0], "rule": r[1], "metric": r[2], "value": r[3],
                 "time": str(r[4]), "summary": (r[5] or "")[:200]} for r in rows
                if "oom" in ((r[1] or "") + (r[5] or "")).lower()]
    return _ok({"hits": hits, "count": len(hits),
                "note": "无命中说明近窗无 OOM；内存高但无 OOM 常见于缓存型占用"}, t0=t0)


async def journal_errors(ident: str, hours: int = 2) -> dict:
    """近 N 小时该主机的严重告警事件（severity≤2，错误第一现场）。"""
    from datetime import datetime as _dt
    from sqlalchemy import text as _text
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _err("主机不存在", t0)
        rows = (await db.execute(_text(
            "SELECT rule_name, metric, severity, trigger_value, trigger_time FROM alert_his_events "
            "WHERE host_ident=:i AND trigger_time > :t AND severity <= 2 "
            "ORDER BY trigger_time DESC LIMIT 50"),
            {"i": host.ident, "t": _dt.utcnow() - _td(hours=min(max(int(hours or 2), 1), 72))})).all()
    events = [{"rule": r[0], "metric": r[1], "severity": r[2], "value": r[3], "time": str(r[4])} for r in rows]
    return _ok({"events": events, "count": len(events),
                "note": "严重度≤2 的近期告警即系统错误第一现场" if events else "近窗无 S1/S2 告警"}, t0=t0)


async def proc_top_mem(ident: str, top_n: int = 5) -> dict:
    """进程快照按内存 TopN（补 get_process_list 只看 CPU 的盲区）。"""
    t0 = time.time()
    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
    if host is None:
        return _err("主机不存在", t0)
    procs = (ring.get_services(host.ident) or {}).get("processes") or []
    if not procs:
        return _no_data("暂无进程快照（升级 Agent 后每分钟上报 CPU∪内存 TopN）", t0)
    rows = sorted(procs, key=lambda p: -(p.get("mem_percent") or 0))[: max(1, min(int(top_n or 5), 10))]
    return _ok([{"name": p.get("name"), "pid": p.get("pid"), "user": p.get("user"),
                 "mem_percent": p.get("mem_percent"), "mem_rss": p.get("mem_rss"),
                 "open_fds": p.get("open_fds"), "cpu_pct": p.get("cpu")} for p in rows],
               note="按内存占用排序（快照每分钟刷新）", t0=t0)


# ---------------- 15) run_readonly（D161：AI 助手的通用只读取证）----------------
# D177：命令串联符号（`a && b` / `a; b` / `a | b` 之外的链式组合）——
# 只读白名单是前缀判定，串联会把"白名单首词"变成走私其它命令的通道。
_CHAIN_RE = re.compile(r"&&|\|\||;|`|\$\(")


def authorize_readonly(cmd: str, allowed: list | None = None) -> tuple[bool, str]:
    """只读命令放行判定（平台侧；D173/P2 加了技能授权路径）。

    返回 (是否放行, 原因)。放行顺序固定：
      1. **denylist 优先**——命中危险模式一律拒绝（技能无法越过这条硬边界）；
      2. 原有只读前缀白名单（runbook.READONLY_PREFIXES）——现状不变；
      3. 技能授权命令：与 allowed 中某条**完全相等**、且通过**只读形状校验**
         （runbook.skill_command_allowed：无 shell 组合字符 + 只读工具集 + 子命令约束）
         才放行。

    第 3 步的两道闸缺一不可：精确相等挡住"前缀逃逸"，形状校验挡住"授权了一个写命令"。
    单独抽成纯函数是为了可测：run_readonly 里还接着写 AgentTask/等回执，
    判定逻辑不该被 IO 绑住。
    """
    from app.services.runbook import guard_command, skill_command_allowed

    cmd = (cmd or "").strip()
    if not cmd:
        return False, "空命令"
    # D177（公网真机实测）：只读白名单是**前缀**判定——"df -h && curl http://x"、
    # "df -h; rm -rf /" 这类**串联命令**会因首词命中白名单而在平台侧被判成"只读命令"，
    # 靠目标机 Agent 的 RO_DENY 才没被执行（此前真机实测：LLM 拼了
    # `df -i && echo ... && du ... | tail` 被 Agent 拦下，报错却说"目标机拒绝"，
    # 模型以为目标机故障、白白多花一轮）。现在平台侧**先**拦串联并说清原因。
    # 注：只加在只读命令路径——变更单的动作白名单里有"nginx -t || 隔离; systemctl restart nginx"
    # 这类**有意设计的复合动作**，不受此处影响。
    if _CHAIN_RE.search(cmd):
        return False, ("已拒绝：命令不支持串联（&& / || / ; / 反引号 / $()）——"
                       "只允许单条只读命令，请拆成多次分别执行")
    g = guard_command(cmd)
    if g.verdict == "denylist":
        return False, (f"命令命中禁用模式（{g.reason}）——危险命令一律拒绝，"
                       f"不在只读白名单范围内")
    allowed_list = [str(c).strip() for c in (allowed or []) if str(c).strip()]
    if g.verdict == "allowlist":
        return True, "只读白名单命令"
    if cmd in allowed_list:
        shape_ok, shape_reason = skill_command_allowed(cmd)
        if not shape_ok:
            return False, f"技能授权命令未通过只读形状校验：{shape_reason}"
        return True, f"本次诊断已加载技能授权的命令（会话内 {len(allowed_list)} 条）"
    return False, (f"命令未通过只读白名单（{g.verdict}）：{g.reason}——只允许只读命令"
                   f"（df/free/ps/ls/cat/tail/head/grep/du/stat/nproc/journalctl…）"
                   + (f"；本会话技能授权命令：{len(allowed_list)} 条（需完全一致）" if allowed_list else ""))


async def run_readonly(ident: str, cmd: str, allowed: list | None = None) -> dict:
    """在目标机执行一条**只读白名单内**的命令并回传输出。

    为什么是"白名单内"而不是"任意命令"：安全边界是**配置**（runbook.py 的
    READONLY_PREFIXES + 禁用模式 + 写重定向拒绝——与 runbook 只读步骤同一条信任链），
    白名单加一条 = 新能力，而不是改代码。平台侧先判一遍，Agent 侧再判一遍（纵深防御）。
    覆盖长尾问题：文件在不在（ls/stat）、目录多大（du）、规格（nproc/free）、
    进程谁占资源（ps/proc_top_mem 组合）、配置在哪（grep）……

    D173/P2：`allowed` = 本次诊断**已加载技能**授权的命令（会话作用域，不落全局）。
    """
    t0 = time.time()
    ok, reason = authorize_readonly(cmd, allowed)
    if not ok:
        return _err(reason)
    cmd = (cmd or "").strip()
    allowed_list = [str(c).strip() for c in (allowed or []) if str(c).strip()]
    from app.models.svc import AgentTask

    async with SessionLocal() as db:
        host = await _host_by_ident(db, ident)
        if host is None:
            return _no_data(f"主机 {ident} 未注册", t0)
        if host.status != "up":
            return _no_data(f"主机 {ident} 当前离线（Agent 不在拉取任务），无法执行", t0)
        now = datetime.utcnow()
        pend = (await db.execute(select(AgentTask).where(
            AgentTask.host_id == host.id, AgentTask.status == "pending",
            AgentTask.expires_at > now))).scalars().first()
        if pend is not None:
            return _err("该主机已有待领取任务（约 15 秒内执行），请稍后重试")
        t = AgentTask(host_id=host.id, kind="run_readonly",
                      payload_json={"cmd": cmd, "allowed_commands": allowed_list},
                      origin=f"tool:{ident}",
                      created_by="rca_tools", expires_at=now + timedelta(minutes=5))
        db.add(t)
        await db.commit()
        task_id = t.id

    deadline = asyncio.get_event_loop().time() + 45
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as db:
            t = await db.get(AgentTask, task_id)
            if t is not None and t.status in ("done", "failed"):
                res = t.result_json or {}
                if not res.get("ok"):
                    err = res.get("error") or t.error or "未知原因"
                    # D177：Agent 拒的是**我们的只读策略**（串联/写重定向/危险命令），
                    # 不是目标机故障——文案说错会让模型以为机器坏了，白白再花一轮。
                    if "被禁止的模式" in str(err) or "只读白名单" in str(err):
                        return _err(f"命令被目标机 Agent 的只读策略拒绝：{err}——"
                                    f"请拆成单条只读命令（不要用 && 串联）", t0)
                    return _err(f"目标机拒绝或执行失败：{err}", t0)
                out = res.get("stdout") or ""
                return _ok({"cmd": cmd, "rc": res.get("rc"), "stdout": out,
                            "stderr": (res.get("stderr") or "")[:2000],
                            "truncated": res.get("truncated", False),
                            "task_id": task_id}, t0=t0)
        await asyncio.sleep(2)
    return _err(f"目标机 {45}s 内未回执（任务 {task_id} 已留痕，可稍后在任务列表查看）", t0)


DISPATCH = {
    "load_skill": load_skill,
    "list_probes": list_probes,
    "list_hosts": list_hosts,
    "alert_history": alert_history,
    "event_history": event_history,
    "runbook_history": runbook_history,
    "recent_changes": recent_changes,
    "inspection_history": inspection_history,
    "notification_history": notification_history,
    "query_metrics": query_metrics,
    "check_probe": check_probe,
    "read_log_tail": read_log_tail,
    "get_process_list": get_process_list,
    "host_info": host_info,
    "service_health": service_health,
    "discovered_services": discovered_services,
    "search_cases": search_cases,
    "fetch_remote_log": fetch_remote_log,
    "tcp_snapshot": tcp_snapshot,
    "disk_io_top": disk_io_top,
    "oom_history": oom_history,
    "journal_errors": journal_errors,
    "proc_top_mem": proc_top_mem,
    "run_readonly": run_readonly,
}


async def invoke(name: str, args: dict, allowed: list | None = None) -> dict:
    """工具调用入口。`allowed` = 本次诊断会话内技能授权的只读命令（D173/P2）。

    只传给 run_readonly（唯一用到它的工具）；其余工具忽略该参数。
    """
    fn = DISPATCH.get(name)
    if fn is None:
        return _err(f"未知工具：{name}")
    try:
        if name == "run_readonly":
            return await fn(allowed=list(allowed or []), **args)
        return await fn(**args)
    except TypeError as e:
        return _err(f"参数错误：{e}")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def spill(content: str, name: str) -> tuple[str, str | None]:
    """超预算输出落盘：返回 (preview, path)。"""
    os.makedirs(SPILL_DIR, exist_ok=True)
    path = os.path.join(SPILL_DIR, f"{name}_{int(time.time() * 1000)}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    preview = content[:2000]
    return preview, path


def tool_result_text(name: str, result: dict, token_budget_chars: int) -> str:
    """把工具结果渲染成给 LLM 的文本；超预算则落盘并给文件路径（Holmes §2.5）。"""
    payload = json.dumps(result, ensure_ascii=False)
    if len(payload) <= token_budget_chars:
        return f"[{name}] {payload}"
    preview, path = spill(payload, name)
    return (f"[{name}] 输出过长（{len(payload)} 字符，超过单次上限 {token_budget_chars}），已落盘。\n"
            f"Saved to: {path}（可用 read_log_tail 读取该路径，或缩小查询范围）\n"
            f"预览（截断）: {preview}")
