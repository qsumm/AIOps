"""D7 集成测试（按 06 §2 的 IT 清单，覆盖本机可确定性验证的用例）。

覆盖：
  IT-告警实时（SSE）· IT-重启回放 · IT-屏蔽 NotifyOnly · IT-异常检测对比（阶跃/尖峰/渐变）
  IT-断网续传 · IT-LLM 降级 · IT-Runbook 五段 + UNVALIDATED 拒入 · IT-权限矩阵
不可在本机覆盖的（真实容器/真实模型/停机时间窗）见 docs/04-交付与运营/测试覆盖报告.md。
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.db.session import SessionLocal
from app.engine import core
from app.engine.detect import detect
from app.main import app
from app.models.alert import AlertCurEvent, AlertHisEvent, AlertMute, AlertRule
from app.models.host import Host
from app.models.ops import ActionAudit, Runbook, RunbookVersion
from app.store import ring

HOST = "d7-int-host"


async def _login(ac) -> str:
    r = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["data"]["access_token"]


async def _ensure_host(atoken_iter=None):
    async with SessionLocal() as db:
        h = (await db.execute(select(Host).where(Host.ident == HOST))).scalar_one_or_none()
        if h is None:
            h = Host(ident=HOST, name="d7-int", ip="10.7.7.7", os="test", status="up", agent_token="d7-token")
            db.add(h)
            await db.commit()
    return "d7-token"


async def _make_rule(**kw) -> int:
    async with SessionLocal() as db:
        r = AlertRule(name=kw.pop("name", "d7-rule"), scope="metric", metric="cpu.percent", expr=">90",
                      severity=2, eval_interval_s=5, window_s=300, algorithm="threshold",
                      recover_duration_s=0, **kw)
        db.add(r)
        await db.commit()
        await db.refresh(r)
        return r.id


async def _cleanup(rule_ids=(), purge_host=HOST):
    async with SessionLocal() as db:
        for rid in rule_ids:
            await db.execute(delete(AlertRule).where(AlertRule.id == rid))
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.rule_id == rid))
            await db.execute(delete(AlertHisEvent).where(AlertHisEvent.rule_id == rid))
        if purge_host:
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.host_ident == purge_host))
            await db.execute(delete(AlertHisEvent).where(AlertHisEvent.host_ident == purge_host))
        await db.commit()
    for rid in rule_ids:
        w = core.workers.pop(rid, None)
        if w:
            await w.stop()
    ring._rings.pop((purge_host, "cpu.percent"), None)


async def _drive(rule_id: int):
    """只驱动被测规则：直接构造 worker 并手动评估一次 + 消费事件。

    刻意**不走 core.reconcile()**：它会为库里所有启用规则起 worker，测试就会受
    dev/演示数据影响（也可能收到别的规则推来的 SSE/队列事件）——D7 实测踩到。
    """
    w = core.workers.get(rule_id)
    if w is None or w.task is not None:
        if w is not None:
            await w.stop()
            await asyncio.sleep(0)
        async with SessionLocal() as db:
            rule = await db.get(AlertRule, rule_id)
        w = core.RuleWorker(rule)
        w.task = None            # 标记"无后台循环"，便于重复调用
        core.workers[rule_id] = w
    await w._eval_once()
    while not core.event_queue.empty():
        await core.consume_one(core.event_queue.get_nowait())


# ---------------- IT-告警实时（SSE） ----------------
@pytest.mark.asyncio
async def test_it_alert_realtime_broadcast_and_sse_auth():
    """实时告警链路（FR-C7）：
      ① 评估产生的事件经 broadcast 送达所有订阅者（SSE 的取数契约）；
      ② SSE 端点鉴权正确（无效 ticket → 401，白名单外需要凭证）。
    """
    await _ensure_host()
    rid = await _make_rule(name="d7-sse")
    try:
        q = core.subscribe()
        try:
            ring.append_point(HOST, "cpu.percent", datetime.utcnow(), 96.0)
            await _drive(rid)
            payload = await asyncio.wait_for(q.get(), timeout=5)
            assert payload["kind"] == "firing", payload
            assert payload["metric"] == "cpu.percent" and payload["trigger_value"] == 96.0
            assert payload["hash"] and payload["host_ident"] == HOST
        finally:
            core.unsubscribe(q)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            # 无效 ticket 建流 → 401（消费即焚，票不对就不给流）
            r = await ac.get("/api/ops/alerts/stream?ticket=deadbeef")
            assert r.status_code == 401 and "TICKET" in r.text
            # 无凭证（既无 ticket 也无 Bearer）→ 401
            assert (await ac.get("/api/ops/alerts/stream")).status_code == 401
            # 换票接口本身可用（白名单：否则前端永远建不了流）
            tok = await _login(ac)
            t = (await ac.get("/api/sse-ticket", headers={"Authorization": f"Bearer {tok}"})).json()["data"]["ticket"]
            assert len(t) == 64
    finally:
        await _cleanup([rid])


# ---------------- IT-重启回放 ----------------
@pytest.mark.asyncio
async def test_it_restart_replay_keeps_active_state():
    """触发中重建 worker（模拟重启）→ 回放后不误报恢复、指纹仍在活跃态（FR-C2 验收）。"""
    await _ensure_host()
    rid = await _make_rule(name="d7-replay")
    try:
        ring.append_point(HOST, "cpu.percent", datetime.utcnow(), 97.0)
        await _drive(rid)
        async with SessionLocal() as db:
            cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rid))).scalars().all()
        assert len(cur) == 1, "应有 1 条活跃事件"

        # 重建 worker（对账会检测到 worker 缺失）→ 走 replay 从 cur 表恢复
        w = core.workers.pop(rid, None)
        if w:
            await w.stop()
        await core.reconcile()
        w2 = core.workers.get(rid)
        assert w2 is not None
        st = w2.sm.fires.get(cur[0].hash)
        assert st is not None, "回放后活跃指纹应恢复到状态机"
        # 再评估一次：不应产生"恢复"事件（指标仍在窗内）
        await _drive(rid)
        async with SessionLocal() as db:
            his = (await db.execute(select(AlertHisEvent).where(
                AlertHisEvent.rule_id == rid, AlertHisEvent.is_recovered.is_(True)))).scalars().all()
        assert not his, "回放后不应误报恢复"
    finally:
        await _cleanup([rid])


# ---------------- IT-屏蔽 NotifyOnly ----------------
@pytest.mark.asyncio
async def test_it_mute_notify_only_persists_event_without_notify():
    """mute_type=1（只屏蔽通知）：事件照常入库、通知被跳过（FR-C4）。"""
    await _ensure_host()
    rid = await _make_rule(name="d7-mute1")
    sent = {"n": 0}

    async def fake_dispatch(db, ev, st, cfg):
        sent["n"] += 1
        return []

    import app.services.notify as notify_svc

    orig = notify_svc.dispatch
    notify_svc.dispatch = fake_dispatch
    try:
        async with SessionLocal() as db:
            now = datetime.utcnow()
            db.add(AlertMute(cause="d7-notify-only", mute_type=1, tag_filters=[{"k": "metric", "op": "==", "v": "cpu.percent"}],
                             severities=[], btime=now - timedelta(hours=1), etime=now + timedelta(hours=1)))
            await db.commit()
        ring.append_point(HOST, "cpu.percent", datetime.utcnow(), 98.0)
        await _drive(rid)
        async with SessionLocal() as db:
            cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rid))).scalars().all()
        assert len(cur) == 1, "NotifyOnly 下事件应照常入库"
        assert sent["n"] == 0, "NotifyOnly 下不应调用通知派发"
    finally:
        notify_svc.dispatch = orig
        async with SessionLocal() as db:
            await db.execute(delete(AlertMute).where(AlertMute.cause == "d7-notify-only"))
            await db.commit()
        await _cleanup([rid])


# ---------------- IT-异常检测对比 ----------------
def test_it_detection_algorithms_differ_on_shapes():
    """阶跃/尖峰/渐变三种形状下，静态阈值与 3σ 的差异可演示（FR-C3 验收）。"""
    base = [50.0 + (i % 3) * 0.2 for i in range(40)]
    step = base + [95.0] * 6                     # 阶跃：持续越阈
    spike = base + [95.0] + base[-6:]            # 尖峰：单点越阈
    gradual = base + [50 + i * 3.0 for i in range(12)]   # 渐变：缓慢漂移

    pts = lambda vals: [(None, v) for v in vals]  # noqa: E731

    # 静态阈值 >90：阶跃与尖峰都命中；渐变（最高 83）不命中——"缓慢劣化漏报"
    th_step = detect("threshold", ">90", pts(step))
    th_spike = detect("threshold", ">90", pts(spike))
    th_grad = detect("threshold", ">90", pts(gradual))
    assert len(th_step) == 6 and len(th_spike) == 1 and not th_grad, (len(th_step), len(th_spike), len(th_grad))

    # 3σ：需要 ≥5 点；渐变会被判为异常（漂移出历史分布），而尖峰同样命中
    sg_grad = detect("3sigma", ">0", pts(gradual))
    assert sg_grad, "3σ 应能识别渐变漂移（这正是它与静态阈值的差异点）"


# ---------------- IT-断网续传 ----------------
@pytest.mark.asyncio
async def test_it_offline_batch_resend_backfills_without_holes():
    """断网续传：把一段时间的点以 batch 形式补报，环形无空洞（FR-B1 验收）。"""
    await _ensure_host()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        await _login(ac)
        base = datetime.utcnow().replace(microsecond=0)
        batch = [{"ts": (base - timedelta(minutes=k)).isoformat() + "Z",
                  "metrics": [{"metric": "cpu.percent", "value": 40.0 + k}], "probes": []} for k in range(6, 0, -1)]
        r = await ac.post("/api/ops/agents/metrics", headers={"X-Ident": HOST, "X-Token": "d7-token"},
                          json={"batch": batch})
        assert r.status_code == 204
    pts = ring.query_points(HOST, "cpu.percent", base - timedelta(minutes=10), base + timedelta(minutes=1))
    assert len(pts) == 6, f"续传批应补齐 6 个点，实际 {len(pts)}"
    ts_sorted = sorted(t for t, _ in pts)
    gaps = [(b - a).total_seconds() for a, b in zip(ts_sorted, ts_sorted[1:])]
    assert all(g <= 61 for g in gaps), f"补报后不应有空洞：{gaps}"
    await _cleanup(()) 


# ---------------- IT-LLM 降级 ----------------
@pytest.mark.asyncio
async def test_it_llm_unavailable_does_not_break_alert_chain(monkeypatch):
    """断 LLM：告警链路照常，AI 功能明确报错（03 §3 降级总表）。"""
    await _ensure_host()

    async def broken(*a, **kw):
        from fastapi import HTTPException

        raise HTTPException(503, "LLM_UNAVAILABLE：连接被拒绝")

    # 注意：rca.py 用的是 `from app.services.llm_gateway import call_llm` 的本地引用，
    # 必须 patch **使用处**（rca_svc.call_llm）才生效——patch 定义处不会改变已绑定的名字。
    from app.services import rca as rca_svc_mod

    monkeypatch.setattr(rca_svc_mod, "call_llm", broken)

    # 1) 摘要降级：诊断入口明确报错，但不影响告警落库
    rid = await _make_rule(name="d7-llm-down")
    try:
        ring.append_point(HOST, "cpu.percent", datetime.utcnow(), 99.0)
        await _drive(rid)
        async with SessionLocal() as db:
            cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rid))).scalars().all()
        assert len(cur) == 1, "LLM 不可用不应影响告警落库"
        # 2) 诊断：应走到 failed stub 而不是抛出去（服务不挂）
        from app.models.event import Incident

        async with SessionLocal() as db:
            import uuid

            inc = Incident(title="d7-llm", severity=2, status="open", alert_hashes=[],
                           source_key=uuid.uuid4().hex, host_ident=HOST)
            db.add(inc)
            await db.commit()
            await db.refresh(inc)
            iid = inc.id
        from app.services import rca as rca_svc

        res = await rca_svc.diagnose(iid, max_steps=3)
        assert res["ok"] is True and res["status"] == "failed", res
        assert "可重新发起诊断" in res["findings_md"], "应落 failed stub 且标注可重试"
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM rca_findings WHERE incident_id = :i"), {"i": iid})
            await db.execute(text("DELETE FROM incidents WHERE id = :i"), {"i": iid})
            await db.commit()
    finally:
        await _cleanup([rid])


# ---------------- IT-Runbook 五段 + UNVALIDATED 拒入 ----------------
@pytest.mark.asyncio
async def test_it_runbook_five_sections_and_unvalidated_excluded():
    """变更单五段齐全；UNVALIDATED 建议不得进步骤（FR-E1）。"""
    from app.models.event import Incident, RCAFinding
    from app.services import runbook as rb_svc

    report = """---
