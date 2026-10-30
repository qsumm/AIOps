"""健康诊断引擎（D119，FR-C；对标阿里云「实例问题排查」）。

设计要点（02-技术设计方案 §5）：
- **规则诊断树**：声明式 Check 列表，确定性判定，**完全不依赖 LLM**——服务商限流时
  诊断照样完整可用（公网 MiniMax 额度耗尽事故的直接教训）。
- **数据源全部只读**：ring 明细 / metric_agg 聚合 / services+disks 内存快照 / DB（探活、服务、告警历史）。
  不下发 Agent 命令 → 零白名单风险、零等待。
- 每检查项独立 try/except：单项失败 → status="error"，绝不中断整份报告。
- 结果四态：pass / warn / fail / skip（数据不足，如老 agent 未采集新指标）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from app.store import ring

log = logging.getLogger("health_check")

WINDOW_MIN = 60          # 诊断数据窗：回看 60 分钟（报告 window 字段落库，FR-C3 追溯）
FAIL_WARN_THRESHOLDS = (85.0, 70.0)   # fail / warn 通用阈值（CPU、内存、磁盘、inode 同源）


# ---------- 数据访问 helper（全只读） ----------

async def _agg_series(db, ident: str, metric: str, minutes: int) -> list[tuple[datetime, float]]:
    """ring 优先（15s 明细），不足部分 metric_agg 1m 桶补齐。"""
    end = datetime.utcnow()
    start = end - timedelta(minutes=minutes)
    pts = list(ring.query_points(ident, metric, start, end))
    if pts and pts[0][0] <= start:
        return pts
    rows = (await db.execute(text(
        "SELECT ts, value_avg FROM metric_agg WHERE ident=:i AND metric=:m AND ts BETWEEN :s AND :e ORDER BY ts"),
        {"i": ident, "m": metric, "s": start, "e": pts[0][0] if pts else end})).all()
    return [(r[0], float(r[1])) for r in rows] + pts


def _stats(pts: list[tuple[datetime, float]]) -> dict:
    """latest / avg / p95 / peak。空序列返回全 None（调用方据此 skip）。"""
    if not pts:
        return {"latest": None, "avg": None, "p95": None, "peak": None}
    vals = sorted(v for _, v in pts)
    n = len(vals)
    return {"latest": round(vals[-1], 1),
            "avg": round(sum(vals) / n, 1),
            "p95": round(vals[min(n - 1, int(n * 0.95))], 1),
            "peak": round(vals[-1], 1) if vals else None}


def _thres(s: dict, fail=FAIL_WARN_THRESHOLDS[0], warn=FAIL_WARN_THRESHOLDS[1]) -> str:
    """按 p95+latest 双口径定级：任一过 fail 阈 → fail；任一过 warn 阈 → warn。"""
    vals = [v for v in (s.get("p95"), s.get("latest")) if v is not None]
    if not vals:
        return "skip"
    if max(vals) >= fail:
        return "fail"
    if max(vals) >= warn:
        return "warn"
    return "pass"


# ---------- 检查项（10 个，FR-C4） ----------

async def check_cpu_high(db, ident, ev):
    s = _stats(await _agg_series(db, ident, "cpu.percent", WINDOW_MIN))
    if s["latest"] is None:
        return "skip", [], "暂无 CPU 指标（等待 Agent 上报）"
    st = _thres(s)
    ev.append({"token": "cpu_stats", "detail": f"avg={s['avg']}% p95={s['p95']}% peak={s['peak']}%"})
    if st == "skip":
        return "skip", ev, "暂无 CPU 指标"
    con = {"fail": "CPU 使用率过高，请到「进程」页签定位高占用进程",
           "warn": "CPU 使用率偏高，建议关注趋势并定位高占用进程",
           "pass": "CPU 使用率正常"}[st]
    return st, ev, con


async def check_mem_high(db, ident, ev):
    s = _stats(await _agg_series(db, ident, "mem.percent", WINDOW_MIN))
    if s["latest"] is None:
        return "skip", [], "暂无内存指标"
    st = _thres(s)
    ev.append({"token": "mem_stats", "detail": f"avg={s['avg']}% p95={s['p95']}% peak={s['peak']}%"})
    # OOM 追溯（FR-C4）：近 24h 告警历史内存类告警且 AI 摘要提及 OOM → 直接 fail（泄漏/超卖实锤）
    rows = (await db.execute(text(
        "SELECT COUNT(*) FROM alert_his_events WHERE host_ident=:i AND trigger_time > :t "
        "AND (metric LIKE 'mem%' OR rule_name LIKE '%内存%') "
        "AND (ai_summary LIKE '%OOM%' OR rule_name LIKE '%OOM%')"),
        {"i": ident, "t": datetime.utcnow() - timedelta(hours=24)})).scalar()
    if rows:
        st = "fail"
        ev.append({"token": "oom_history", "detail": f"近 24h 命中 OOM 相关告警 {rows} 条"})
    con = {"fail": "内存吃紧（含 OOM 追溯命中）——查 RSS TopN 进程定位泄漏",
           "warn": "内存偏高，建议关注 buffers/cached 与 RSS TopN 进程",
           "pass": "内存使用正常"}[st]
    return st, ev, con


async def _disk_mount_series(db, ident, minutes):
    """真实分区使用率序列（disk.<mnt>.percent，排除 io/inode 前缀）。"""
    names = ring.list_metric_names(ident, "disk.")
    return [n for n in names if not n.startswith("disk.io.") and not n.startswith("disk.inode.")]


async def check_disk_full(db, ident, ev):
    mounts = await _disk_mount_series(db, ident, WINDOW_MIN)
    if not mounts:
        return "skip", [], "暂无分区使用率指标（可用磁盘分区卡查看快照）"
    worst, worst_s = None, {"latest": -1}
    for m in mounts:
        s = _stats(await _agg_series(db, ident, m, WINDOW_MIN))
        if (s["latest"] or -1) > worst_s["latest"]:
            worst, worst_s = m, s
    st = _thres(worst_s)
    mnt = "/" + worst.split(".")[2] if worst and worst.startswith("disk.") and len(worst.split(".")) > 3 else (worst or "")
    ev.append({"token": "disk_worst", "detail": f"{mnt}: latest={worst_s['latest']}% p95={worst_s['p95']}%"})
    con = {"fail": f"磁盘 {mnt} 使用率 {worst_s['latest']}%——用「大文件/大目录定位」清理（可让 AI 诊断执行 du 分析）",
           "warn": f"磁盘 {mnt} 使用率 {worst_s['latest']}%，接近告警线",
           "pass": "磁盘水位正常"}[st]
    return st, ev, con


async def check_load_mismatch(db, ident, ev):
    l5 = _stats(await _agg_series(db, ident, "load.5", WINDOW_MIN))
    cpu = _stats(await _agg_series(db, ident, "cpu.percent", WINDOW_MIN))
    if l5["avg"] is None or cpu["avg"] is None:
        return "skip", [], "暂无 load/cpu 指标"
    ev.append({"token": "load5_vs_cpu", "detail": f"load5_avg={l5['avg']} cpu_avg={cpu['avg']}%"})
    if l5["avg"] >= 8:
        return "fail", ev, f"系统负载持续偏高（load5_avg={l5['avg']}），优先排查 IO 等待与进程排队"
    if l5["avg"] >= 4 and cpu["avg"] < 60:
        return "warn", ev, "负载高但 CPU 不忙——典型的 IO 等待/不可中断阻塞，建议结合磁盘 IO 与网络页签排查"
    return "pass", ev, "负载与 CPU 匹配正常"


async def check_disk_io(db, ident, ev):
    names = ring.list_metric_names(ident, "disk.io.")
    iops = [n for n in names if n.endswith("_iops")]
    bps = [n for n in names if n.endswith("_bps")]
    if not iops and not bps:
        return "skip", [], "暂无磁盘 IO 指标"
    worst_iops, worst_iops_s = None, {"p95": -1}
    for m in iops:
        s = _stats(await _agg_series(db, ident, m, WINDOW_MIN))
        if (s["p95"] or -1) > worst_iops_s["p95"]:
            worst_iops, worst_iops_s = m, s
    if worst_iops_s["p95"] > 1000:
        ev.append({"token": "io_iops_p95", "detail": f"{worst_iops}: p95={worst_iops_s['p95']} IOPS"})
        return "warn", ev, f"磁盘 IO 打满迹象（{worst_iops} p95 {worst_iops_s['p95']} IOPS）——考虑限流或升级云盘"
    return "pass", ev, "磁盘 IO 正常"


async def check_tcp(db, ident, ev):
    out = {}
    for st_name in ("time_wait", "close_wait", "established"):
        s = _stats(await _agg_series(db, ident, f"net.tcp.{st_name}", WINDOW_MIN))
        if s["latest"] is not None:
            out[st_name] = s["latest"]
            ev.append({"token": f"tcp_{st_name}", "detail": f"latest={s['latest']}"})
    if not out:
        return "skip", [], "暂无 TCP 连接指标"
    if out.get("close_wait", 0) > 500:
        return "warn", ev, "CLOSE_WAIT 偏高——应用未关闭对端连接（连接泄漏），检查未 close 的 socket"
    if out.get("time_wait", 0) > 10000:
        return "warn", ev, "TIME_WAIT 偏高——短连接压测/高频请求，考虑连接复用与端口回收参数"
    return "pass", ev, "TCP 连接状态正常"


async def check_probe_failed(db, ident, ev, host_id):
    from app.models.collect import Probe
    from sqlalchemy import select as _sel
    probes = (await db.execute(_sel(Probe).where(Probe.host_id == host_id))).scalars().all()
    if not probes:
        return "skip", [], "该主机未配置探活"
    downs = [p for p in probes if (p.last_status or "") != "up"]
    for p in downs:
        ev.append({"token": "probe_down", "detail": f"{p.name}({p.type}) 目标 {p.target} 状态 {p.last_status}"})
    if downs:
        return "fail", ev, f"{len(downs)} 个探活失败：{'、'.join(p.name for p in downs[:5])}"
    return "pass", ev, f"{len(probes)} 个探活全部正常"


async def check_net_errors(db, ident, ev):
    names = [n for n in ring.list_metric_names(ident, "net.") if n.split(".")[-1] in ("errin", "errout", "dropin", "dropout")]
    if not names:
        return "skip", [], "暂无网卡错误/丢包指标"
    bad = []
    for m in names:
        s = _stats(await _agg_series(db, ident, m, WINDOW_MIN))
        if (s["avg"] or 0) > 1:
            bad.append(f"{m.split('.', 1)[1]}≈{s['avg']}/s")
    if bad:
        for b in bad:
            ev.append({"token": "net_error", "detail": b})
        return "warn", ev, f"网卡存在错误/丢包：{'; '.join(bad[:4])}"
    return "pass", ev, "网卡无错误包与丢包"


async def check_inode(db, ident, ev):
    names = ring.list_metric_names(ident, "disk.inode.")
    if not names:
        return "skip", [], "暂无 inode 指标（Linux agent 才采集）"
    worst, worst_v = None, -1
    for m in names:
        s = _stats(await _agg_series(db, ident, m, WINDOW_MIN))
        if (s["latest"] or -1) > worst_v:
            worst, worst_v = m, s["latest"]
    ev.append({"token": "inode_worst", "detail": f"{worst}: {worst_v}%"})
    if worst_v >= 85:
        return "fail", ev, f"inode 耗尽风险（{worst} {worst_v}%）——海量小文件，清理或重建文件系统"
    if worst_v >= 70:
        return "warn", ev, f"inode 使用偏高（{worst} {worst_v}%）"
    return "pass", ev, "inode 充足"


async def check_service_down(db, ident, ev, host_id):
    from sqlalchemy import select as _sel
    from app.models.svc import Service
    rows = (await db.execute(_sel(Service).where(Service.host_id == host_id))).scalars().all()
    downs = [s for s in rows if (s.status or "") == "down"]
    for s in downs:
        spec = s.spec_json or {}
        target = spec.get("unit") or spec.get("container") or spec.get("port") or "-"
        ev.append({"token": "service_down", "detail": f"{s.name}（{s.type}，target={target}）"})
    if downs:
        return "fail", ev, f"{len(downs)} 个服务不可用：{'、'.join(s.name for s in downs[:5])}"
    if rows:
        return "pass", ev, f"{len(rows)} 个服务全部在线"
    return "skip", [], "未声明服务监控"


# ---------- 注册表（scope 与阿里云诊断分类对齐：性能/存储/网络/系统/服务） ----------

CHECKS = [
    ("cpu_high", "CPU 使用率", "性能", check_cpu_high),
    ("mem_high", "内存与 OOM 追溯", "性能", check_mem_high),
    ("load_mismatch", "系统负载匹配", "性能", check_load_mismatch),
    ("disk_full", "磁盘水位", "存储", check_disk_full),
    ("disk_io", "磁盘 IO 饱和", "存储", check_disk_io),
    ("inode", "inode 水位", "存储", check_inode),
    ("tcp_anomaly", "TCP 连接健康", "网络", check_tcp),
    ("net_errors", "网卡错误与丢包", "网络", check_net_errors),
    ("probe", "探活状态", "服务", None),          # 需要 host_id，见 diagnose_host 分发
    ("service_down", "服务可用性", "服务", None),  # 同上
]


async def diagnose_host(db, host_id: str, ident: str, minutes: int = WINDOW_MIN,
                        emitter=None) -> dict:
    """执行一次健康诊断（规则轨）。返回 summary。emitter(kind, payload) 可选（SSE 推流）。"""
    from app.models.health import HealthReport

    started = datetime.utcnow()
    report = HealthReport(host_ident=ident, status="running", source="rule",
                          window_start=started - timedelta(minutes=minutes), window_end=started)
    db.add(report)
    await db.commit()
    await db.refresh(report)
    if emitter:
        await emitter("diag_start", {"report_id": report.id, "host": ident, "checks": len(CHECKS)})

    items, counts = [], {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for idx, (cid, title, scope, fn) in enumerate(CHECKS, 1):
        t0 = time.time()
        status, evidence, conclusion = "error", [], f"检查项异常"
        try:
            if cid == "probe":
                status, evidence, conclusion = await check_probe_failed(db, ident, [], host_id)
            elif cid == "service_down":
                status, evidence, conclusion = await check_service_down(db, ident, [], host_id)
            else:
                status, evidence, conclusion = await fn(db, ident, [])
        except Exception as e:  # noqa: BLE001 —— 单项失败不中断整份报告
            log.exception("健康检查 %s 失败 host=%s", cid, ident)
            status, evidence, conclusion = "error", [{"token": "error", "detail": str(e)[:200]}], f"检查执行异常：{type(e).__name__}"
        item = {"id": cid, "title": title, "scope": scope, "status": status,
                "evidence": evidence[:8], "conclusion": conclusion,
                "elapsed_ms": int((time.time() - t0) * 1000)}
        items.append(item)
        counts[status if status in counts else "skip"] = counts.get(status if status in counts else "skip", 0) + 1
        if emitter:
            await emitter("check_done", {"report_id": report.id, "index": idx, **item})

    overall = "unhealthy" if counts["fail"] else ("attention" if counts["warn"] else "healthy")
    report.summary = {"overall": overall, **counts}
    report.items = items
    report.status = "done"
    report.finished_at = datetime.utcnow()
    await db.commit()
    if emitter:
        await emitter("diag_done", {"report_id": report.id, "summary": report.summary})
    return {"report_id": report.id, "summary": report.summary, "items": items}