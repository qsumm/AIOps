"""定时巡检与健康报告（FR-D5；03 §1.16）。

设计要点：
- InspectionJob = 一段 cron（APScheduler 注册）+ 巡检项清单 + 目标主机范围
- run()：逐项取数（全只读）→ 汇总异常项 → 生成报告（模板表格 + fast 模型润色段落）
       → 异常项逐条走收敛（cluster → incident，进入问题看板）
- 巡检项固定清单（磁盘>85% / 内存>90% / 负载>核数 / 探活失败 / 证书<15天 / 主机离线）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.collect import Probe
from app.models.host import Host
from app.models.ops import Inspection
from app.services import cluster as cluster_svc
from app.services.llm_gateway import call_llm
from app.store import ring

log = logging.getLogger("inspect")

# 巡检项（FR-D5：磁盘水位 / 内存 / 异常进程 / 探活成功率 / 证书到期 / 系统负载）
ITEMS = [
    {"key": "disk", "label": "磁盘使用率 > 85%", "metric": "disk.percent", "op": ">", "threshold": 85},
    {"key": "mem", "label": "内存使用率 > 90%", "metric": "mem.percent", "op": ">", "threshold": 90},
    {"key": "cpu", "label": "CPU 使用率 > 90%", "metric": "cpu.percent", "op": ">", "threshold": 90},
    {"key": "load", "label": "系统负载 load.1 偏高（> 8）", "metric": "load.1", "op": ">", "threshold": 8},
    {"key": "cert", "label": "证书剩余天数 < 15", "kind": "cert", "op": "<", "threshold": 15},
    {"key": "probe", "label": "探活成功率 < 90%（或存在失败探活）", "kind": "probe", "op": "<", "threshold": 90},
    {"key": "proc", "label": "异常进程（单进程 CPU > 80% 或 RSS > 2GB）", "kind": "proc"},
    {"key": "offline", "label": "主机离线", "kind": "host", "op": "==", "threshold": "down"},
]


def _is_platform_host_row(row: dict, hosts: list[Host], platform_mod) -> bool:
    """判断这一行是不是"平台自身主机"（进程/日志可读）。"""
    ident = row.get("ident")
    for h in hosts:
        if h.ident == ident:
            name = (h.name or "").lower()
            return name in (platform_mod.node().lower(), "localhost") or (h.ip or "") in ("127.0.0.1", "::1")
    return False


async def _targets(scope: dict) -> list[Host]:
    async with SessionLocal() as db:
        names = (scope or {}).get("hosts") or "all"
        q = select(Host)
        if isinstance(names, list) and names:
            q = q.where(Host.ident.in_(names))
        return list((await db.execute(q)).scalars().all())


async def run_inspection(inspection_id: int | None = None, *, create_incidents: bool = True,
                         quota_role: str = "") -> dict:
    """执行一次巡检。inspection_id 为空则跑一次"即时巡检"（不落任务表）。"""
    started = datetime.utcnow()
    async with SessionLocal() as db:
        job = await db.get(Inspection, inspection_id) if inspection_id else None
        scope = (job.target_scope if job else {}) or {}
        if job:
            job.last_status = "running"
            job.last_run_at = started
            await db.commit()
        name = job.name if job else "即时巡检"

    hosts = await _targets(scope)
    anomalies: list[dict] = []
    rows: list[dict] = []

    for h in hosts:
        row = {"host": h.name or h.ident[:8], "ident": h.ident, "status": h.status, "items": []}
        # 指标类
        for item in ITEMS:
            if item.get("kind") or "metric" not in item:
                continue   # cert/probe/proc/host/offline 等非"取时序"项，另在各分支处理
            pts = ring.query_points(h.ident, item["metric"], datetime.utcnow() - timedelta(minutes=15), datetime.utcnow())
            latest = pts[-1][1] if pts else None
            bad = latest is not None and latest > item["threshold"]
            row["items"].append({"key": item["key"], "value": None if latest is None else round(latest, 1), "bad": bad})
            if bad:
                anomalies.append({"host": h.ident, "host_name": h.name, "item": item["key"],
                                  "label": item["label"], "value": round(latest, 1)})
        # 主机离线
        if h.status == "down":
            row["items"].append({"key": "offline", "value": "down", "bad": True})
            anomalies.append({"host": h.ident, "host_name": h.name, "item": "offline", "label": "主机离线", "value": "down"})
        rows.append(row)

    # 探活 / 证书类（跨主机）
    async with SessionLocal() as db:
        idents = {h.ident: (h.name or h.ident[:8]) for h in hosts}
        probes = (await db.execute(select(Probe, Host.ident).join(Host, Probe.host_id == Host.id)
                                   .where(Probe.enabled.is_(True)))).all()
    per_host_probe: dict[str, list[int]] = {}   # ident → [up 数, 总数]
    for p, ident in probes:
        if ident not in idents and (scope.get("hosts") not in (None, "all", [])):
            continue
        stat = per_host_probe.setdefault(ident, [0, 0])
        stat[1] += 1
        if p.last_status == "up":
            stat[0] += 1
        if p.last_status == "down":
            anomalies.append({"host": ident, "host_name": idents.get(ident, ident[:8]), "item": "probe",
                              "label": "探活失败", "value": f"{p.name} → {p.target}"})
        if p.type == "https-cert" and (p.last_latency_ms or 9999) < 15:
            anomalies.append({"host": ident, "host_name": idents.get(ident, ident[:8]), "item": "cert",
                              "label": "证书临期", "value": f"{p.name} 剩余 {p.last_latency_ms} 天"})

    # 探活成功率（按主机汇总；低于 90% 记异常——比"存在失败探活"更能反映整体健康）
    for ident, (up_n, total) in per_host_probe.items():
        rate = round(up_n * 100 / total, 1) if total else 100.0
        for row in rows:
            if row["ident"] == ident:
                row["items"].append({"key": "probe_rate", "value": f"{rate}%", "bad": rate < 90})
        if total and rate < 90:
            anomalies.append({"host": ident, "host_name": idents.get(ident, ident[:8]), "item": "probe_rate",
                              "label": "探活成功率低", "value": f"{up_n}/{total} = {rate}%"})

    # 异常进程：**仅平台自身主机可查**（V1 Agent 不上报远程进程）。
    # 远程主机在报告里标注"未检查"，不假装查过——与诊断工具的诚实边界一致。
    import platform as _pf
    import subprocess

    for row in rows:
        if not _is_platform_host_row(row, hosts, _pf):
            row["items"].append({"key": "proc", "value": "未检查（远程主机）", "bad": False, "skipped": True})
            continue
        try:
            out = subprocess.run(["ps", "-Ao", "pcpu,rss,comm", "-r"], capture_output=True, text=True, timeout=8)
            worst = None
            for line in out.stdout.strip().splitlines()[1:6]:
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                cpu, rss_kb, comm = float(parts[0]), int(parts[1]), parts[2]
                if cpu > 80 or rss_kb > 2 * 1024 * 1024:
                    worst = f"{comm.strip()} cpu={cpu:.0f}% rss={rss_kb // 1024}MB"
                    break
            row["items"].append({"key": "proc", "value": worst or "正常", "bad": bool(worst)})
            if worst:
                anomalies.append({"host": row["ident"], "host_name": row["host"], "item": "proc",
                                  "label": "异常进程", "value": worst})
        except Exception:  # noqa: BLE001 —— 取进程失败不算异常，但要如实标注
            row["items"].append({"key": "proc", "value": "未检查（取进程失败）", "bad": False, "skipped": True})

    report_md = await _render_report(name, rows, anomalies, started, quota_role)

    created = 0
    if create_incidents:
        for a in anomalies[:20]:
            try:
                r = await cluster_svc.cluster_alert({
                    "hash": _inspect_hash(a), "rule_name": f"巡检-{a['label']}", "metric": a["item"],
                    "host_ident": a["host"], "severity": 2 if a["item"] in ("probe", "offline", "cert") else 3,
                    "trigger_value": a["value"], "trigger_time": str(started), "ai_summary": "",
                })
                created += 1
            except Exception:  # noqa: BLE001 —— 单条失败不影响巡检
                log.exception("巡检异常项收敛失败：%s", a)

    async with SessionLocal() as db:
        if job:
            job = await db.get(Inspection, inspection_id)
            job.last_status = "ok"
            job.report_md = report_md
            job.findings = anomalies
            job.incidents_created = created
            job.last_run_at = started
            await db.commit()
    return {"name": name, "hosts": len(hosts), "anomalies": anomalies, "report_md": report_md,
            "incidents_created": created, "elapsed_s": round((datetime.utcnow() - started).total_seconds(), 1)}


def _inspect_hash(a: dict) -> str:
    import hashlib

    return hashlib.md5(f"inspect|{a['host']}|{a['item']}|{a.get('value')}".encode()).hexdigest()


async def _render_report(name: str, rows: list[dict], anomalies: list[dict], ts: datetime,
                         quota_role: str = "") -> str:
    """模板表格 + fast 模型润色段落（LLM 不可用则纯模板）。"""
    head = f"""# 巡检报告 · {name}