agent_id: rca-1
purpose: d7
status: succeeded
tools_used: [query_metrics]
citations: [step_1]
self_assessed_strength: weak
confidence: LIKELY
impact:
  affected: []
  checked_healthy: []
  changed_other_reason: []
  not_checked: []
coverage:
  error_signal: checked
  recent_change: not_checked: 无部署数据
  runtime_state: checked
  history: checked
  blast_radius: checked
---

## 结论

磁盘将满。

## 证据

- disk 92%（step_1）

## 推理

日志未轮转。

## 已排除的假设

- 数据增长

## Recommended fix

- 清理 /var/log 下的过期日志并开启 logrotate 轮转
- 把整个数据库迁移到新集群（无验证依据的建议）
"""
    async with SessionLocal() as db:
        import uuid

        inc = Incident(title="d7-runbook", severity=2, status="open", alert_hashes=[],
                       source_key=uuid.uuid4().hex, host_ident=HOST)
        db.add(inc)
        await db.commit()
        await db.refresh(inc)
        db.add(RCAFinding(incident_id=inc.id, status="succeeded", findings_md=report, tools_used=["query_metrics"]))
        await db.commit()
        iid = inc.id
    r = await rb_svc.generate_runbook(iid)
    try:
        assert r["ok"], r
        assert len(r["validated"]) == 1 and len(r["unvalidated"]) == 1, r
        assert "迁移到新集群" in r["unvalidated"][0]["action"]
        async with SessionLocal() as db:
            rb = await db.get(Runbook, r["runbook_id"])
            assert rb.risk_assessment and rb.pre_checks and rb.steps and rb.rollback_plan and rb.verification, "五段必填"
            assert all("迁移到新集群" not in json.dumps(s, ensure_ascii=False) for s in rb.steps), "UNVALIDATED 不得进步骤"
            vers = (await db.execute(select(RunbookVersion).where(RunbookVersion.runbook_id == rb.id))).scalars().all()
            assert len(vers) == 1, "初版应有 1 个版本快照"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Runbook).where(Runbook.incident_id == iid))
            await db.execute(delete(RunbookVersion).where(RunbookVersion.runbook_id == r.get("runbook_id", -1)))
            await db.execute(delete(ActionAudit).where(ActionAudit.runbook_id == r.get("runbook_id", -1)))
            await db.execute(text("DELETE FROM rca_findings WHERE incident_id = :i"), {"i": iid})
            await db.execute(text("DELETE FROM incidents WHERE id = :i"), {"i": iid})
            await db.commit()


# ---------------- IT-权限矩阵 ----------------
@pytest.mark.asyncio
async def test_it_permission_matrix_three_roles():
    """三角色 × 代表路由：读/写/诊断权限与矩阵一致（FR-A1）。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        admin = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        demo = (await ac.post("/api/auth/demo", json={})).json()["data"]["access_token"]

        # admin：读 + 写 + 诊断 全通
        assert (await ac.get("/api/ops/hosts", headers={"Authorization": f"Bearer {admin}"})).status_code == 200
        # D161-7：先清同名残留（上一轮中途失败会留下 'd7-perm' → 本轮创建返回 409 → 假红）
        from sqlalchemy import delete as _del
        from app.db.session import SessionLocal as _SLS
        from app.models.alert import AlertRule as _AR
        async with _SLS() as _db:
            await _db.execute(_del(_AR).where(_AR.name == "d7-perm"))
            await _db.commit()
        assert (await ac.post("/api/ops/alert-rules", headers={"Authorization": f"Bearer {admin}"}, json={
            "name": "d7-perm", "scope": "metric", "metric": "x", "expr": ">1"})).status_code == 200

        # demo：只读放行，写/诊断 403
        assert (await ac.get("/api/ops/hosts", headers={"Authorization": f"Bearer {demo}"})).status_code == 200
        assert (await ac.post("/api/ops/alert-rules", headers={"Authorization": f"Bearer {demo}"}, json={
            "name": "x", "scope": "metric", "metric": "x", "expr": ">1"})).status_code == 403
        assert (await ac.post("/api/llm-configs", headers={"Authorization": f"Bearer {demo}"}, json={
            "name": "x", "api_key": "k", "model": "m"})).status_code == 403

        # 未认证：401
        assert (await ac.get("/api/ops/hosts")).status_code == 401

        # 清理 admin 建的规则
        rules = (await ac.get("/api/ops/alert-rules", headers={"Authorization": f"Bearer {admin}"})).json()["data"]["items"]
        for r in rules:
            if r["name"] == "d7-perm":
                await ac.delete(f"/api/ops/alert-rules/{r['id']}", headers={"Authorization": f"Bearer {admin}"})
                async with SessionLocal() as db:
                    await db.execute(delete(AlertRule).where(AlertRule.id == r["id"]))
                    await db.commit()


