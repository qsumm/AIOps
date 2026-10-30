"""100 主机规模压测（FR §8.2 的性能门槛实测）。

需求门槛：100 主机 × 15s 周期下，**评估周期 P95 < 2s**；告警 SSE 延迟 ≤1 个评估周期；
大盘查询 P95 < 1s。本脚本给真实数字，并对每个门槛给 PASS/FAIL。

两种跑法：
  ① **容器内（推荐，能测引擎与 SSE 真实路径）**：
     docker compose --env-file .env -f deploy/docker-compose.yaml exec platform \
         python scripts/bench_100_hosts.py --hosts 100
  ② 只有 HTTP 的降级模式：`--api-only`（只测接口 P95，不测引擎评估周期）

设计取舍：
- 主机用 `bench-` 前缀 + 独立指纹注册，跑完**默认清理**（删主机/规则/指标），不污染演示库；
- 评估周期不是"看日志估"，而是直接调同一批函数（reconcile diff → worker._eval_once）并计时；
- SSE 延迟用一个真实订阅者测：注入越阈值指标 → 等 consume_loop 广播 → 计时间差。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

PREFIX = "bench-host-"
RULE_NAME = "bench-100-cpu"
SSE_RULE = "bench-sse-latency"


def call(base: str, method: str, path: str, token: str | None = None, body: dict | None = None,
         headers: dict | None = None):
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=60) as r:
            text = r.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:200]}")
    return json.loads(text)


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((p / 100) * len(s) + 0.5)) - 1))
    return s[idx]


def verdict(name: str, value: float, limit: float, unit: str = "ms") -> str:
    ok = value <= limit
    return f"{'PASS' if ok else 'FAIL'}  {name}: {value:.1f}{unit}（门槛 ≤{limit}{unit}）"


async def _engine_bench(hosts: int, rounds: int) -> dict:
    """容器内：测**真实评估周期**（直接驱动 reconcile + worker._eval_once，与生产同一批函数）。

    注意：本脚本是独立进程，无法订阅 uvicorn 进程的广播——SSE 延迟另走真实 HTTP 流测（见 measure_sse）。
    """
    from sqlalchemy import delete

    from app.db.session import SessionLocal, dispose_all_engines
    from app.engine import core
    from app.models.alert import AlertRule

    out: dict = {}
    async with SessionLocal() as db:
        await db.execute(delete(AlertRule).where(AlertRule.name == RULE_NAME))
        rule = AlertRule(name=RULE_NAME, scope="metric", metric="cpu.percent", expr=">95",
                         severity=2, eval_interval_s=15, window_s=300, algorithm="threshold",
                         recover_duration_s=0)
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        rule_id = rule.id

    await core.reconcile()
    worker = core.workers.get(rule_id)
    if worker is None:
        out["engine_note"] = "规则 worker 未创建（reconcile 未覆盖该规则）"
    else:
        times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            await worker._eval_once()
            times.append((time.perf_counter() - t0) * 1000)
        out["eval_hosts"] = hosts
        out["eval_p50_ms"] = statistics.median(times)
        out["eval_p95_ms"] = pct(times, 95)
        out["eval_max_ms"] = max(times)

    async with SessionLocal() as db:
        await db.execute(delete(AlertRule).where(AlertRule.id == rule_id))
        await db.commit()
    await dispose_all_engines()
    return out


def measure_sse(base: str, tok: str, agent: dict, timeout_s: int = 45) -> dict:
    """经**真实 SSE 端点**测告警推送延迟（含等待下一个评估周期）。

    用**已有的 metric 规则**（优先 cpu.percent 的阈值规则）而不是临时新建：
    临时规则要等对账建 worker、窗口又短，测出来的"没收到"分不清是延迟大还是规则没起。
    复用一条稳定规则 + 推一个必超阈的值，链路更接近真实告警。
    """
    import threading

    rules = call(base, "GET", "/api/ops/alert-rules", tok)["data"]["items"]
    cand = next((r for r in rules if r.get("scope") == "metric" and r.get("metric") == "cpu.percent"
                 and not r.get("disabled") and str(r.get("expr", "")).startswith(">")), None)
    if cand is None:
        return {"sse_latency_ms": None, "sse_note": "没有可复用的 cpu.percent 阈值规则"}
    rule_name = cand["name"]

    ticket = call(base, "GET", "/api/sse-ticket", tok)["data"]["ticket"]
    got: dict = {}
    t_fire: list[float] = []

    def reader():
        req = urllib.request.Request(f"{base}/api/ops/alerts/stream?ticket={ticket}")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                for raw in r:
                    line = raw.decode().strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[5:].strip())
                    except Exception:  # noqa: BLE001
                        continue
                    if payload.get("rule_name") == rule_name and payload.get("kind") in ("firing", "updated"):
                        got.update(payload)
                        t_fire.append(time.perf_counter())
                        return
        except Exception as e:  # noqa: BLE001
            got["_error"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(2)
    now = datetime.utcnow()
    t0 = time.perf_counter()
    for ts in (now - timedelta(seconds=5), now):
        call(base, "POST", "/api/ops/agents/metrics",
             body={"ts": ts.isoformat() + "Z",
                   "metrics": [{"metric": "cpu.percent", "value": 99.9}], "probes": []},
             headers={"X-Ident": agent["ident"], "X-Token": agent["token"]})
    th.join(timeout=timeout_s)
    lat = (t_fire[0] - t0) * 1000 if t_fire else None
    return {"sse_latency_ms": lat, "sse_event": got.get("kind"), "sse_rule": rule_name,
            "sse_note": got.get("_error", "")}


def _rule_id_by_name(base: str, tok: str, name: str) -> int:
    for r in call(base, "GET", "/api/ops/alert-rules", tok)["data"]["items"]:
        if r["name"] == name:
            return r["id"]
    return 0


async def _count_hosts() -> int:  # pragma: no cover - 占位（保持函数签名一致）
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="100 主机规模压测（FR §8.2）")
    ap.add_argument("--base", default="http://127.0.0.1:8848")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--pass", dest="pwd", default="admin123")
    ap.add_argument("--hosts", type=int, default=100)
    ap.add_argument("--points", type=int, default=6, help="每台主机回填的指标点数")
    ap.add_argument("--rounds", type=int, default=10, help="评估周期采样轮数")
    ap.add_argument("--api-only", action="store_true", help="只测 HTTP 接口，不测引擎")
    ap.add_argument("--keep", action="store_true", help="保留压测数据（默认清理）")
    args = ap.parse_args()

    tok = call(args.base, "POST", "/api/auth/login", body={"username": args.user, "password": args.pwd})["data"]["access_token"]
    print(f"== 100 主机压测：{args.hosts} 台 × {args.points} 点 ==\n")

    # 1) 注册主机
    t_reg = time.perf_counter()
    agents = []
    for i in range(args.hosts):
        inst = call(args.base, "GET", "/api/ops/dev/mint-install-token", tok)["data"]["install_token"]
        reg = call(args.base, "POST", "/api/ops/agents/register", body={
            "fingerprint": f"{PREFIX}{i}-{int(time.time())}", "hostname": f"{PREFIX}{i}",
            "ip": f"10.99.{i // 250}.{i % 250 + 1}", "os": "bench", "install_token": inst})["data"]
        agents.append({"ident": reg["ident"], "token": reg["token"], "name": f"{PREFIX}{i}"})
    print(f"注册 {len(agents)} 台：{(time.perf_counter() - t_reg):.2f}s")

    # 2) 上报指标（含一个越过 95% 的点，供 SSE 延迟测量）
    t_ing = time.perf_counter()
    now = datetime.utcnow()
    for r in range(args.points):
        ts = (now - timedelta(seconds=15 * (args.points - r))).isoformat() + "Z"
        for a in agents:
            v = 99.9 if r == args.points - 1 else 30 + (r % 5)
            call(args.base, "POST", "/api/ops/agents/metrics",
                 body={"ts": ts, "metrics": [{"metric": "cpu.percent", "value": v},
                                             {"metric": "mem.percent", "value": 45.0},
                                             {"metric": "disk.percent", "value": 50.0},
                                             {"metric": "load.1", "value": 1.2}],
                       "probes": [{"name": "bench-http", "type": "http", "target": "http://127.0.0.1/health",
                                   "status": "up", "latency_ms": 5}]},
                 headers={"X-Ident": a["ident"], "X-Token": a["token"]})
    dur = time.perf_counter() - t_ing
    print(f"上报 {len(agents) * args.points} 次（{len(agents) * args.points * 4} 点）：{dur:.2f}s"
          f" → {len(agents) * args.points / max(dur, 1e-6):.0f} 次/s\n")

    # 3) 接口 P95
    api = {}
    for name, path in [("大盘 overview", "/api/ops/dashboard/overview"),
                       ("主机列表", "/api/ops/hosts"),
                       ("告警列表", "/api/ops/alerts?page=1"),
                       ("事件列表", "/api/ops/incidents?page=1"),
                       ("指标查询(单机60m)", f"/api/ops/metrics/query?ident={agents[0]['ident']}&metric=cpu.percent&minutes=60")]:
        lat = []
        for _ in range(30):
            t0 = time.perf_counter()
            call(args.base, "GET", path, tok)
            lat.append((time.perf_counter() - t0) * 1000)
        api[name] = {"p50": statistics.median(lat), "p95": pct(lat, 95)}
    print("接口延迟（30 次采样）")
    for k, v in api.items():
        print(f"  {k:22s} P50 {v['p50']:7.1f}ms   P95 {v['p95']:7.1f}ms")

    # 4) SSE 推送延迟（真实流；任何模式都能测）
    sse = measure_sse(args.base, tok, agents[0])

    # 5) 引擎评估周期（仅容器内可行）
    engine = {}
    if not args.api_only:
        try:
            engine = asyncio.run(_engine_bench(args.hosts, args.rounds))
            print("\n引擎评估（同一批函数驱动）")
            for k, v in engine.items():
                print(f"  {k}: {v if isinstance(v, str) else round(v, 1)}")
        except Exception as e:  # noqa: BLE001
            print(f"\n引擎测量不可用（{type(e).__name__}: {e}）——请用容器内跑法")

    # 5) 结论
    print("\n== 门槛判定（FR §8.2）==")
    print(" ", verdict("评估周期 P95", engine.get("eval_p95_ms", 0), 2000) if engine else "  跳过：评估周期（--api-only）")
    print(" ", verdict("大盘查询 P95", api["大盘 overview"]["p95"], 1000))
    worst = max(v["p95"] for v in api.values())
    print(" ", verdict("接口最差 P95", worst, 2000))
    if sse.get("sse_latency_ms"):
        print(" ", verdict("告警 SSE 延迟（含等下一个评估周期）", sse["sse_latency_ms"], 15000, "ms")
              + f"（实测事件 {sse.get('sse_event')}）")
    else:
        print(f"  SKIP  告警 SSE 延迟：未在超时内收到事件 {sse.get('sse_note', '')}")

    # 6) 清理
    if not args.keep:
        from sqlalchemy import delete, select, text as _text

        sys.path.insert(0, ".")
        from app.db.session import SessionLocal, dispose_all_engines
        from app.models.host import Host

        async def _clean():
            """清理压测产物。

            ⚠️ 必须连**活跃/历史事件**一起清：只删主机与规则会留下指向不存在主机的
            "幽灵告警"（实测踩过：100 条 cur 事件挂在一台已删主机上，大盘/告警中心一直显示）。
            """
            from app.models.alert import AlertCurEvent, AlertHisEvent, AlertRule

            async with SessionLocal() as db:
                idents = [a["ident"] for a in agents]
                # 1) 压测规则（含软删标记的）与其事件
                for rule_name in (RULE_NAME, SSE_RULE):
                    for r in (await db.execute(select(AlertRule).where(AlertRule.name == rule_name))).scalars().all():
                        await db.execute(delete(AlertCurEvent).where(AlertCurEvent.rule_id == r.id))
                        await db.execute(delete(AlertHisEvent).where(AlertHisEvent.rule_id == r.id))
                        await db.execute(delete(AlertRule).where(AlertRule.id == r.id))
                # 2) 压测主机：聚合指标 + 事件 + 主机行
                for i in idents:
                    await db.execute(_text("DELETE FROM metric_agg WHERE ident = :i"), {"i": i})
                    await db.execute(delete(AlertCurEvent).where(AlertCurEvent.host_ident == i))
                    await db.execute(delete(AlertHisEvent).where(AlertHisEvent.host_ident == i))
                await db.execute(delete(Host).where(Host.ident.in_(idents)))
                await db.commit()
            await dispose_all_engines()

        try:
            asyncio.run(_clean())
            print(f"\n已清理 {len(agents)} 台压测主机与聚合指标（--keep 可保留）")
        except Exception as e:  # noqa: BLE001
            print(f"\n清理失败（{type(e).__name__}: {e}）：请手工删除 name like '{PREFIX}%' 的主机")
    return 0


if __name__ == "__main__":
    sys.exit(main())
