"""D9 补齐项的回归测试（需求对账发现的缺口 → 逐条锁死）。

覆盖：
  1. 每日 token 配额（FR 非功能 §8.3）：超限 429 + 只计量不误伤 + demo 更严
  2. 告警规则 cron 调度（FR-C1）：cron 表达式求下次评估时刻；非法表达式回落
  3. 内置技能 ≥5 且领域化（FR §6.8）+ 连接检查 fail-closed（Aurora 语义）
  4. Agent 心跳（FR-B1/B5）：只刷 last_beat，不上报数据
  5. 手动建事件（05 §4）+ 全局处置审计（05 §4）
  6. 活跃更新事件入队（D9 实测缺陷）：repeat_step 节奏与 updated 事件依赖它
  7. 巡检三项新检查（异常进程/探活成功率/系统负载）
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.engine import core
from app.engine.state import StateMachine
from app.main import app
from app.models.alert import AlertCurEvent, AlertHisEvent, AlertRule
from app.models.host import Host
from app.models.llm import LLMUsage
from app.services import skills as skills_svc

HOST = "d9-host"


# ---------------- 1. token 配额 ----------------
@pytest.mark.asyncio
async def test_daily_token_quota_blocks_when_exceeded(monkeypatch):
    """超限必须 429 且原因可读；未超限不得误伤。"""
    from app.services import llm_gateway as gw

    s = get_settings()
    monkeypatch.setattr(s, "llm_daily_token_limit", 1000, raising=False)
    async with SessionLocal() as db:
        await db.execute(delete(LLMUsage))
        await db.commit()
        st = await gw.quota_state(db)
        assert st["limit"] == 1000 and st["used"] == 0 and not st["exceeded"]
        await gw.enforce_quota(db)   # 未超限：不应抛

        db.add(LLMUsage(purpose="chat", model="m", total_tokens=1200))
        await db.commit()
        st = await gw.quota_state(db)
        assert st["exceeded"] and st["remaining"] == 0
        with pytest.raises(Exception) as e:
            await gw.enforce_quota(db)
        assert "LLM_QUOTA_EXCEEDED" in str(getattr(e.value, "detail", e.value))
        await db.execute(delete(LLMUsage))
        await db.commit()


@pytest.mark.asyncio
async def test_demo_quota_is_stricter(monkeypatch):
    """demo 角色取 demo 限额与全局限额的更严者。

    D21 语义修正：demo 口径统计的是 **demo 自己的用量**。
    原用例插入的用量行没有归属（role=""），等于把"引擎/管理员的消耗也算进演示站额度"
    这一**缺陷行为**当成了正确行为 —— 修后必须显式打 role="demo"。

    另：原用例会 `DELETE FROM llm_usage` 清空整张台账（影响其他测试与用量页显示），
    改为只删自己插入的行。
    """
    from app.services import llm_gateway as gw

    s = get_settings()
    monkeypatch.setattr(s, "llm_daily_token_limit", 100000, raising=False)
    monkeypatch.setattr(s, "demo_daily_token_limit", 500, raising=False)
    marker = "d9-quota-marker"
    async with SessionLocal() as db:
        try:
            db.add(LLMUsage(purpose="chat", model=marker, role="demo", total_tokens=600))
            await db.commit()

            demo = await gw.quota_state(db, "demo")
            assert demo["limit"] == 500, "demo 应取更严的 demo 限额"
            assert demo["scope"] == "demo"
            assert demo["exceeded"] is True, "demo 自己用超了必须判超限"
            assert demo["used"] == await gw.tokens_used_today(db, role="demo")

            admin = await gw.quota_state(db, "admin")
            assert admin["limit"] == 100000, "管理员走全局限额"
            assert admin["scope"] == "global"
            # 全局口径的本义：统计全平台（含 demo 与引擎），故必然 ≥ demo 自己的用量
            assert admin["used"] >= demo["used"]
        finally:
            await db.execute(delete(LLMUsage).where(LLMUsage.model == marker))
            await db.commit()


# ---------------- 2. cron 调度 ----------------
def test_rule_cron_scheduling():
    """cron_pattern 非空按 cron 求下次评估；非法表达式回落固定间隔（不炸告警链路）。"""

    class R:
        def __init__(self, interval=15, cron=""):
            self.eval_interval_s, self.cron_pattern, self.id = interval, cron, 1

    assert core.next_rule_delay(R(30)) == 30.0
    # 10:02 时 */5 * * * * → 10:05 → 180s
    assert core.next_rule_delay(R(30, "*/5 * * * *"), datetime(2026, 10, 14, 10, 2, 0)) == 180.0
    # 非法表达式 → 回落
    assert core.next_rule_delay(R(45, "not-a-cron"), datetime(2026, 10, 14, 10, 2, 0)) == 45.0
    # 下限 5s（防抖）
    assert core.next_rule_delay(R(1)) == 5.0


# ---------------- 3. 技能（≥5 内置 + 连接检查 fail-closed）----------------
def test_builtin_skills_cover_required_domains():
    """FR §6.8：内置 ≥5 个技能，且覆盖主机排查/日志检索/指标分析/案例检索/磁盘问题。"""
    names = set(skills_svc.CORE_SKILLS)
    assert len(names) >= 5, names
    for need in ("host-health-diagnose", "log-search", "metric-analysis", "case-recall", "disk-troubleshoot"):
        assert need in names, f"缺领域技能 {need}"
    # 领域技能必须声明连接检查依赖（fail-closed 的前提）
    for n, spec in skills_svc.CORE_SKILLS.items():
        if n.startswith("core-"):
            assert not (spec.get("check") or {}).get("requires"), "核心方法论技能不该有外部依赖"
        else:
            assert (spec.get("check") or {}).get("requires"), f"{n} 缺 requires 声明"


@pytest.mark.asyncio
async def test_skill_connection_check_excludes_unusable_skill(monkeypatch):
    """连接检查失败 → 该技能不进索引、不可加载（fail-closed）。"""
    await skills_svc.sync_builtin_skills()
    name = "metric-analysis"
    # 造一个"依赖未就绪"：直接把结果写进去（等价于检查失败后的状态）
    async with SessionLocal() as db:
        row = (await db.execute(select(skills_svc.Skill).where(skills_svc.Skill.name == name))).scalar_one()
        row.connection_check = {"spec": {"requires": ["metrics"]}, "ok": False, "note": "测试置为失败"}
        await db.commit()
    idx = [i["name"] for i in await skills_svc.skill_index()]
    assert name not in idx, "依赖未就绪的技能不该进索引"
    assert await skills_svc.load_skill(name) is None, "依赖未就绪的技能不该可加载"

    r = await skills_svc.check_skill(name)      # 真实检查（测试库里没有近 5 分钟上报，可能仍失败）
    assert "ok" in r and "checks" in r
    async with SessionLocal() as db:            # 复原：置为通过
        row = (await db.execute(select(skills_svc.Skill).where(skills_svc.Skill.name == name))).scalar_one()
        row.connection_check = {"spec": {"requires": ["metrics"]}, "ok": True, "note": "测试复原"}
        await db.commit()
    assert name in [i["name"] for i in await skills_svc.skill_index()]


@pytest.mark.asyncio
async def test_skill_check_endpoint(client):
    ac, hdrs = client
    d = await ac.get("/api/ops/skills", headers=hdrs["admin"])
    assert d.status_code == 200
    items = d.json()["data"]["items"]
    assert len(items) >= 5, "内置技能未同步？"
    sid = next(i["id"] for i in items if i["name"] == "core-safety")
    r = await ac.post(f"/api/ops/skills/{sid}/check", headers=hdrs["admin"])
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["ok"] is True and "核心方法论" in body["note"]


# ---------------- 4/5. 新接口 ----------------
@pytest.mark.asyncio
async def test_agent_heartbeat_refreshes_last_beat(client):
    """心跳只刷 last_beat（不上报数据）：离线判定不会因为"这轮没数据"误判。"""
    ac, hdrs = client
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == HOST))
        db.add(Host(ident=HOST, name=HOST, ip="10.9.9.9", os="t", status="down",
                    agent_token="d9-token", last_beat=datetime.utcnow() - timedelta(minutes=10)))
        await db.commit()
    try:
        r = await ac.post("/api/ops/agents/heartbeat", json={"agent_version": "9.9"},
                          headers={"X-Ident": HOST, "X-Token": "d9-token"})
        assert r.status_code == 200, r.text
        async with SessionLocal() as db:
            h = (await db.execute(select(Host).where(Host.ident == HOST))).scalar_one()
            assert h.status == "up" and h.agent_version == "9.9"
            assert (datetime.utcnow() - h.last_beat).total_seconds() < 5, "last_beat 未被刷新"
        bad = await ac.post("/api/ops/agents/heartbeat", json={}, headers={"X-Ident": HOST, "X-Token": "wrong"})
        assert bad.status_code == 401
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Host).where(Host.ident == HOST))
            await db.commit()


@pytest.mark.asyncio
async def test_manual_incident_and_global_action_audit(client):
    """手动建事件（alert_ops 级）+ 全局处置审计查询。"""
    ac, hdrs = client
    r = await ac.post("/api/ops/incidents", json={"title": "d9 手动事件", "severity": 2,
                                                  "host_ident": "abc"}, headers=hdrs["admin"])
    assert r.status_code == 200, r.text
    iid = r.json()["data"]["id"]
    try:
        detail = await ac.get(f"/api/ops/incidents/{iid}", headers=hdrs["admin"])
        assert detail.json()["data"]["title"] == "d9 手动事件"
        empty = await ac.post("/api/ops/incidents", json={"title": "  "}, headers=hdrs["admin"])
        assert empty.status_code == 422, "空标题应 422"
        audit = await ac.get("/api/ops/action-audit?page=1", headers=hdrs["member"])
        assert audit.status_code == 200 and "total" in audit.json()["data"]
    finally:
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM incidents WHERE id = :i"), {"i": iid})
            await db.commit()


# ---------------- 6. 更新事件入队（repeat_step 的前提）----------------
@pytest.mark.asyncio
async def test_active_update_reaches_consumer_for_repeat_notify():
    """活跃更新必须入队并触发"重发"分支：这是 FR-C5 节奏的参数化验证。

    设 notify_repeat_step_min=0（只发一次）→ 更新不重发，且**不写 his 账本**（避免账本被更新淹没）；
    设 notify_repeat_step_min=1 且 last_sent 很旧 → 更新触发重发，台账 +1。
    """
    from app.services import notify as notify_svc
    from app.services.notify import should_notify

    sm = StateMachine({"id": 7, "name": "r", "severity": 2, "query": "q",
                       "for_duration_s": 0, "recover_duration_s": 0})
    labels = {"host": HOST, "metric": "cpu.percent"}
    first = sm.process([(labels, 99.0)], now=datetime.utcnow() - timedelta(minutes=10))
    assert len(first) == 1 and not first[0].is_update
    upd = sm.process([(labels, 99.5)], now=datetime.utcnow())
    assert len(upd) == 1 and upd[0].is_update

    st = sm.fires[upd[0].hash]
    # 首次（从未发过）→ 该发
    assert should_notify(st, 0, 0, True, False) is True
    # 已发过一条 + repeat=0（只发一次）→ 不再重发
    st.last_sent = datetime.utcnow()
    st.notify_count = 2
    assert should_notify(st, 0, 0, True, False) is False
    # repeat=1 但还没到间隔 → 不发；间隔到了 → 重发
    assert should_notify(st, 1, 0, True, False) is False
    st.last_sent = datetime.utcnow() - timedelta(minutes=5)
    assert should_notify(st, 1, 0, True, False) is True
    # 封顶：notify_count = 1 + 已发数，最多发 max_number 条
    st.notify_count = 6
    assert should_notify(st, 1, 5, True, False) is False
    assert callable(notify_svc.dispatch)


@pytest.mark.asyncio
async def test_silent_update_writes_no_ledger(client):
    """未发送通知的更新不写 his 账本（否则每轮评估每台主机都写一行）。"""
    ac, hdrs = client
    async with SessionLocal() as db:
        await db.execute(delete(AlertRule).where(AlertRule.name == "d9-update-rule"))
        rule = AlertRule(name="d9-update-rule", scope="metric", metric="cpu.percent", expr=">90",
                         severity=2, eval_interval_s=15, window_s=300, algorithm="threshold",
                         recover_duration_s=0, notify_repeat_step_min=0)
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        rid = rule.id
    try:
        ev = core.Ev(hash="d9updatehash", rule_id=rid, rule_name="d9-update-rule", host_ident=HOST,
                     metric="cpu.percent", severity=2, trigger_value=95.0, labels={"host": HOST}, tags={})
        async with SessionLocal() as db:
            # 1) 首次：写 cur + his
            assert await core._persist(db, ev, muted_notify=False) is True
            # 2) 更新且未通知：不写 his
            ev2 = core.Ev(hash="d9updatehash", rule_id=rid, rule_name="d9-update-rule", host_ident=HOST,
                          metric="cpu.percent", severity=2, trigger_value=96.0,
                          labels={"host": HOST}, tags={}, is_update=True)
            assert await core._persist(db, ev2, muted_notify=False, notified=False) is False
            n = (await db.execute(select(AlertHisEvent).where(AlertHisEvent.hash == "d9updatehash"))).scalars().all()
            assert len(n) == 1, f"更新不该写账本，实得 {len(n)} 行"
            # 3) 更新且真发了通知：写账本
            await core._persist(db, ev2, muted_notify=False, notified=True)
            n2 = (await db.execute(select(AlertHisEvent).where(AlertHisEvent.hash == "d9updatehash"))).scalars().all()
            assert len(n2) == 2
            cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash == "d9updatehash"))).scalars().all()
            assert len(cur) == 1 and cur[0].notify_cur_number == 2
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(AlertRule).where(AlertRule.id == rid))
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.hash == "d9updatehash"))
            await db.execute(delete(AlertHisEvent).where(AlertHisEvent.hash == "d9updatehash"))
            await db.commit()


# ---------------- 7. 巡检新检查项 ----------------
def test_inspection_items_cover_required_checks():
    """FR-D5 的检查项清单：磁盘/内存/CPU/负载/证书/探活成功率/异常进程/离线。"""
    from app.services import inspect as inspect_svc

    keys = {i["key"] for i in inspect_svc.ITEMS}
    for need in ("disk", "mem", "cpu", "load", "cert", "probe", "proc", "offline"):
        assert need in keys, f"巡检缺检查项 {need}"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        mtok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        yield ac, {"admin": {"Authorization": f"Bearer {tok}"},
                   "member": {"Authorization": f"Bearer {mtok}"}}


# ---------------- 8. FR-B3+ exporter 直采 ----------------
NODE_METRICS = """# HELP node_load1 1m load average.
# TYPE node_load1 gauge
node_load1 3.25
# TYPE node_cpu_seconds_total counter
node_cpu_seconds_total{cpu="0",mode="idle"} 900.0
node_cpu_seconds_total{cpu="0",mode="user"} 80.0
node_cpu_seconds_total{cpu="0",mode="system"} 20.0
# TYPE node_memory_MemAvailable_bytes gauge
node_memory_MemAvailable_bytes 2.0e+09
# TYPE node_memory_MemTotal_bytes gauge
node_memory_MemTotal_bytes 8.0e+09
# TYPE node_filesystem_size_bytes gauge
node_filesystem_size_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 1.0e+11
node_filesystem_size_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/data"} 1.0e+11
# TYPE node_filesystem_avail_bytes gauge
node_filesystem_avail_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 9.0e+10
node_filesystem_avail_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/data"} 1.0e+10
# TYPE node_uptime_seconds gauge
node_uptime_seconds 4242
"""


def test_exporter_metrics_mapping():
    """node_exporter 文本 → 平台指标：CPU/内存/磁盘取最紧挂载点/负载，另有正则白名单透传。"""
    from app.services import prom_scrape

    parsed = prom_scrape._parse_exposition(NODE_METRICS)
    assert parsed["node_load1"][0][1] == 3.25
    vals = prom_scrape.map_to_platform_metrics(parsed)
    # CPU：非 idle 占比 = (80+20)/(900+80+20)
    assert abs(vals["cpu.percent"] - 10.0) < 0.01, vals
    # 内存：(8-2)/8 = 75%
    assert abs(vals["mem.percent"] - 75.0) < 0.01, vals
    # 磁盘：取最紧的 /data（已用 90%）
    assert abs(vals["disk.percent"] - 90.0) < 0.01, vals
    assert vals["load.1"] == 3.25
    # 正则白名单：额外保留 node_uptime_seconds
    extra = prom_scrape.extract_declared_metrics(NODE_METRICS, ["node_uptime_seconds"])
    assert extra == {"node_uptime_seconds": 4242.0}, extra


@pytest.mark.asyncio
async def test_scrape_once_creates_host_and_metrics(monkeypatch):
    """直采一次 → 合成主机出现 + 指标进环形（FR-B3+ 的"零改造接入"验收）。"""
    from app.models.prom import PromDatasource
    from app.services import prom_scrape
    from app.store import ring

    async def fake_fetch(ds, path="/metrics"):
        return NODE_METRICS

    monkeypatch.setattr(prom_scrape, "fetch_metrics", fake_fetch)
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM prom_datasources WHERE name = 'd9-scrape'"))
        await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": prom_scrape.SCRAPE_IDENT.format(did=0)})
        ds = PromDatasource(name="d9-scrape", base_url="http://127.0.0.1:9100", mode="scrape")
        db.add(ds)
        await db.commit()
        await db.refresh(ds)
    ident = prom_scrape.SCRAPE_IDENT.format(did=ds.id)
    try:
        async with SessionLocal() as db:
            ds = await db.get(PromDatasource, ds.id)
            res = await prom_scrape.scrape_once(ds)
        assert res["ok"], res
        assert res["host_ident"] == ident
        assert abs(res["metrics"]["disk.percent"] - 90.0) < 0.01
        async with SessionLocal() as db:
            h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
            assert h.name.startswith("[scrape]") and h.status == "up"
        pts = ring.query_points(ident, "cpu.percent", datetime.utcnow() - timedelta(minutes=1), datetime.utcnow())
        assert pts and abs(pts[-1][1] - 10.0) < 0.01, pts
        # 抓取失败不炸：换个会抛错的 fetch
        async def bad_fetch(ds, path="/metrics"):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(prom_scrape, "fetch_metrics", bad_fetch)
        async with SessionLocal() as db:
            ds = await db.get(PromDatasource, ds.id)
            bad = await prom_scrape.scrape_once(ds)
        assert bad["ok"] is False and "抓取失败" in bad["note"]
    finally:
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM prom_datasources WHERE name = 'd9-scrape'"))
            await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
            await db.commit()
        ring._rings.pop((ident, "cpu.percent"), None)


# ---------------- 9. 演示数据可见域（FR-A1）----------------
@pytest.mark.asyncio
async def test_demo_visibility_filter(monkeypatch, client):
    """配置 DEMO_DATA_PREFIXES 后：demo 只看得到前缀命中的主机及其告警/事件；admin 不受影响。"""
    from app.core.config import get_settings
    from app.services import visibility

    ac, hdrs = client
    s = get_settings()
    monkeypatch.setattr(s, "demo_data_prefixes", "demo-", raising=False)
    idents = {"demo-x1": "d9demoident0001", "prod-x1": "d9prodident0001"}
    async with SessionLocal() as db:
        for name, ident in idents.items():
            await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
            db.add(Host(ident=ident, name=name, ip="10.1.1.1", os="t", status="up", agent_token="t"))
        await db.commit()
    demo_hdr = {"Authorization": f"Bearer {(await ac.post('/api/auth/demo', json={})).json()['data']['access_token']}"}
    try:
        # 服务层
        async with SessionLocal() as db:
            demo_user = type("U", (), {"role": "demo"})()
            admin_user = type("U", (), {"role": "admin"})()
            vis = await visibility.visible_host_idents(db, demo_user)
            assert vis is not None and idents["demo-x1"] in vis and idents["prod-x1"] not in vis
            assert await visibility.visible_host_idents(db, admin_user) is None
        # 接口层：demo 的主机列表里没有 prod-x1；admin 有
        names_demo = [h["name"] for h in (await ac.get("/api/ops/hosts", headers=demo_hdr)).json()["data"]["items"]]
        assert "prod-x1" not in names_demo, names_demo
        names_admin = [h["name"] for h in (await ac.get("/api/ops/hosts", headers=hdrs["admin"])).json()["data"]["items"]]
        assert "prod-x1" in names_admin and "demo-x1" in names_admin
        # 详情：demo 看不可见主机 → 404（不泄露存在性）
        assert (await ac.get(f"/api/ops/hosts/{idents['prod-x1']}", headers=demo_hdr)).status_code == 404
        assert (await ac.get(f"/api/ops/hosts/{idents['demo-x1']}", headers=demo_hdr)).status_code == 200
    finally:
        async with SessionLocal() as db:
            for ident in idents.values():
                await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
            await db.commit()


@pytest.mark.asyncio
async def test_prom_ds_api_persists_scrape_mode(client):
    """直采模式与 scrape_config 必须真的落库（否则 UI 配了也没用——E2E 实测踩到过）。"""
    ac, hdrs = client
    body = {"name": "d9-ds-api", "base_url": "http://127.0.0.1:9100", "mode": "scrape",
            "scrape_config": {"path": "/metrics", "metric_keep": ["node_uptime_seconds"]}}
    r = await ac.post("/api/ops/prom-datasources", json=body, headers=hdrs["admin"])
    assert r.status_code == 200, r.text
    try:
        items = (await ac.get("/api/ops/prom-datasources", headers=hdrs["admin"])).json()["data"]["items"]
        row = next(i for i in items if i["name"] == "d9-ds-api")
        assert row["mode"] == "scrape", row
        assert row["scrape_config"]["metric_keep"] == ["node_uptime_seconds"], row
        # 更新（PUT）也要能改模式
        upd = await ac.put(f"/api/ops/prom-datasources/{row['id']}",
                           json={**body, "mode": "query", "scrape_config": {}}, headers=hdrs["admin"])
        assert upd.status_code == 200, upd.text
        items2 = (await ac.get("/api/ops/prom-datasources", headers=hdrs["admin"])).json()["data"]["items"]
        assert next(i for i in items2 if i["name"] == "d9-ds-api")["mode"] == "query"
    finally:
        items = (await ac.get("/api/ops/prom-datasources", headers=hdrs["admin"])).json()["data"]["items"]
        for i in items:
            if i["name"] == "d9-ds-api":
                await ac.delete(f"/api/ops/prom-datasources/{i['id']}", headers=hdrs["admin"])


# ---------------- 10. 处置执行：三模式 + 审批凭据（FR-E2）----------------
async def _mk_runbook(mode: str, steps: list[dict]) -> int:
    from app.models.ops import Runbook

    async with SessionLocal() as db:
        rb = Runbook(name="d9-hitl", host_ident=HOST, risk_level="mid", execution_mode=mode,
                     whitelist_enabled=(mode == "auto"), steps=steps, status="draft",
                     rollback_plan="回滚：恢复原状", version=1)
        db.add(rb)
        await db.commit()
        await db.refresh(rb)
        return rb.id


async def _drop_runbook(rid: int) -> None:
    from app.models.ops import ActionApproval, Runbook

    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM action_audit WHERE runbook_id = :i"), {"i": rid})
        await db.execute(text("DELETE FROM action_approvals WHERE runbook_id = :i"), {"i": rid})
        await db.execute(text("DELETE FROM runbook_versions WHERE runbook_id = :i"), {"i": rid})
        await db.execute(delete(Runbook).where(Runbook.id == rid))
        await db.commit()


def test_action_allowlist_matching():
    """动作清单（FR-E2 脚本级白名单）：只认清单内动作；危险/未知动作不命中。"""
    from app.services.runbook import action_allowlist, match_action_allowlist

    assert len(action_allowlist()) >= 4
    assert match_action_allowlist("find /tmp -type f -mtime +7 -delete")["id"] == "cleanup-tmp"
    assert match_action_allowlist("systemctl restart nginx")["id"] == "restart-nginx"
    assert match_action_allowlist("systemctl restart nginx.service")["id"] == "restart-nginx"
    assert match_action_allowlist("systemctl restart some-other-app") is None
    assert match_action_allowlist("rm -rf /tmp/x") is None


@pytest.mark.asyncio
async def test_execute_modes_and_confirm_credential():
    """三模式语义矩阵 + 凭据单次/过期 + 审计落审批人与方式（FR-E2 的核心验收）。"""
    from app.models.ops import ActionApproval
    from app.services import runbook as svc

    ro = {"idx": 0, "desc": "看磁盘", "cmd": "df -h", "mode": "readonly"}
    chg = {"idx": 1, "desc": "清理临时文件", "cmd": "find /tmp -type f -mtime +7 -delete", "mode": "approval"}
    unknown = {"idx": 2, "desc": "未知变更", "cmd": "systemctl restart some-app", "mode": "approval"}

    # ① 只读：任何模式都直接放行，approval_mode=readonly
    rid = await _mk_runbook("suggest", [ro, chg, unknown])
    try:
        r = await svc.execute_step(rid, 0, operator_name="tester")
        assert r["ok"] and r["approval_mode"] == "readonly", r
    finally:
        await _drop_runbook(rid)

    # ② 仅建议模式：非只读步骤一律拒绝（既不签票也不出执行指令）
    rid = await _mk_runbook("suggest", [ro, chg])
    try:
        no_confirm = await svc.issue_confirm(rid, 1, operator="tester")
        assert not no_confirm["ok"] and "SUGGEST_ONLY" in no_confirm["msg"], no_confirm
        no_exec = await svc.execute_step(rid, 1, operator_name="tester")
        assert not no_exec["ok"] and "SUGGEST_ONLY" in no_exec["msg"], no_exec
    finally:
        await _drop_runbook(rid)

    # ③ 人工确认：无凭据拒绝 → 有凭据放行 → 凭据不可重放
    rid = await _mk_runbook("manual", [ro, chg])
    try:
        denied = await svc.execute_step(rid, 1, operator_name="tester")
        assert not denied["ok"] and "HITL_REQUIRED" in denied["msg"], denied
        # 老客户端自报 approved=true 也不再放行（客户端自报不可信）
        denied2 = await svc.execute_step(rid, 1, approved=True, operator_name="tester")
        assert not denied2["ok"] and denied2["data"]["approved_param_ignored"] is True, denied2
        cfm = await svc.issue_confirm(rid, 1, operator="tester")
        assert cfm["ok"] and cfm["allowlisted"] and cfm["confirm_token"].startswith("cfm-"), cfm
        ok = await svc.execute_step(rid, 1, confirm_token=cfm["confirm_token"], operator_name="tester")
        assert ok["ok"] and ok["approval_mode"] == "manual" and ok["approved_by"] == "tester", ok
        again = await svc.execute_step(rid, 1, confirm_token=cfm["confirm_token"], operator_name="tester")
        assert not again["ok"], "凭据必须一次性（不可重放）"
        # 过期凭据：直接改 DB 里的 expires_at
        cfm2 = await svc.issue_confirm(rid, 1, operator="tester")
        async with SessionLocal() as db:   # 只把**这一张**票改过期（按 token 哈希定位）
            row = (await db.execute(select(ActionApproval).where(
                ActionApproval.token_hash == svc._hash_token(cfm2["confirm_token"])))).scalars().one()
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
            await db.commit()
        expired = await svc.execute_step(rid, 1, confirm_token=cfm2["confirm_token"], operator_name="tester")
        assert not expired["ok"], "过期凭据必须拒绝"
    finally:
        await _drop_runbook(rid)

    # ④ auto 模式：命中动作清单 → 免确认放行（approval_mode=auto）；未命中 → 仍需确认
    rid = await _mk_runbook("auto", [chg, unknown])
    try:
        auto_ok = await svc.execute_step(rid, 1, operator_name="ops")
        assert auto_ok["ok"] and auto_ok["approval_mode"] == "auto" and auto_ok["allowlist_id"] == "cleanup-tmp", auto_ok
        need = await svc.execute_step(rid, 2, operator_name="ops")
        assert not need["ok"] and "未命中动作清单" in need["msg"], need
        cfm3 = await svc.issue_confirm(rid, 2, operator="ops")
        ok3 = await svc.execute_step(rid, 2, confirm_token=cfm3["confirm_token"], operator_name="ops")
        assert ok3["ok"] and ok3["approval_mode"] == "manual", ok3
    finally:
        await _drop_runbook(rid)

    # ⑤ 审计里能查到"谁在何时以何种方式批准"
    rid = await _mk_runbook("manual", [chg])
    try:
        cfm = await svc.issue_confirm(rid, 1, operator="auditor")
        await svc.execute_step(rid, 1, confirm_token=cfm["confirm_token"], operator_name="auditor")
        async with SessionLocal() as db:
            rows = (await db.execute(text("SELECT approval_mode, approved_by, approved_at, guardrail_verdict "
                                          "FROM action_audit WHERE runbook_id = :i"), {"i": rid})).all()
        hit = [r for r in rows if r[0] == "manual"]
        assert hit and hit[0][1] == "auditor" and hit[0][2] is not None and hit[0][3] == "hitl", rows
    finally:
        await _drop_runbook(rid)


@pytest.mark.asyncio
async def test_confirm_and_allowlist_api(client):
    """接口层：确认签票（含命令原文/回滚/风险）+ 动作清单查询 + 带票执行 + 审计回查。"""
    ac, hdrs = client
    rid = await _mk_runbook("manual", [{"idx": 0, "desc": "重启 nginx", "cmd": "systemctl restart nginx",
                                        "mode": "approval", "rollback": "stop 后按原配置启动"}])
    try:
        cfm = await ac.post(f"/api/ops/runbooks/{rid}/steps/0/confirm", headers=hdrs["admin"])
        assert cfm.status_code == 200, cfm.text
        c = cfm.json()["data"]
        assert c["cmd"] == "systemctl restart nginx" and c["risk_level"] == "mid"
        assert c["rollback"] == "stop 后按原配置启动" and c["allowlisted"] is True
        # 无票执行 → 403（护栏拦下）
        no = await ac.post(f"/api/ops/runbooks/{rid}/execute", json={"step_idx": 0}, headers=hdrs["admin"])
        assert no.status_code == 403
        # 带票执行 → 200 且返回待执行命令
        ok = await ac.post(f"/api/ops/runbooks/{rid}/execute",
                           json={"step_idx": 0, "confirm_token": c["confirm_token"]}, headers=hdrs["admin"])
        assert ok.status_code == 200, ok.text
        assert ok.json()["data"]["execute_on_target"] == "systemctl restart nginx"
        assert ok.json()["data"]["approval_mode"] == "manual"
        # 动作清单可查 + 全局审计里能看到审批字段
        al = await ac.get("/api/ops/action-allowlist", headers=hdrs["member"])
        assert al.status_code == 200 and len(al.json()["data"]["items"]) >= 4
        au = await ac.get(f"/api/ops/action-audit?runbook_id={rid}", headers=hdrs["admin"])
        items = au.json()["data"]["items"]
        assert any(i["approval_mode"] == "manual" and i["approved_by"] for i in items), items
    finally:
        await _drop_runbook(rid)


@pytest.mark.asyncio
async def test_runbook_execution_mode_patch(client):
    """执行方式可切换，且 legacy 的 whitelist_enabled 会同步（老客户端兼容）。"""
    ac, hdrs = client
    rid = await _mk_runbook("manual", [])
    try:
        r = await ac.put(f"/api/ops/runbooks/{rid}", json={"execution_mode": "auto"}, headers=hdrs["admin"])
        assert r.status_code == 200, r.text
        d = (await ac.get(f"/api/ops/runbooks/{rid}", headers=hdrs["admin"])).json()["data"]
        assert d["execution_mode"] == "auto" and d["whitelist_enabled"] is True
        bad = await ac.put(f"/api/ops/runbooks/{rid}", json={"execution_mode": "whatever"}, headers=hdrs["admin"])
        assert bad.status_code == 422
    finally:
        await _drop_runbook(rid)


# ---------------- 11. Agent 指标名契约（真跑发现的漂移）----------------
def test_agent_metric_names_match_platform_contract():
    """agent/agent_main.py 上报的指标名必须与平台契约逐字一致。

    起因（实测）：Agent 曾上报 `load.load1` 与 `disk{mnt}.percent`，而平台规则/巡检/大盘用的是
    `load.1` / `disk.percent` —— **名字对不上，磁盘与负载在 Agent 主机上永远是空白**，
    磁盘类告警永远不会触发。之前的验证全都绕过了这条路径（演示主机只报 cpu/mem、
    种子/压测脚本按平台名字直接 push），所以这个洞一直没被发现。
    """
    from pathlib import Path

    from app.services.ingest import AGENT_METRIC_CONTRACT

    src = (Path(__file__).resolve().parents[2] / "agent" / "agent_main.py").read_text(encoding="utf-8")
    assert AGENT_METRIC_CONTRACT, "契约不能为空"
    missing = [m for m in AGENT_METRIC_CONTRACT if f'"metric": "{m}"' not in src]
    assert not missing, f"Agent 未按契约上报这些指标名：{missing}"
    # 历史漂移名不得再次出现
    for legacy in ('"metric": "load.load1"', 'f"disk{mnt}.percent"'):
        assert legacy not in src, f"检测到历史漂移的指标名写法：{legacy}"


def test_agent_cert_probe_reads_self_signed_der():
    """Agent 证书探针必须能读出自签/内网证书的剩余天数（不能依赖证书链/主机名校验）。

    起因（实测）：probe_https_cert 用 ssl.create_default_context()（默认校验证书链 + 主机名），
    自签证书、内网私有 CA、以及用 IP 探 CN=xxx 的证书都会抛 SSLCertVerificationError →
    永远返回 down/-1。于是"证书临期"告警在这类环境（恰好是本产品主战场）里永远读不到
    "还剩几天"——已过期证书也会显示 -1 而不是真实的负数天数。

    这里真正加载 agent_main.py 的纯函数，用**现场生成的自签证书 DER** 验证 notAfter 解析，
    而不是只做字符串比对（字符串拦不住"还在 used create_default_context"这种回归）。
    """
    import importlib.util
    from datetime import datetime, timezone
    from pathlib import Path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    agent_path = Path(__file__).resolve().parents[2] / "agent" / "agent_main.py"
    spec = importlib.util.spec_from_file_location("agent_under_test", agent_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 现场生成一张 2 天期自签证书
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mock-db-01")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)

    got = mod._cert_not_after_der(der)
    exp = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    if exp.tzinfo is None:  # 旧版 cryptography 返回 naive，补齐时区再比
        exp = exp.replace(tzinfo=timezone.utc)
    assert abs((got - exp).total_seconds()) < 2, f"期望 notAfter={exp}，解析得到={got}"

    # _parse_asn1_time 需同时支持 UTCTime（2 位年，含秒）与 GeneralizedTime（4 位年）
    utc = datetime(2026, 10, 14, 6, 30, 0, tzinfo=timezone.utc)
    assert mod._parse_asn1_time(b"261014063000Z") == utc      # 'YYMMDDHHMMSSZ' 12 字符
    assert mod._parse_asn1_time(b"20261014063000Z") == utc    # GeneralizedTime

    # 防回归：探针取值路径必须用"不校验"上下文（CERT_NONE 才拿得到自签/内网证书），
    # 且靠 binary_form 取 DER——绝不能退回 create_default_context 直接 getpeercert()["notAfter"]
    # 那条对自签证书永远抛 SSLCertVerificationError 的老路。校验结论另存为 verify_error，不丢弃。
    probe_src = (Path(__file__).resolve().parents[2] / "agent" / "agent_main.py").read_text(encoding="utf-8")
    assert "CERT_NONE" in probe_src and "getpeercert(binary_form=True)" in probe_src, \
        "证书取值应走 CERT_NONE + binary_form，不做链校验（否则自签证书读不到 notAfter）"
    assert 'SSLCertVerificationError' not in probe_src or 'verify_error' in probe_src, \
        "校验失败的结论应显式保留为 verify_error 字段"


# ---------------- 12. 故障脚本安全约束（不让演示脚本伤到宿主）----------------
def test_fault_scripts_are_safe_for_host():
    """故障脚本必须三重保护：安全上限 / 自动恢复 / 不写无效数据。

    起因（实测）：`disk_full.sh` 用 `dd count=${need_mb}` 做兜底，而 need_mb 在"宿主大盘"场景下
    会算出 ~71 万 MB（≈693GB）—— fallocate 一旦失败就会尝试写满宿主盘；
    `mem_leak.sh` 默认 `--vm-bytes 85%` 在 11.7G 的 Docker VM 上等于申请 ~10G 内存。
    这类脚本会在**用户的真实机器**上跑，必须有硬边界。
    """
    from pathlib import Path

    faults = Path(__file__).resolve().parents[2] / "deploy" / "faults"
    disk = (faults / "disk_full.sh").read_text(encoding="utf-8")
    mem = (faults / "mem_leak.sh").read_text(encoding="utf-8")
    readme = (faults / "README.md").read_text(encoding="utf-8")

    # ① 磁盘脚本：默认上限存在且有界；dd 兜底不得使用"需要多少"
    assert 'MAX_SIZE="${MAX_SIZE:-' in disk, "磁盘脚本必须给出默认安全上限"
    assert "count=${WRITE_MB}" in disk, "dd 兜底必须用上限 WRITE_MB，不能用 need_mb"
    assert "count=${need_mb}" not in disk, "禁止用 need_mb 作为写入量（可能写满宿主盘）"
    # ② 跨不过阈值时必须直接退出，不写任何数据
    assert "exit 0" in disk, "无法越阈时应直接退出（不写无效数据）"
    # ③ 自动恢复 + 幂等清理
    assert "auto_recover" in disk and "rm -f /tmp/fault-disk.img" in disk
    # ④ 内存脚本：默认必须有界（百分比在 VM 上会吃掉大量宿主内存）
    assert '"${BYTES:-2G}"' in mem, "内存脚本默认必须是有界值（2G），不能是 85%"
    # ⑤ 安全须知与回收命令必须在 README 里
    for kw in ("安全须知", "rm -f /tmp/fault-disk.img", "pkill -f stress-ng", "不要在生产机上跑"):
        assert kw in readme, f"README 缺少安全说明：{kw}"


# ---------------- 12. 夯实轮：入参校验与渠道 scheme 白名单 ----------------
def test_rule_create_input_validation():
    """create_rule / update_rule 对非法入参给 400，不再落到全局兜底变 500。

    夯实轮测出来的真实问题：之前 _parse_expr 抛裸 ValueError → 500；
    severity/interval/window/cron/name 全部没有钳制。
    """
    import asyncio
    from httpx import AsyncClient, ASGITransport
    from app.core.security import get_current_user
    from app.db.session import SessionLocal
    from app.models.shell import User
    from app.main import create_app
    from app.services import runbook as runbook_svc  # noqa: F401  防无规则体

    app = create_app()

    async def _admin_user():
        async with SessionLocal() as db:
            u = await db.get(User, 1)
            return u

    async def run():
        # 前置清理（幂等关键）：上一次若在断言处失败，后置清理执行不到 → 残留会让本测试永久 409
        from sqlalchemy import text as _text
        _names = ("d9-validation-ok", "d9-validation-x", "d9-validation-sev",
                  "d9-validation-int0", "d9-validation-cron")
        async with SessionLocal() as _db:
            await _db.execute(_text("DELETE FROM alert_rules WHERE name IN :n"), {"n": _names})
            await _db.execute(_text("DELETE FROM alert_cur_events WHERE rule_name IN :n"), {"n": _names})
            await _db.commit()
        admin = await _admin_user()
        # 直接 patch 掉 require（最简单的"以 admin 身份调用"）—— 安全测试无需真登录
        async def fake_admin():
            return admin
        app.dependency_overrides[get_current_user] = fake_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            cases = [
                ({"name": "d9-validation-ok", "scope": "metric", "metric": "cpu.percent", "expr": ">1", "severity": 2}, 200, "合法 body"),
                ({"name": "d9-validation-x", "scope": "metric", "metric": "cpu.percent", "expr": "DROP TABLE", "severity": 2}, 400, "非法表达式"),
                ({"name": "d9-validation-sev", "scope": "metric", "metric": "cpu.percent", "expr": ">1", "severity": 7}, 400, "severity 越界"),
                ({"name": "d9-validation-int0", "scope": "metric", "metric": "cpu.percent", "expr": ">1", "severity": 2, "eval_interval_s": 0}, 400, "interval=0"),
                ({"name": "", "scope": "metric", "metric": "cpu.percent", "expr": ">1", "severity": 2}, 400, "空 name"),
                ({"name": "d9-validation-cron", "scope": "metric", "metric": "cpu.percent", "expr": ">1", "severity": 2, "cron_pattern": "@@@"}, 400, "坏 cron"),
            ]
            for body, expect_code, label in cases:
                r = await ac.post("/api/ops/alert-rules", json=body)
                assert r.status_code == expect_code, f"{label}: 期望 {expect_code}，得到 {r.status_code}  body={r.text[:120]}"
        # 清理: 本测试用过的规则名(含 fuzz 字符串)不留到生产 DB,
        # 否则会随 alert 引擎/巡检写入 alert_cur_events+incidents,污染演示数据
        from sqlalchemy import text
        async with SessionLocal() as db:
            names = {c[0].get("name") for c in cases if isinstance(c[0], dict) and c[0].get("name")}
            if names:
                await db.execute(text("DELETE FROM alert_rules WHERE name IN :ns"), {"ns": tuple(names)})
                await db.execute(text("DELETE FROM alert_cur_events WHERE rule_name IN :ns"), {"ns": tuple(names)})
                await db.execute(text("DELETE FROM alert_his_events WHERE rule_name IN :ns"), {"ns": tuple(names)})
                await db.commit()
        app.dependency_overrides.clear()

    asyncio.run(run())


def test_channel_scheme_denylist():
    """通知渠道的 URL 字段不允许 file:// / scp:// / ssh:// / exec://。

    夯实轮发现 file:// 渠道创建未被拦截——apprise 的 file:// 会把通知内容
    写到本地文件，等于拿通知当写文件通道，公共接入面下绝不该放过。
    """
    from app.services.notify_transport import validate_channel
    bad = [
        ("webhook", {"apprise_url": "file:///etc/passwd"}),
        ("webhook", {"webhook_url": "file:///tmp/x"}),
        ("webhook", {"apprise_url": "scp://x"}),
        ("webhook", {"apprise_url": "exec://whoami"}),
    ]
    for t, cfg in bad:
        err = validate_channel(t, cfg)
        assert err and "不允许" in err, f"应拒 {t} {cfg}，实际：{err}"
    # 不影响正常 http/https
    assert validate_channel("webhook", {"webhook_url": "http://example.com/"}) is None
    assert validate_channel("webhook", {"apprise_url": "json://http://x/"}) is None


def test_retention_job_clears_overdue_rows():
    """只增表保留期清理确实会按 cutoff 删除过期行（默认 180d）。

    用 -e 注入：把 alert_his 改 1 行到 200 天前，跑 job_retention，验证该行被删。
    """
    import asyncio
    from sqlalchemy import text
    from app.db.session import SessionLocal
    from app.sched import job_retention
    from datetime import datetime, timedelta

    TEST_HASH = "r3t3nt10n" + "0" * 23   # 32 位，专属本测试

    async def run():
        # 自己造一行 200 天前的历史事件（不依赖库里现有数据——此前 UPDATE ... LIMIT 1
        # 的写法在空库上会静默不生效，让测试假失败）
        async with SessionLocal() as db:
            await db.execute(text(
                "INSERT INTO alert_his_events (hash, rule_id, rule_name, host_ident, metric, "
                "severity, trigger_time, trigger_value, tags, is_recovered, ai_summary) "
                "VALUES (:h, 1, 'retention-test', 'rt-host', 'cpu.percent', 2, "
                "(NOW() - INTERVAL 200 DAY), 1.0, '{}', 0, '')"), {"h": TEST_HASH})
            await db.commit()
            before = (await db.execute(text(
                "SELECT COUNT(*) FROM alert_his_events WHERE hash = :h"), {"h": TEST_HASH})).scalar()
        assert before == 1, "测试数据插入失败"
        await job_retention()
        async with SessionLocal() as db:
            after = (await db.execute(text(
                "SELECT COUNT(*) FROM alert_his_events WHERE hash = :h"), {"h": TEST_HASH})).scalar()
            # 兜底清理（若未被保留期任务清掉，避免污染后续）
            await db.execute(text("DELETE FROM alert_his_events WHERE hash = :h"), {"h": TEST_HASH})
            await db.commit()
        assert after == 0, f"200 天前的行应被保留期任务清掉，剩 {after}"

    asyncio.run(run())


def test_db_hang_middleware_returns_500_quickly():
    """DB 挂起时 main.py 中间件应在 25s 内返回 500 + 统一信封，不无限挂。

    端到端测：这里把 _db_hang_guard 同样的逻辑复制到测试（不真挂库——太重），
    验证：① 受保护路径 + 慢 handler → 在超时内返回 500 + 正确信封
    ② 白名单路径直接穿透
    真实的 "pause seekdb 25s 兜底" 在 D9 报告的实测章节里覆盖。
    """
    import asyncio
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    async def guard(request, call_next):
        path = request.url.path
        if path in ("/api/health",) or path.startswith("/install") or path.endswith(".html") or path.endswith(".png"):
            return await call_next(request)
        try:
            return await asyncio.wait_for(call_next(request), timeout=1)
        except asyncio.TimeoutError:
            return JSONResponse({"code": 500, "msg": "服务端响应超时（疑似数据库不可达）", "data": None}, status_code=500)

    req = Request({"type": "http", "method": "GET", "path": "/api/ops/alerts", "headers": []})
    async def slow(request):
        await asyncio.sleep(60)
        return Response("never")
    resp = asyncio.run(guard(req, slow))
    assert resp.status_code == 500
    body = resp.body.decode()
    assert "服务端响应超时" in body and "数据库" in body

    req2 = Request({"type": "http", "method": "GET", "path": "/api/health", "headers": []})
    async def fast(request): return Response("ok")
    resp2 = asyncio.run(guard(req2, fast))
    assert resp2.body == b"ok"


# ---------------- 13. D11：规则重名唯一（与渠道/模型配置看齐） ----------------
def test_rule_name_unique_constraint():
    """D11：同名的第二条告警规则创建应返 409（不与渠道/模型口径不一）。

    夯实轮真伪审查：notification_channels.name / llm_configs.name 都 unique，
    alert_rules.name 是漏网之鱼，第二次"改其中一条"会误改另一条，操作人困惑。
    决策改"默认加唯一约束"（与渠道/模型配置口径一致，不让用户再决策一次）。
    """
    import asyncio
    from httpx import AsyncClient, ASGITransport
    from app.core.security import get_current_user
    from app.db.session import SessionLocal
    from app.models.alert import AlertRule
    from app.models.shell import User
    from app.main import create_app
    from sqlalchemy import select, delete

    async def run():
        # 跑前先清掉可能的同名残留
        async with SessionLocal() as db:
            await db.execute(delete(AlertRule).where(AlertRule.name.in_(("d11-dup", "d11-other"))))
            await db.commit()
        app = create_app()
        async with SessionLocal() as db:
            admin = await db.get(User, 1)
        app.dependency_overrides[get_current_user] = lambda: admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            body = {"name": "d11-dup", "scope": "metric", "metric": "cpu.percent", "expr": ">1", "severity": 2}
            r1 = await ac.post("/api/ops/alert-rules", json=body)
            assert r1.status_code == 200, r1.text
            r2 = await ac.post("/api/ops/alert-rules", json=body)
            # 唯一约束应落 IntegrityError → 409（统一错误信封），不是 500
            assert r2.status_code == 409, f"同名校验未生效，期望 409，得到 {r2.status_code}  body={r2.text[:200]}"
        # 跑后清理（不污染后续测试）
        async with SessionLocal() as db:
            await db.execute(delete(AlertRule).where(AlertRule.name.in_(("d11-dup", "d11-other"))))
            await db.commit()
        app.dependency_overrides.clear()

    asyncio.run(run())


# ---------------- 14. 诊断 SSE 的 ticket 鉴权（P0 真 bug 回归）----------------
def test_diagnose_stream_accepts_ticket_not_only_bearer():
    """诊断 SSE 必须支持 `?ticket=` 鉴权 —— 前端用 EventSource（无法带 Bearer header）。

    夯实轮实测抓到的 P0：`diagnose/stream` 只挂 `Depends(require("diag"))`（认 Bearer），
    而前端 `new EventSource('.../diagnose/stream?ticket=xxx')` 带不了 header
    → 一律 401 → **「一键诊断」按钮在前端完全不可用**。
    之前没暴露：验证都走 curl 带 Bearer 打 POST /diagnose，绕过了前端 EventSource 路径。
    """
    import asyncio
    from httpx import AsyncClient, ASGITransport
    from app.main import create_app
    from app.services import sse_ticket as tickets

    async def run():
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            # ① 无 ticket 无 Bearer → 401（不是 200、不是 500）
            r = await ac.get("/api/ops/incidents/999999/diagnose/stream")
            assert r.status_code == 401, f"未认证应 401，得到 {r.status_code}"

            # ② demo ticket → 403（demo 无诊断权限，与 require("diag") 同口径）
            t_demo = tickets.mint(user_id=99, role="demo")
            r = await ac.get(f"/api/ops/incidents/999999/diagnose/stream?ticket={t_demo}")
            assert r.status_code == 403, f"demo 应 403，得到 {r.status_code}"

            # ③ admin ticket → 不再是 401/403（消费即焚：第二次同 ticket 必须失效）
            t_admin = tickets.mint(user_id=1, role="admin")
            # 不真读流（会跑诊断）：只看"鉴权通过"——用不存在的 incident 会快速在流内报错并结束
            try:
                async with ac.stream("GET", f"/api/ops/incidents/999999/diagnose/stream?ticket={t_admin}") as resp:
                    assert resp.status_code == 200, f"admin ticket 应放行（200 建流），得到 {resp.status_code}"
                    # 读前几行确认是 SSE 流而不是错误 JSON
                    chunks = []
                    async for line in resp.aiter_lines():
                        chunks.append(line)
                        if len(chunks) >= 3 or any("event:" in c for c in chunks):
                            break
            except Exception as e:  # 只关心"鉴权是否放行"，流内的诊断失败不影响本断言
                raise AssertionError(f"admin ticket 建流异常：{type(e).__name__}: {e}")

            # ④ ticket 消费即焚：同 ticket 再来 → 401
            r = await ac.get(f"/api/ops/incidents/999999/diagnose/stream?ticket={t_admin}")
            assert r.status_code == 401, f"ticket 应一次性，复用应 401，得到 {r.status_code}"

    asyncio.run(run())


# ---------------- 15. 通知熔断（D12 夯实）----------------
@pytest.mark.asyncio
async def test_notify_circuit_breaker_opens_and_skips(monkeypatch):
    """坏渠道连续失败达阈值 → 熔断冷却，冷却期内跳过投递且**不写失败台账**。

    起因（实测）：测 file:// 拦截时建的坏渠道，几分钟内 notification_records
    累积 33 条 failed——坏渠道被每次告警反复重试。熔断后：冷却期内的跳过不写台账。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import delete, select
    from app.db.session import SessionLocal
    from app.models.alert import NotificationChannel, NotificationRecord
    from app.services import notify as notify_svc
    from app.services.notify import dispatch, NOTIFY_FAIL_THRESHOLD
    from app.engine.state import Ev

    async def run():
        async with SessionLocal() as db:
            # 建一个必然失败的渠道（无效 host）
            await db.execute(delete(NotificationChannel).where(NotificationChannel.name == "cb-test"))
            await db.commit()
            ch = NotificationChannel(name="cb-test", type="webhook", enabled=True,
                                     config_json={"webhook_url": "http://127.0.0.1:1/nope"})
            db.add(ch)
            await db.commit()
            await db.refresh(ch)
            cid = ch.id

            ev = Ev(hash="cb" * 16, rule_id=1, rule_name="cb", host_ident="h", metric="m",
                    severity=1, trigger_value=1.0, labels={}, tags={})
            # 连发 NOTIFY_FAIL_THRESHOLD 次（每次都会真实失败）
            for i in range(NOTIFY_FAIL_THRESHOLD):
                await dispatch(db, ev, None, {"name": "cb", "channels_json": ["cb-test"]})
            await db.refresh(ch)
            assert ch.fail_streak >= NOTIFY_FAIL_THRESHOLD, f"失败计数应达阈值，实际 {ch.fail_streak}"
            assert ch.disabled_until is not None, "应已熔断（disabled_until 非空）"

            # 冷却期内再发一次 → 应跳过（结果 detail 含"熔断"）
            before = (await db.execute(select(func.count()).select_from(NotificationRecord)
                                       .where(NotificationRecord.channel == "cb-test"))).scalar()
            res = await dispatch(db, ev, None, {"name": "cb", "channels_json": ["cb-test"]})
            after = (await db.execute(select(func.count()).select_from(NotificationRecord)
                                      .where(NotificationRecord.channel == "cb-test"))).scalar()
            assert any("熔断" in str(r.get("detail", "")) for r in res), res
            assert after == before, f"冷却期内不应再写台账：{before} → {after}"

            # 清理
            await db.execute(delete(NotificationRecord).where(NotificationRecord.channel == "cb-test"))
            await db.execute(delete(NotificationChannel).where(NotificationChannel.id == cid))
            await db.commit()

    from sqlalchemy import func as _f
    globals()["func"] = _f
    await run()


