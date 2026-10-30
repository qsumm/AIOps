"""D160：告警规则的「目标名」在探活/服务域的语义与死规则防护。

**真机验收发现的缺陷（D160-1）**：scope=probe / scope=service 时，引擎把
`alert_rules.metric` 当作**探活名 / 服务名**过滤（core.py `_eval_probe_once` /
`_eval_service_once`）；而前端对所有作用域一律默认填 `cpu.percent` → 引擎去找一个
不存在的探活/服务名 → **零行 → 规则永不触发**（靶机真机实测：种子里的 S1
「探活失败」「服务不可用」两条规则 4 天 fires=0，nginx 真挂了也不报）。

本套件锁死三条防线：
  ① 写入口径：探活/服务域填了不存在的名字 → **400 人话报错**（不再静默零命中）；
  ② 运行口径：过滤后零行 → **引擎 warning 日志**点名规则与所填名字（存量脏数据可见）；
  ③ 前端口径：目标名默认值随作用域（指标域默认 cpu.percent；探活/服务域必须留空）。

另含 D160-2 的守卫：Agent 必须把 **204 当成功**（平台 /agents/metrics 成功就是
204 No Content；此前只认 200 → 每次成功上报都被当成失败、每 15 秒刷假错误、
缓存反复重投、"首个上报已确认"信号永不点亮）。
"""
from __future__ import annotations

import pathlib
import re
import uuid

import pytest
from sqlalchemy import select

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ── ① 写入口径：名字写错 → 400（探活/服务域）──────────────────────
@pytest.mark.asyncio
async def test_probe_rule_rejects_unknown_target_name():
    """探活域填了不存在的探活名 → 400，并说清「留空=全部」。"""
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login",
                              json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        body = {"name": f"d160-probe-bad-{_uid()}", "scope": "probe",
                "metric": "并不存在的探活项", "expr": ">0", "severity": 3}
        r = await ac.post("/api/ops/alert-rules", json=body, headers=h)
        assert r.status_code == 400, f"探活名写错必须 400，实际 {r.status_code}: {r.text[:200]}"
        msg = r.json()["msg"]
        assert "并没有" in msg or "没有叫" in msg, f"报错要指名哪个名字不对：{msg}"
        assert "留空" in msg, f"报错要指路（留空=全部）：{msg}"


@pytest.mark.asyncio
async def test_service_rule_rejects_unknown_target_name():
    """服务域同理：填了不存在的服务名 → 400。"""
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login",
                              json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        body = {"name": f"d160-svc-bad-{_uid()}", "scope": "service",
                "metric": "并不存在的服务", "expr": ">1", "severity": 3}
        r = await ac.post("/api/ops/alert-rules", json=body, headers=h)
        assert r.status_code == 400, f"服务名写错必须 400，实际 {r.status_code}: {r.text[:200]}"
        assert "留空" in r.json()["msg"], r.json()["msg"]


@pytest.mark.asyncio
async def test_probe_rule_allows_empty_and_real_target_name():
    """留空=全部、填真实存在的探活名 → 都能建（别把正常用法也挡了）。"""
    from httpx import ASGITransport, AsyncClient
    from app.db.session import SessionLocal
    from app.main import app
    from app.models.collect import Probe

    async with SessionLocal() as db:
        real = (await db.execute(select(Probe.name).limit(1))).scalars().first()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    created: list[int] = []
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login",
                              json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        r1 = await ac.post("/api/ops/alert-rules", json={
            "name": f"d160-probe-empty-{_uid()}", "scope": "probe", "metric": "",
            "expr": ">0", "severity": 3}, headers=h)
        assert r1.status_code == 200, f"探活域留空必须可建：{r1.text[:200]}"
        created.append(r1.json()["data"]["id"])
        if real:
            r2 = await ac.post("/api/ops/alert-rules", json={
                "name": f"d160-probe-real-{_uid()}", "scope": "probe", "metric": real,
                "expr": ">0", "severity": 3}, headers=h)
            assert r2.status_code == 200, f"真实探活名必须可建：{r2.text[:200]}"
            created.append(r2.json()["data"]["id"])
        for rid in created:
            await ac.delete(f"/api/ops/alert-rules/{rid}", headers=h)


# ── ② 运行口径：零行 → warning 日志（存量脏数据可见）────────────────
@pytest.mark.asyncio
async def test_engine_warns_when_probe_target_name_has_no_match(caplog):
    """探活名在库里不存在 → 本轮零命中，且日志点名规则与名字（不再静默）。"""
    import logging
    from app.db.session import SessionLocal
    from app.engine.core import RuleWorker
    from app.models.alert import AlertRule

    async with SessionLocal() as db:
        rule = AlertRule(name=f"d160-零命中自检-{_uid()}", scope="probe", metric="并不存在的探活项",
                         expr=">0", severity=3, algorithm="threshold",
                         eval_interval_s=15, window_s=300, for_duration_s=0,
                         recover_duration_s=60, disabled=True)
        db.add(rule)
        await db.commit()
        rid = rule.id
    try:
        caplog.set_level(logging.WARNING, logger="engine")
        async with SessionLocal() as db:
            r = (await db.execute(select(AlertRule).where(AlertRule.id == rid))).scalar_one()
            out = await RuleWorker(r)._eval_probe_once()
        assert out == [], "名字不存在的探活域本轮必须零命中"
        assert any("并不存在的探活项" in m for m in caplog.messages), \
            f"零命中必须打 warning 日志点名（caplog={caplog.messages[-3:]}）"
    finally:
        from sqlalchemy import text
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM alert_rules WHERE id = :i"), {"i": rid})
            await db.commit()


# ── ③ 前端口径：目标名默认值随作用域 ────────────────────────────
def test_frontend_target_default_follows_scope():
    """rules.js：目标名不得对所有作用域一律默认 cpu.percent（探活/服务域必须留空）。"""
    src = (ROOT / "static" / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
    assert 'value: rule?.metric || "cpu.percent"' not in src, \
        "目标名输入框不得默认填 cpu.percent（探活/服务域那是探活名/服务名）"
    assert "syncTargetDefault" in src, "缺少 scope 联动（切作用域时目标名默认值要跟着变）"
    assert 'f.scope.addEventListener("change", syncTargetDefault);' in src, "scope 变化必须真的联动"
    assert re.search(r"syncTargetDefault\(\);\s*\n", src), "打开抽屉时必须立即按当前作用域同步默认值"
    assert 'f.metric.value = "cpu.percent"' in src, "指标域应保留 cpu.percent 便捷默认"
    assert 'else if (f.metric.value.trim() === "cpu.percent") { f.metric.value = ""; }' in src, \
        "切到探活/服务域必须清掉 cpu.percent（否则又是死规则）"


# ── ④ Agent：204 = 成功（D160-2）─────────────────────────────────
def test_agent_accepts_204_as_success():
    """Agent 上报与缓存续传都必须接受 204（平台成功就是 204 No Content）。"""
    src = (ROOT / "agent" / "agent_main.py").read_text(encoding="utf-8")
    assert src.count("not in (200, 204)") >= 2, \
        "上报与缓存续传两处都要接受 204（否则成功被当成失败 → 假错误 + 缓存反复重投）"
    # 上报点与续传点必须都在 204 容忍集合里（注册/配置端点仍应严格 200，不在此断言范围）
    up = src.index('f"{args.server}/api/ops/agents/metrics"')
    assert "not in (200, 204)" in src[up:up + 700], "指标上报点必须接受 204"
    batch = src.index('json={"batch": batch}')
    assert "not in (200, 204)" in src[batch:batch + 400], "缓存续传点必须接受 204"