# ---------------- FR-C6 告警 AI 摘要 ----------------
@pytest.mark.asyncio
async def test_it_alert_ai_summary_written_once_and_degrades(monkeypatch):
    """首次落库触发摘要（fast 模型）写入 ai_summary；LLM 不可用时静默降级且不重复触发。"""
    await _ensure_host()
    rid = await _make_rule(name="d7-summary", enable_ai_summary=True)
    calls = {"n": 0}

    class _R:
        content = "demo-db-01 磁盘写入持续偏高，疑似日志未轮转"

    from app.core.config import get_settings as _gs

    monkeypatch.setattr(_gs(), "alert_ai_summary_enabled", True, raising=False)

    async def fake_call(db, purpose, messages, **kw):
        calls["n"] += 1
        assert purpose == "summary" and kw.get("model_role") == "fast", (purpose, kw)
        return _R()

    from app.services import rca as _rca  # noqa: F401  （确保模块已导入，patch 生效于 wasm 使用处）

    import app.engine.core as core_mod

    monkeypatch.setattr(core_mod, "call_llm", fake_call, raising=False)
    # core 里是函数内 import，故 patch 定义处 —— 但它是 `from ... import call_llm` 的函数内绑定，
    # 每次调用都会重新取属性，因此 patch 源码模块即可生效。
    import app.services.llm_gateway as gw

    monkeypatch.setattr(gw, "call_llm", fake_call)
    try:
        ring.append_point(HOST, "cpu.percent", datetime.utcnow(), 97.5)
        await _drive(rid)
        async with SessionLocal() as db:
            cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rid))).scalars().all()
        assert len(cur) == 1
        assert cur[0].ai_summary and "日志" in cur[0].ai_summary, cur[0].ai_summary
        assert calls["n"] == 1, "摘要应只在首次落库触发一次"

        # 再来一轮（同一指纹，非首次）→ 不应再调摘要
        await _drive(rid)
        assert calls["n"] == 1, "非首次落库不应重复生成摘要"

        # LLM 不可用 → 静默降级，事件照常
        async def boom(*a, **kw):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(gw, "call_llm", boom)
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM alert_cur_events WHERE rule_id = :r"), {"r": rid})
            await db.commit()
        await _clear_ring_quiet()
        # 删 DB 行不会清状态机：需一并让指纹"从活跃态消失"，否则新点只会被当成更新
        w = core.workers.get(rid)
        if w:
            w.sm.fires.clear()
            w.sm.pendings.clear()
        ring.append_point(HOST, "cpu.percent", datetime.utcnow(), 99.0)
        await _drive(rid)
        async with SessionLocal() as db:
            cur2 = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rid))).scalars().all()
        assert len(cur2) == 1, "LLM 不可用时告警仍应落库"
        assert not cur2[0].ai_summary, "降级时不写摘要"
    finally:
        await _cleanup([rid])