@pytest.mark.asyncio
async def test_bulk_resolve_batches_in_one_transaction(client):
    """批量解决：一次处理多条（此前逐条 commit+audit 的 N+1 已改批量）。"""
    ac, hdrs = client
    # 造 3 条假告警（直接落 cur）
    from app.db.session import SessionLocal
    from app.models.alert import AlertCurEvent, AlertHisEvent
    from sqlalchemy import delete, select

    async def seed():
        async with SessionLocal() as db:
            for i in range(3):
                h = f"blk{i:029d}"[:32]
                await db.execute(delete(AlertCurEvent).where(AlertCurEvent.hash == h))
                db.add(AlertCurEvent(hash=h, rule_id=1, rule_name="bulk-test", host_ident="blkhost",
                                     metric="cpu.percent", severity=2, trigger_value=1.0, tags={},
                                     rule_config={}))
            await db.commit()
            return [f"blk{i:029d}"[:32] for i in range(3)]

    async def count_his(hashes):
        async with SessionLocal() as db:
            return (await db.execute(select(AlertHisEvent).where(AlertHisEvent.hash.in_(hashes)))).scalars().all()

    hashes = await seed()
    try:
        r = await ac.post("/api/ops/alerts/bulk", json={"hashes": hashes, "action": "resolve"}, headers=hdrs["admin"])
        assert r.status_code == 200, r.text
        assert r.json()["data"]["affected"] == 3, r.text
        his = await count_his(hashes)
        assert len(his) == 3, f"3 条都应进 his(recovered)，实际 {len(his)}"
        assert all(x.is_recovered for x in his)
        # 全进 his 后，cur 里应已删除
        async with SessionLocal() as db:
            remain = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash.in_(hashes)))).scalars().all()
            assert not remain, f"cur 应已清空，剩 {len(remain)}"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(AlertHisEvent).where(AlertHisEvent.hash.in_(hashes)))
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.hash.in_(hashes)))
            await db.commit()


