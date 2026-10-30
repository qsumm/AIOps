"""D2 端到端：Agent 上报 → 对账 → 状态机 → 静默 → 通知 → 落库（06 §3 集成用例雏形）。

不依赖后台定时的 reconcile_loop/consume_loop，而是显式驱动同一批函数：
reconcile() → RuleWorker._eval_once() → consume_one()，链路与生产一致但确定性。
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.engine import core
from app.main import app
from app.models.alert import AlertCurEvent, AlertHisEvent, AlertMute, AlertRule
from app.models.host import Host
from app.store import ring

H = {"Authorization": ""}


async def _login(ac) -> str:
    r = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["data"]["access_token"]


async def _ingest(ac, ident: str, atoken: str, value: float) -> None:
    """上报一个 cpu.percent 点（时间戳=now，确保落在规则 window 内）。"""
    await ac.post("/api/ops/agents/metrics", headers={"X-Ident": ident, "X-Token": atoken}, json={
        "ts": datetime.utcnow().isoformat() + "Z",
        "metrics": [{"metric": "cpu.percent", "value": value}], "probes": []})


def _slide_window(ident: str, metric: str = "cpu.percent") -> None:
    """清空环形窗口，模拟"时间推移后旧点滑出 window"（threshold 按窗口内所有超阈点判定）。"""
    ring._rings.pop((ident, metric), None)


async def _drain_queue() -> None:
    while not core.event_queue.empty():
        ev = core.event_queue.get_nowait()
        await core.consume_one(ev)


async def _tick(ident: str, rule_id: int | None = None) -> None:
    """只驱动被测规则（直接构造 worker），再消费事件。

    刻意**不走 core.reconcile()**：库里可能已有演示/调试规则，全局对账会让它们也参与评估，
    测试就依赖数据库现状（D7 实测：宿主库 9 条启用规则导致偶发失败）。
    """
    if rule_id is not None and rule_id not in core.workers:
        async with SessionLocal() as db:
            rule = await db.get(AlertRule, rule_id)
        w = core.RuleWorker(rule)
        w.task = None            # 无后台循环，纯手动驱动
        core.workers[rule_id] = w
    for rid, w in list(core.workers.items()):
        if rule_id is not None and rid != rule_id:
            continue
        if w.task is not None and not w.task.done():
            continue
        await w._eval_once()
    await _drain_queue()


@pytest.mark.asyncio
async def test_alert_pipeline_end_to_end():
    ident = rule_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            H["Authorization"] = f"Bearer {await _login(ac)}"
            # 1) 注册 Agent
            mint = await ac.get("/api/ops/dev/mint-install-token", headers=H)
            install_token = mint.json()["data"]["install_token"]
            reg = await ac.post("/api/ops/agents/register", headers=H, json={
                "fingerprint": "e2e-fp-001", "hostname": "e2e-1", "ip": "127.0.0.1",
                "os": "test", "install_token": install_token})
            ident, atoken = reg.json()["data"]["ident"], reg.json()["data"]["token"]

            # 2) 上报超阈指标：cpu.percent = 95（规则阈值 >90），时间戳=now（必须落在 window 内）
            now = datetime.utcnow()
            await ac.post("/api/ops/agents/metrics", headers={"X-Ident": ident, "X-Token": atoken}, json={
                "ts": now.isoformat() + "Z",
                "metrics": [{"metric": "cpu.percent", "value": 95.0}], "probes": []})

            # 3) 建规则（阈值算法 + 立刻生效 + 无 for_duration + 单渠道）
            rule = await ac.post("/api/ops/alert-rules", headers=H, json={
                "name": "e2e-cpu-高负载", "scope": "metric", "metric": "cpu.percent",
                "expr": ">90", "severity": 2, "eval_interval_s": 5, "window_s": 120,
                "algorithm": "threshold", "recover_duration_s": 0})
            rule_id = rule.json()["data"]["id"]

            # 4) 触发
            await _tick(ident, rule_id)
            alerts = await ac.get("/api/ops/alerts", headers=H)
            items = alerts.json()["data"]["items"]
            firing = [a for a in items if a["rule_id"] == rule_id and a["metric"] == "cpu.percent"]
            assert firing, f"应有 firing 事件，实际: {items}"
            assert firing[0]["trigger_value"] == 95.0
            assert firing[0]["host_ident"] == ident
            assert firing[0]["status"] == "firing"
            ev_hash = firing[0]["hash"]

            # 5) 静默链路：MutedAll(0) —— 事件被丢弃（不落 cur/his）
            #    先让窗口推移 + 上报低值，令状态机恢复并清掉指纹，再重新越阈才走到 mute 分支
            _slide_window(ident)
            await _ingest(ac, ident, atoken, 12.0)
            await _tick(ident, rule_id)
            after_recover = await ac.get("/api/ops/alerts", headers=H)
            assert not [a for a in after_recover.json()["data"]["items"] if a["rule_id"] == rule_id], "低值后应恢复"
            now2 = datetime.utcnow()
            m = await ac.post("/api/ops/alert-mutes", headers=H, json={
                "cause": "e2e 维护窗口",
                "tag_filters": [{"k": "metric", "op": "==", "v": "cpu.percent"}],
                "severities": [],
                "btime": (now2 - timedelta(hours=1)).isoformat(),
                "etime": (now2 + timedelta(hours=1)).isoformat(),
                "mute_type": 0})
            assert m.status_code == 200, m.text
            async with SessionLocal() as db:
                firing_before = len((await db.execute(select(AlertHisEvent).where(
                    AlertHisEvent.rule_id == rule_id, AlertHisEvent.is_recovered.is_(False)))).scalars().all())
            await _ingest(ac, ident, atoken, 95.0)
            await _tick(ident, rule_id)
            again = await ac.get("/api/ops/alerts", headers=H)
            assert not [a for a in again.json()["data"]["items"] if a["rule_id"] == rule_id], "MutedAll 下不应产生 cur 事件"
            async with SessionLocal() as db:
                firing_after = len((await db.execute(select(AlertHisEvent).where(
                    AlertHisEvent.rule_id == rule_id, AlertHisEvent.is_recovered.is_(False)))).scalars().all())
            assert firing_after == firing_before, "MutedAll 下不应新增 firing 历史"

            # 6) 解除静默 → 重新触发 → 恢复 → his 记录恢复态，cur 清空
            mid = m.json()["data"]["id"]
            await ac.delete(f"/api/ops/alert-mutes/{mid}", headers=H)
            await _tick(ident, rule_id)  # 该指纹已被丢弃，重新评估应新建事件
            again2 = await ac.get("/api/ops/alerts", headers=H)
            refire = [a for a in again2.json()["data"]["items"] if a["rule_id"] == rule_id]
            assert refire, "解除静默后应重新触发"
            _slide_window(ident)
            await _ingest(ac, ident, atoken, 12.0)
            await _tick(ident, rule_id)
            async with SessionLocal() as db:
                his = (await db.execute(select(AlertHisEvent).where(AlertHisEvent.rule_id == rule_id))).scalars().all()
            assert any(h.is_recovered for h in his), "恢复事件应写入 his 且 is_recovered=True"
            async with SessionLocal() as db:
                cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.rule_id == rule_id))).scalars().all()
            assert not cur, "恢复后 cur 表应清空该指纹"
    finally:
        # 清理：规则/主机/内存态（保持测试可重复）
        async with SessionLocal() as db:
            if rule_id:
                await db.execute(delete(AlertRule).where(AlertRule.id == rule_id))
            if ident:
                await db.execute(delete(AlertCurEvent).where(AlertCurEvent.host_ident == ident))
                await db.execute(delete(AlertHisEvent).where(AlertHisEvent.host_ident == ident))
                await db.execute(delete(Host).where(Host.ident == ident))
            await db.execute(delete(AlertMute).where(AlertMute.cause == "e2e 维护窗口"))
            await db.commit()
        if rule_id:
            w = core.workers.pop(rule_id, None)
            if w:
                await w.stop()
        if ident:
            ring._rings.pop((ident, "cpu.percent"), None)
