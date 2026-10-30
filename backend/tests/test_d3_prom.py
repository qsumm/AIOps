"""D3 测试：Prometheus 轨（FR-B3）——用真实 HTTP 假 Prometheus 验证全链路。

覆盖：数据源 CRUD / 连接测试 / query_range 解析 / prom 规则端到端触发 / 指标查询代理。
不 mock httpx：起一个 stdlib HTTP 线程服务返回 Prometheus 形状响应，走真实网络栈。
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.engine import core
from app.main import app
from app.models.alert import AlertCurEvent, AlertHisEvent, AlertRule
from app.models.prom import PromDatasource

H = {"Authorization": ""}

# 假 Prometheus：query_range 返回两条序列（一条 instance-a 值 99，一条 b 值 10）
_MATRIX = {
    "status": "success",
    "data": {"resultType": "matrix", "result": [
        {"metric": {"__name__": "node_cpu_usage", "instance": "a:9100"},
         "values": [[1750000000, "1"], [1750000060, "99"]]},
        {"metric": {"__name__": "node_cpu_usage", "instance": "b:9100"},
         "values": [[1750000000, "10"], [1750000060, "10"]]},
    ]},
}
_VECTOR = {"status": "success", "data": {"resultType": "vector", "result": [
    {"metric": {}, "value": [1750000060, "1"]}]}}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = _MATRIX if self.path.startswith("/api/v1/query_range") else _VECTOR
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # 静音
        pass


class _BadHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(500)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def fake_prom():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def dead_prom():
    srv = HTTPServer(("127.0.0.1", 0), _BadHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


async def _login(ac) -> str:
    r = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["data"]["access_token"]


async def _quiesce(rule_id: int):
    """停掉 worker 的后台定时循环，改由测试手动驱动 _eval_once（保证确定性）。

    生产链路不变：reconcile() 仍创建 worker 并启动循环，这里只是把循环停掉。
    """
    await core.reconcile()
    w = core.workers[rule_id]
    await w.stop()   # set stop + cancel task
    await asyncio.sleep(0)  # 让取消落地
    return w


@pytest.mark.asyncio
async def test_prom_datasource_crud_and_test(fake_prom, dead_prom):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        H["Authorization"] = f"Bearer {await _login(ac)}"
        ok = await ac.post("/api/ops/prom-datasources", headers=H, json={
            "name": "d3-e2e-prom", "base_url": fake_prom, "is_default": True})
        assert ok.status_code == 200, ok.text
        did = ok.json()["data"]["id"]
        bad = await ac.post("/api/ops/prom-datasources", headers=H, json={
            "name": "d3-e2e-dead", "base_url": dead_prom})
        bid = bad.json()["data"]["id"]
        try:
            # 连接测试：ok / failed
            t1 = await ac.post(f"/api/ops/prom-datasources/{did}/test", headers=H)
            assert t1.json()["data"]["ok"] is True
            t2 = await ac.post(f"/api/ops/prom-datasources/{bid}/test", headers=H)
            assert t2.json()["data"]["ok"] is False
            # 列表：has_secret 字段、last_check_status 已写
            lst = await ac.get("/api/ops/prom-datasources", headers=H)
            item = [x for x in lst.json()["data"]["items"] if x["id"] == did][0]
            assert item["last_check_status"] == "ok" and "has_secret" in item
            # 指标查询代理：原样转发 matrix
            q = await ac.get("/api/ops/prom/query", headers=H,
                             params={"datasource_id": did, "query": "node_cpu_usage", "minutes": 10})
            assert q.json()["data"]["body"]["data"]["resultType"] == "matrix"
            # 统一查询入口 source=prom
            q2 = await ac.get("/api/ops/metrics/query", headers=H,
                              params={"source": "prom", "datasource_id": did, "query": "node_cpu_usage"})
            assert q2.json()["data"]["source"] == "prom"
            # 不可达数据源 → 503
            q3 = await ac.get("/api/ops/metrics/query", headers=H,
                              params={"source": "prom", "datasource_id": bid, "query": "x"})
            assert q3.json()["code"] == 503
        finally:
            for i in (did, bid):
                await ac.delete(f"/api/ops/prom-datasources/{i}", headers=H)


@pytest.mark.asyncio
async def test_prom_rule_end_to_end(fake_prom):
    """prom 规则：query_range → detect(>90) → 只 a:9100 触发 → cur 落库。"""
    rule_id = ds_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            H["Authorization"] = f"Bearer {await _login(ac)}"
            ds_id = (await ac.post("/api/ops/prom-datasources", headers=H, json={
                "name": "d3-e2e-prom2", "base_url": fake_prom, "is_default": True})).json()["data"]["id"]
            rule_id = (await ac.post("/api/ops/alert-rules", headers=H, json={
                "name": "d3-prom-高CPU", "scope": "prom", "metric": "node_cpu_usage",
                "queries_json": ["node_cpu_usage"], "datasource_id": ds_id,
                "expr": ">90", "severity": 2, "eval_interval_s": 15, "window_s": 300,
                "algorithm": "threshold", "recover_duration_s": 0})).json()["data"]["id"]
            # 试运行
            t = await ac.post(f"/api/ops/alert-rules/{rule_id}/test", headers=H)
            td = t.json()["data"]
            assert td["anomaly_count"] == 1, td
            assert td["matched"][0]["host"] == "a:9100", td
            # 真实评估链路（手动驱动，先停后台循环）
            w = await _quiesce(rule_id)
            await w._eval_once()
            while not core.event_queue.empty():
                await core.consume_one(core.event_queue.get_nowait())
            async with SessionLocal() as db:
                cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rule_id))).scalars().all()
            assert len(cur) == 1, f"应只有 a:9100 触发，实际 {[(c.host_ident, c.trigger_value) for c in cur]}"
            assert cur[0].host_ident == "a:9100"
            assert cur[0].metric == "node_cpu_usage"
            assert cur[0].trigger_value == 99.0
    finally:
        async with SessionLocal() as db:
            if rule_id:
                await db.execute(delete(AlertRule).where(AlertRule.id == rule_id))
                await db.execute(delete(AlertCurEvent).where(AlertCurEvent.rule_id == rule_id))
                await db.execute(delete(AlertHisEvent).where(AlertHisEvent.rule_id == rule_id))
            if ds_id:
                await db.execute(delete(PromDatasource).where(PromDatasource.id == ds_id))
            await db.commit()
        if rule_id:
            w = core.workers.pop(rule_id, None)
            if w:
                await w.stop()


@pytest.mark.asyncio
async def test_prom_unreachable_does_not_recover(dead_prom):
    """数据源不可达：本轮不产生事件、不产生恢复误报（03 §1.6）。"""
    rule_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            H["Authorization"] = f"Bearer {await _login(ac)}"
            ds_id = (await ac.post("/api/ops/prom-datasources", headers=H, json={
                "name": "d3-e2e-dead2", "base_url": dead_prom, "is_default": True})).json()["data"]["id"]
            rule_id = (await ac.post("/api/ops/alert-rules", headers=H, json={
                "name": "d3-prom-不可达", "scope": "prom", "metric": "x", "queries_json": ["x"],
                "datasource_id": ds_id, "expr": ">0", "recover_duration_s": 0})).json()["data"]["id"]
            w = await _quiesce(rule_id)
            for _ in range(3):
                await w._eval_once()
            assert w.eval_error_streak == 3, w.eval_error_streak
            assert core.event_queue.empty(), "不可达时不应产生任何事件（含恢复事件）"
    finally:
        async with SessionLocal() as db:
            if rule_id:
                await db.execute(delete(AlertRule).where(AlertRule.id == rule_id))
            await db.execute(delete(PromDatasource).where(PromDatasource.name.in_(["d3-e2e-dead2"])))
            await db.commit()
        if rule_id:
            w = core.workers.pop(rule_id, None)
            if w:
                await w.stop()


@pytest.mark.asyncio
async def test_probe_scope_rule_fires():
    """探活域规则（FR-C1 scope=probe）：down→1，证书剩余天数→原值。

    回归点：探活状态只落 Probe 表、不进内存环形，早期实现只读环形 → 探活规则永不触发。
    """
    from app.models.collect import Probe
    from app.models.host import Host

    rule_id = None
    async with SessionLocal() as db:
        h = Host(ident="probe-scope-test", name="probe-test", ip="10.9.9.9", os="test", status="up")
        db.add(h)
        await db.commit()
        await db.refresh(h)
        db.add(Probe(host_id=h.id, name="http-80", type="http", target="http://10.9.9.9:80/health", last_status="down", last_latency_ms=0))
        db.add(Probe(host_id=h.id, name="tls-cert", type="https-cert", target="10.9.9.9:443", last_status="up", last_latency_ms=12))
        await db.commit()
        hid = h.id
    try:
        async with SessionLocal() as db:
            r = AlertRule(name="d3-探活失败", scope="probe", metric="http-80", expr=">0", severity=1,
                          eval_interval_s=15, window_s=300, algorithm="threshold", recover_duration_s=0)
            db.add(r)
            await db.commit()
            await db.refresh(r)
            rule_id = r.id
        w = await _quiesce(rule_id)
        await w._eval_once()
        while not core.event_queue.empty():
            await core.consume_one(core.event_queue.get_nowait())
        async with SessionLocal() as db:
            cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rule_id))).scalars().all()
        mine = [c for c in cur if c.host_ident == "probe-scope-test"]
        assert len(mine) == 1, f"本主机只有 http-80(down) 应触发，实际 {[(c.host_ident, c.metric) for c in cur]}"
        assert mine[0].metric == "http-80" and mine[0].trigger_value == 1.0
    finally:
        async with SessionLocal() as db:
            if rule_id:
                await db.execute(delete(AlertRule).where(AlertRule.id == rule_id))
                await db.execute(delete(AlertCurEvent).where(AlertCurEvent.rule_id == rule_id))
                await db.execute(delete(AlertHisEvent).where(AlertHisEvent.rule_id == rule_id))
            await db.execute(delete(Probe).where(Probe.host_id == hid))
            await db.execute(delete(Host).where(Host.id == hid))
            await db.commit()
        if rule_id:
            w = core.workers.pop(rule_id, None)
            if w:
                await w.stop()