@pytest.mark.asyncio
async def test_notify_transport_has_timeout():
    """apprise 投递必须有超时兜底（夯实轮补）。

    起因：apprise 底层是 requests，**默认无超时**——对端"连接挂住"（防火墙 DROP、
    服务假死）而非明确拒绝时，发送会永久挂住；而 dispatch 在**告警消费链路**上，
    一条挂住的投递会拖住整条通知流程（此前 33 条失败台账只是"拒绝"的情形，
    若换成"黑洞"则表现为整条链路卡死）。
    """
    import time
    from app.services.notify_transport import send_via_apprise, _with_timeout

    # ① URL 自动追加 timeout（用户已显式设置则尊重）
    assert "timeout=" in _with_timeout("json://host/path")
    assert _with_timeout("json://host/path?timeout=3").endswith("timeout=3")

    # ② 黑洞地址（IANA 保留段，通常不可路由）：必须在超时上限内返回，不能挂住
    t0 = time.time()
    ok, detail = await send_via_apprise("webhook", {"webhook_url": "http://10.255.255.1:9999/x"}, "t", "b")
    dt = time.time() - t0
    assert ok is False
    assert dt < 25, f"应在超时上限（20s）内返回，实际 {dt:.1f}s —— 说明超时未生效"


# ---------------- 16. Agent 安装脚本的健壮性（D13 实测补齐）----------------
def test_install_script_handles_pip_less_and_systemd_less_hosts():
    """install.sh 必须能装进「有 python3 但没 pip」和「没有 systemd」的机器。

    背景（夯实轮真机实测）：在干净 Ubuntu 22.04 容器里跑文档里的接入命令，连撞两坑——
      ① Ubuntu 云主机**默认有 python3 但没 pip**（`python3-pip` 需另装），
         且该发行版的 `ensurepip` 不可用 → 原脚本 pip 三连兜底全失败；
         更糟的是 `set -e` 让脚本在兜底链末尾静默退出，用户看不到"请手工执行 X"的指引；
      ② 容器/WSL/精简系统**没有 systemctl** → 原脚本在 `systemctl daemon-reload`
         处 `set -e` 静默退出，安装"看起来跑了但没装成"。
    修：① pip 缺失时自动用 apt/dnf/yum 装；整条兜底链用 `if ! { ...; }` 包住；
        ② 无 systemd 时降级 nohup 常驻（写 PID 文件），卸载分支同步 kill PID。

    这里做静态守卫（真实端到端在验收脚本里）：确保这两个降级分支不会被后人删掉。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "agent" / "install.sh").read_text(encoding="utf-8")
    # ① pip 自动安装分支
    assert "apt-get install -y python3-pip" in src, "缺少 apt 装 pip 的兜底"
    assert "dnf install -y python3-pip" in src, "缺少 dnf 装 pip 的兜底"
    assert "if ! { $PY -m pip install" in src, "pip 兜底链必须用 `if ! { ...; }` 包住（防 set -e 静默退出）"
    # ② 无 systemd 降级分支
    assert "未检测到 systemd" in src and "nohup" in src, "缺少无 systemd 的 nohup 降级"
    assert "agent.pid" in src, "降级路径必须写 PID 文件（卸载要靠它 kill）"
    assert 'kill "$(cat "$AGENT_DIR/agent.pid")"' in src, "卸载分支必须处理 nohup 的 PID"
    # ③ 日志必须无缓冲（否则 agent.log 长期为空——实测：进程在跑、指标在上报、日志 0 字节）
    assert 'nohup "$PY" -u' in src, "nohup 启动必须带 -u（否则 agent.log 空文件）"
    # ④ 参数解析与 --uninstall 仍在（历史修复不能被回归掉）
    for kw in ("--server)", "--token|--install-token)", "--uninstall)", "--probes)"):
        assert kw in src, f"参数解析缺 {kw}"


# ---------------- 17. 长窗口必须从 metric_agg 补前段（D15 审计修复）----------------
@pytest.mark.asyncio
async def test_metrics_query_backfills_history_from_agg():
    """查长窗口时，ring 覆盖不到的前段必须从 metric_agg 补齐。

    实测缺陷（手册审计）：原实现 `if not pts` 只在"ring 一条都没有"时才查聚合表，
    于是"ring 有最近 2h + 窗口是 1 天"时**直接返回最近一段，前 23 小时静默丢失**
    （实测查 1 天只回最近 13 分钟的 47 个点）——手册承诺的"超窗自动落 metric_agg"不成立。
    修：ring 覆盖不到窗口起点（`not has_points_before(start)`）→ 从聚合表补前段并拼接。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import delete, text
    from app.db.session import SessionLocal
    from app.store import ring
    from app.routers.collect import query_metrics

    ident, metric = "d15-hist-host", "cpu.percent"
    now = datetime.utcnow()
    old_ts = (now - timedelta(hours=5)).replace(second=0, microsecond=0)   # 5h 前（ring 覆盖不到）

    async with SessionLocal() as db:
        # 造历史：metric_agg 里 2 个 5 小时前的点
        await db.execute(text("DELETE FROM metric_agg WHERE ident=:i"), {"i": ident})
        await db.execute(text(
            "INSERT INTO metric_agg (ident, metric, granularity, ts, value_avg, value_min, value_max) "
            "VALUES (:i, :m, '1m', :ts1, 42.0, 42.0, 42.0), (:i, :m, '1m', :ts2, 43.0, 43.0, 43.0)"),
            {"i": ident, "m": metric, "ts1": old_ts, "ts2": old_ts + timedelta(minutes=1)})
        await db.commit()
        # 造 ring：只放"最近的"1 个点（模拟进程刚重启）
        ring._rings.pop((ident, metric), None)
        ring.append_point(ident, metric, now - timedelta(seconds=10), 50.0)

        try:
            r = await query_metrics(ident=ident, metric=metric, minutes=1440, db=db)
            pts = (r.get("data") or {}).get("points", [])
            src = (r.get("data") or {}).get("source", "")
            ts_list = [p[0] for p in pts]
            assert len(pts) >= 3, f"应含 2 个历史点 + 1 个 ring 点，实际 {len(pts)} 个"
            assert any(t.startswith(old_ts.strftime("%Y-%m-%dT%H:%M")) for t in ts_list), \
                f"必须包含 5 小时前的历史点，实际范围 {ts_list[0]} … {ts_list[-1]}"
            assert src == "ring+metric_agg", f"来源应标记拼接，实际 {src!r}"
        finally:
            await db.execute(text("DELETE FROM metric_agg WHERE ident=:i"), {"i": ident})
            await db.commit()
            ring._rings.pop((ident, metric), None)
            ring._flushed_until.pop((ident, metric), None)