async def _clear_ring_quiet():
    """清空该主机的环形数据（模拟窗口推移，避免上一轮超阈点仍在窗内导致不产生新事件）。"""
    from app.store import ring as _ring

    _ring._rings.pop((HOST, "cpu.percent"), None)


# ---------------- 回归：incidents 列表的 status/severity 过滤 ----------------
@pytest.mark.asyncio
async def test_it_incident_list_filters_work():
    """incidents 列表按 status/severity 过滤必须可用。

    回归背景（D7 实测发现）：`incidents.status` 是 ENUM 且曾是复合索引 `(status, severity)` 的首列，
    SeekDB 对"ENUM 作索引首列 + 等值过滤"直接抛 `(1210, 'Invalid argument')` → 接口 500。
    此前测试从未覆盖过该过滤条件，所以一直没被发现；索引已改为 `(severity, created_at)`。
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        tok = await _login(ac)
        H = {"Authorization": f"Bearer {tok}"}
        for params in ({"status": "open"}, {"status": "resolved"}, {"severity": 1},
                       {"status": "open", "severity": 2}, {}):
            r = await ac.get("/api/ops/incidents", headers=H, params=params)
            assert r.status_code == 200, f"{params} → {r.status_code}: {r.text[:200]}"
            assert "items" in r.json()["data"], r.text
        # 表格里也必须真的按 status 过滤（不只是不报错）
        opened = (await ac.get("/api/ops/incidents", headers=H, params={"status": "resolved", "page_size": 100})).json()["data"]["items"]
        assert all(x["status"] == "resolved" for x in opened), "返回项与 status 过滤不符"