- 巡检时间：{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC
- 主机数：{len(rows)}　异常项：{len(anomalies)}
- 结论：{'全部正常' if not anomalies else '发现 ' + str(len(anomalies)) + ' 项异常'}
"""
    table = ["\n## 主机巡检明细\n",
             "| 主机 | 状态 | CPU% | 内存% | 磁盘% | 负载1m | 探活成功率 | 异常进程 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        def g(k):
            v = next((i["value"] for i in r["items"] if i["key"] == k), None)
            bad = next((i["bad"] for i in r["items"] if i["key"] == k), False)
            return f"{v}（超阈值）" if (v is not None and bad) else (f"{v}" if v is not None else "-")

        table.append(f"| {r['host']} | {r['status']} | {g('cpu')} | {g('mem')} | {g('disk')} | "
                     f"{g('load')} | {g('probe_rate')} | {g('proc')} |")
    if anomalies:
        table.append("\n## 异常项\n")
        for a in anomalies:
            table.append(f"- **{a['host_name']}** · {a['label']}：{a['value']}")
    body = "\n".join(table)

    async with SessionLocal() as db:
        try:
            r = await call_llm(db, "inspect", [
                {"role": "system", "content": "你是运维巡检报告撰写人。用不超过 200 字的中文总结本次巡检的关键风险与建议关注点，"
                                              "只依据给出的数据，不要臆测，不要编造未提供的数据。"},
                {"role": "user", "content": f"主机数 {len(rows)}，异常项 {len(anomalies)}。" +
                 "；".join(f"{a['host_name']} {a['label']}({a['value']})" for a in anomalies[:15])},
            ], model_role="fast", quota_role=quota_role)
            return head + body + f"\n\n## 巡检结论（AI 摘要）\n\n{r.content.strip()}\n"
        except Exception:  # noqa: BLE001 —— LLM 不可用：纯模板报告
            return head + body + "\n\n> 注：AI 摘要不可用（LLM 未配置或不可达），本报告为纯数据模板。\n"


def default_inspection() -> Inspection:
    """首次启动种子：每日全量主机巡检。

    cron 由 `CronTrigger.from_crontab(..., timezone="UTC")` 解释（D37）。
    取值 `0 1 * * *` = UTC 01:00 = **北京时间 09:00**（运维习惯的早上九点）。
    """
    return Inspection(name="每日主机巡检", cron_expr="0 1 * * *",
                      target_scope={"hosts": "all", "items": [i["key"] for i in ITEMS]}, enabled=True)