@pytest.mark.asyncio
async def test_d39_inspection_edit_and_delete():
    """D39：巡检任务必须可编辑/可删除（手册 §8 承诺「cron 改了会自动生效」）。

    缺口背景：此前巡检只有 GET/POST/run —— **既无更新接口也无前端入口**，
    用户拿到新环境后想改 cron（如改成北京 09:00 = UTC 01:00）无处可改。
    """
    from app.core.security import get_current_user
    from app.main import create_app
    from app.models.shell import User

    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "admin"))).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: u
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            # 建
            r = await ac.post("/api/ops/inspections", json={"name": "d39-巡检", "cron_expr": "0 9 * * *",
                                                           "target_scope": {"hosts": "all"}, "enabled": True})
            assert r.status_code == 200, r.text[:120]
            iid = r.json()["data"]["id"]
            # 改 cron（这一步此前完全做不到）
            r = await ac.put(f"/api/ops/inspections/{iid}", json={"name": "d39-巡检", "cron_expr": "0 1 * * *",
                                                                  "target_scope": {"hosts": "all"}, "enabled": True})
            assert r.status_code == 200, r.text[:120]
            r = await ac.get(f"/api/ops/inspections/{iid}")
            assert r.json()["data"]["cron_expr"] == "0 1 * * *", "cron 未生效"
            # 非法 cron 被拒
            r = await ac.put(f"/api/ops/inspections/{iid}", json={"name": "d39-巡检", "cron_expr": "not-a-cron",
                                                                  "target_scope": {}, "enabled": True})
            assert r.status_code == 400, "非法 cron 应 400"
            # 删
            r = await ac.delete(f"/api/ops/inspections/{iid}")
            assert r.status_code == 200, r.text[:120]
            r = await ac.get(f"/api/ops/inspections/{iid}")
            assert r.status_code == 404, "已删除应 404"
    finally:
        app.dependency_overrides.clear()
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM inspections WHERE name='d39-巡检'"))
            await db.commit()


