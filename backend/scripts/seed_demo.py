"""演示种子数据（FR-G2）：7 天历史指标 + 规则 + 历史事件 + 案例卡，保证首屏不空。

用法（平台已启动）：
    python scripts/seed_demo.py                      # 默认 7 天 × 4 主机，含历史事件与案例卡
    python scripts/seed_demo.py --days 1             # 快速种子（1 天）
    python scripts/seed_demo.py --skip-metrics       # 只种规则/事件/案例卡
    python scripts/seed_demo.py --base http://127.0.0.1:8848 --user admin --pass admin123

设计说明：
- 指标走 **Agent 上报协议**（与真实采集同一条链路），5 分钟粒度、含昼夜周期波动与缓慢磁盘增长。
- 明细只保留 2h（内存环形），更早的由 sched 的聚合任务落 metric_agg：**种子后等一个聚合周期（≤60s）
  或手动跑一次聚合**，"超窗走聚合"的查询路径才有数据。
- 历史事件与案例卡直接落库（含向量），用于问题看板与 search_cases 召回演示。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HOSTS = [
    ("demo-web-01", "10.0.1.11", "Ubuntu 22.04", {"cpu": (30, 55), "mem": (55, 70), "disk": (40, 55)}),
    ("demo-web-02", "10.0.1.12", "Ubuntu 22.04", {"cpu": (25, 50), "mem": (50, 65), "disk": (38, 52)}),
    ("demo-db-01", "10.0.2.21", "Debian 12", {"cpu": (40, 60), "mem": (65, 80), "disk": (70, 88)}),
    ("demo-cache-01", "10.0.3.31", "Rocky 9", {"cpu": (12, 30), "mem": (35, 50), "disk": (30, 45)}),
]

HISTORICAL_INCIDENTS = [
    {"title": "demo-db-01 磁盘使用率持续增长", "severity": 2, "host": "demo-db-01",
     "confidence": "LIKELY", "root_cause": "数据库慢日志与系统日志未轮转，7 天内磁盘从 70% 涨到 88%",
     "symptom": "磁盘使用率单调上升，同期无发布变更",
     "resolution": "开启 logrotate 并清理历史慢日志；将磁盘阈值从 90 调整为 85 提前预警"},
    {"title": "demo-web-01 整点突发 CPU 高负载", "severity": 2, "host": "demo-web-01",
     "confidence": "LIKELY", "root_cause": "多个定时任务在整点集中执行，短时争抢 CPU",
     "symptom": "每小时整点 CPU 冲高至 95% 以上，持续约 3 分钟后自愈",
     "resolution": "定时任务错峰到非整点，并加并发锁避免叠加执行"},
    {"title": "demo-cache-01 连接数打满导致接口超时", "severity": 1, "host": "demo-cache-01",
     "confidence": "CONFIRMED", "root_cause": "应用侧连接未归还，连接池被占满",
     "symptom": "连接数逼近上限，多个接口大面积超时，重启后短时恢复又复发",
     "resolution": "修复未释放连接的缺陷；连接池开启泄漏检测与空闲回收"},
]


def call(base, method, path, token=None, body=None, raw=False, headers=None):
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            text = r.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")
    return text if raw else json.loads(text)


def register_hosts(base, token):
    idents = []
    for name, ip, os_, ranges in HOSTS:
        inst = call(base, "GET", "/api/ops/dev/mint-install-token", token)["data"]["install_token"]
        reg = call(base, "POST", "/api/ops/agents/register", body={
            "fingerprint": f"demo-{name}", "hostname": name, "ip": ip, "os": os_, "install_token": inst})["data"]
        idents.append((name, ip, reg["ident"], reg["token"], ranges))
    print(f"✓ 注册主机 {len(idents)} 台")
    return idents


def seed_metrics(base, token, days: int):
    idents = register_hosts(base, token)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    step, total = 5, days * 24 * 60 // 5
    for k in range(total, 0, -1):
        t = now - timedelta(minutes=k * step)
        ts = t.isoformat().replace("+00:00", "Z")
        diurnal = 1 + 0.25 * math.sin((t.hour - 9) / 24 * 2 * math.pi)   # 白天高、夜间低
        for name, ip, ident, atok, ranges in idents:
            lo, hi = ranges["cpu"]
            cpu = min(99, max(3, random.uniform(lo, hi) * diurnal + random.gauss(0, 3)))
            mem = min(99, max(5, random.uniform(*ranges["mem"]) + random.gauss(0, 2)))
            disk = min(99, max(5, ranges["disk"][0] + (ranges["disk"][1] - ranges["disk"][0]) * (1 - k / total)
                               + random.gauss(0, 0.6)))
            call(base, "POST", "/api/ops/agents/metrics", body={
                "ts": ts,
                "metrics": [
                    {"metric": "cpu.percent", "value": round(cpu, 1)},
                    {"metric": "mem.percent", "value": round(mem, 1)},
                    {"metric": "disk.percent", "value": round(disk, 1)},
                    {"metric": "load.1", "value": round(cpu / 12, 2)},
                ],
                "probes": [
                    {"name": "业务HTTP", "type": "http", "target": f"http://{ip}:80/health",
                     "status": "down" if name == "demo-db-01" else "up", "latency_ms": random.randint(3, 45)},
                    {"name": "业务端口", "type": "tcp", "target": f"{ip}:80",
                     "status": "down" if name == "demo-db-01" else "up", "latency_ms": random.randint(1, 20)},
                    {"name": "HTTPS证书", "type": "https-cert", "target": f"{ip}:443",
                     "status": "up", "latency_ms": random.randint(12, 40)},
                ]}, raw=True, headers={"X-Ident": ident, "X-Token": atok})
        if k % 200 == 0:
            print(f"  指标回填进度：{total - k}/{total}")
    print(f"✓ 回填 {days} 天指标（{step} 分钟粒度 × {len(idents)} 主机 × 4 指标）")
    return idents


def seed_rules(base, token):
    existing = {r["name"] for r in call(base, "GET", "/api/ops/alert-rules", token)["data"]["items"]}
    specs = [
        {"name": "演示-CPU高负载", "scope": "metric", "metric": "cpu.percent", "expr": ">85", "severity": 2,
         "eval_interval_s": 15, "window_s": 300, "algorithm": "threshold", "recover_duration_s": 60,
         "runbook_url": "runbook://ops/cpu-high"},
        {"name": "演示-磁盘使用率", "scope": "metric", "metric": "disk.percent", "expr": ">85", "severity": 2,
         "eval_interval_s": 30, "window_s": 600, "algorithm": "threshold", "recover_duration_s": 120},
        {"name": "演示-内存异常检测(3σ)", "scope": "metric", "metric": "mem.percent", "expr": ">0", "severity": 3,
         "eval_interval_s": 30, "window_s": 300, "algorithm": "3sigma", "recover_duration_s": 60},
        {"name": "演示-探活失败", "scope": "probe", "metric": "", "expr": ">0", "severity": 1,
         "eval_interval_s": 15, "window_s": 300, "recover_duration_s": 60},
        {"name": "演示-证书30天内到期", "scope": "probe", "metric": "HTTPS证书", "expr": "<30", "severity": 2,
         "eval_interval_s": 60, "window_s": 300},
        # FR-415：服务状态告警——把"服务监控"的判定接到通知/事件链上（否则服务红了也没人知道）
        {"name": "演示-服务不可用", "scope": "service", "metric": "", "expr": ">1", "severity": 1,
         "eval_interval_s": 30, "window_s": 300, "for_duration_s": 60, "recover_duration_s": 60},
    ]
    made = 0
    for s in specs:
        if s["name"] not in existing:
            call(base, "POST", "/api/ops/alert-rules", token, s)
            made += 1
    print(f"✓ 规则新建 {made} 条（已存在 {len(existing)} 条）")


def seed_llm_config(base, token):
    """若 DB 无 LLM 配置，则用 .env 的兜底配置建一条（api_key 加密落库），让设置页可见可测。"""
    import os

    if call(base, "GET", "/api/llm-configs", token)["data"]:
        print("✓ LLM 配置已存在")
        return
    key = os.getenv("LLM_API_KEY", "")
    if not key:
        print("· 未提供 LLM_API_KEY，跳过 LLM 配置种子（AI 功能将回落到空配置并明确报错）")
        return
    call(base, "POST", "/api/llm-configs", token, {
        "name": "demo-minimax", "provider": "openai-compatible",
        "base_url": os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"),
        "api_key": key,
        "model": os.getenv("LLM_MODEL_MAIN", "MiniMax-M3"),
        "fast_model": os.getenv("LLM_MODEL_FAST", "MiniMax-M2.7-highspeed"),
        "extra_config": {"embedding_model": "embo-01", "temperature": 0.2},
        "enabled": True, "is_default": True})
    print("✓ 已写入 LLM 配置（api_key AES-GCM 加密落库）")


def seed_history(base, token, ident_map=None):
    """历史事件 + 案例卡（直连库；含向量）。

    ident_map: {主机名 → Agent 上报的 ident}。历史事件里必须写**真实 ident**，
    否则对该事件跑 AI 诊断时，工具按 host_ident 取证会全部 NO_DATA（实测会出现
    "主机未注册"的尴尬报告）。缺省时可回退主机名（工具侧已支持 ident/name 互认）。
    """
    sys.path.insert(0, ".")
    import asyncio

    from sqlalchemy import text

    from app.db.session import SessionLocal, dispose_all_engines
    from app.services.llm_gateway import embed

    async def _run():
        async with SessionLocal() as db:
            for h in HISTORICAL_INCIDENTS:
                if (await db.execute(text("SELECT id FROM incidents WHERE title = :t"), {"t": h["title"]})).scalar():
                    continue
                r = await db.execute(text(
                    "INSERT INTO incidents (title, severity, status, alert_hashes, source_key, host_ident, "
                    "cluster_reason, ai_report_md, root_cause, confidence, created_at, resolved_at) "
                    "VALUES (:t, :s, 'resolved', :ah, :sk, :host, 'demo_seed', :rep, :rc, :conf, :ca, :ca)"),
                    {"t": h["title"], "s": h["severity"], "ah": json.dumps([]),
                     "sk": f"seed{random.getrandbits(96):024x}"[:32],
                     "host": (ident_map or {}).get(h["host"], h["host"]),
                     "rep": (f"## Summary\n\n{h['root_cause']}\n\n## Evidence\n\n（演示种子数据）\n\n"
                             f"## Reasoning\n\n{h['symptom']}\n\n## What I ruled out\n\n（示例）\n"),
                     "rc": h["root_cause"], "conf": h["confidence"],
                     "ca": datetime.utcnow() - timedelta(days=random.randint(1, 6))})
                await db.commit()
                iid = r.lastrowid
                vec = None
                try:
                    vec = (await embed(db, [f"{h['symptom']} {h['root_cause']}"[:1500]]))[0]
                except Exception:  # noqa: BLE001 —— 向量不可用则仅写文本
                    pass
                await db.execute(text(
                    "INSERT INTO case_cards (incident_id, symptom, root_cause, resolution_md, tags, embedding) "
                    "VALUES (:i, :s, :rc, :res, :t, :e)"),
                    {"i": iid, "s": h["symptom"], "rc": h["root_cause"], "res": h["resolution"],
                     "t": json.dumps(["seed", h["confidence"]]), "e": json.dumps(vec) if vec else None})
                await db.commit()
                print(f"  + 历史事件 #{iid} {h['title'][:28]}（案例卡{'含向量' if vec else '仅文本'}）")
        await dispose_all_engines()

    asyncio.run(_run())
    print(f"✓ 历史事件与案例卡（{len(HISTORICAL_INCIDENTS)} 组上限）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8848")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--pass", dest="pwd", default="admin123")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--skip-metrics", action="store_true")
    ap.add_argument("--skip-history", action="store_true")
    args = ap.parse_args()
    try:  # 读取 backend/.env 的 LLM_* 兜底配置（若存在）
        from dotenv import load_dotenv

        load_dotenv(".env")
    except Exception:  # noqa: BLE001
        pass

    tok = call(args.base, "POST", "/api/auth/login",
               body={"username": args.user, "password": args.pwd})["data"]["access_token"]
    print("✓ 登录成功")
    idents = []
    if not args.skip_metrics:
        idents = seed_metrics(args.base, tok, args.days)
    seed_rules(args.base, tok)
    seed_llm_config(args.base, tok)
    if not args.skip_history:
        seed_history(args.base, tok, {name: ident for name, _ip, ident, _tok, _r in idents})
    print(f"\n完成。打开 {args.base} 登录查看；指标明细仅保留 2h，"
          "早于 2h 的点等一个聚合周期（≤60s）后从 metric_agg 读出。")


if __name__ == "__main__":
    sys.exit(main())