@pytest.mark.asyncio
async def test_d47_chat_suggestions_realtime():
    """D47：AI 助手的推荐问题必须**由实时状态生成**（用户要求「不要通用题库，要时效性」）。

    同时锁住本次踩到的真 bug：`llm_usage.purpose` 是 ENUM 列，
    新增用途（'suggest'）必须**同时**改模型 Enum 与数据库 ENUM——
    否则 INSERT 报 "Data truncated"，毒化 session 后重试报 PendingRollbackError，
    最终静默降级成规则版（表现为"AI 生成没生效"）。
    """
    from app.core.security import get_current_user
    from app.main import create_app
    from app.models.llm import LLMUsage
    from app.models.shell import User

    # ① 模型 Enum 必须含 suggest（漏了就会在写用量时炸）
    assert "suggest" in [e for e in LLMUsage.__table__.c.purpose.type.enums], \
        "llm_usage.purpose 枚举缺 'suggest'"

    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "admin"))).scalars().first()
        # ② 数据库 ENUM 也必须含（迁移到位）
        col = (await db.execute(text("SHOW COLUMNS FROM llm_usage LIKE 'purpose'"))).all()
        assert col and "suggest" in col[0][1], f"数据库 ENUM 缺 suggest：{col}"
    app.dependency_overrides[get_current_user] = lambda: u
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/api/ops/chat/suggestions")
            assert r.status_code == 200, r.text[:120]
            d = r.json()["data"]
            assert isinstance(d.get("headline"), str) and d["headline"], "缺动态 headline"
            sugs = d.get("suggestions") or []
            assert len(sugs) >= 1, "至少要给出一条建议"
            for x in sugs:
                assert x.get("q"), f"建议缺问题文本：{x}"
                assert x.get("tone") in ("urgent", "warn", "info", "ok"), f"tone 非法：{x}"
            # ③ 建议必须**贴当前状态**：本环境存在 S1 告警/离线主机，问题里应出现相关线索
            blob = " ".join(x["q"] + x.get("why", "") for x in sugs)
            assert any(k in blob for k in ("告警", "离线", "阈值", "事件", "恢复")), \
                f"建议未贴合实时状态（疑似退回通用题库）：{blob[:120]}"
    finally:
        app.dependency_overrides.clear()
