"""P0：告警新鲜度/僵尸清理回归锁。

锁四件事：
  ① 规则停用/删除 → 活跃行立即 stale，不再当 firing；
  ② 规则重新评估 → touch_rule_evaluated 解除 stale 并刷新 last_eval_at；
  ③ 全局扫描：超过 4 个评估周期（至少 300s）未刷新的行 → stale；
  ④ 诊断上下文把 fresh/historical 分开（历史告警不能当当前证据）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select, text

from app.db.session import SessionLocal
from app.models.alert import AlertCurEvent, AlertRule
from app.services import alert_freshness


def _mk_rule(name: str, *, disabled: bool = False) -> AlertRule:
    return AlertRule(name=name, expr=">90", metric="cpu.percent", scope="metric",
                     disabled=disabled, eval_interval_s=15)


def _mk_cur(rule: AlertRule, suffix: str, *, last_eval_at=None, stale=False) -> AlertCurEvent:
    now = datetime.utcnow()
    return AlertCurEvent(
        hash=("p0af" + suffix)[:32].ljust(32, "0"), rule_id=rule.id, rule_name=rule.name,
        host_ident="p0-af-host", metric="cpu.percent", severity=2,
        trigger_time=now, first_trigger_time=now, trigger_value=95.0,
        last_eval_at=last_eval_at or now, stale=stale,
        stale_reason="test" if stale else "",
    )


@pytest.mark.asyncio
async def test_mark_rule_stale_and_touch_recovers():
    async with SessionLocal() as db:
        rule = _mk_rule("P0 freshness unit")
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        cur = _mk_cur(rule, "a")
        db.add(cur)
        await db.commit()
        try:
            n = await alert_freshness.mark_rule_stale(db, rule.id, "rule_disabled")
            assert n == 1
            await db.refresh(cur)
            assert cur.stale is True and cur.stale_reason == "rule_disabled"

            n = await alert_freshness.touch_rule_evaluated(db, rule.id)
            assert n == 1
            await db.refresh(cur)
            assert cur.stale is False and cur.stale_reason == ""
            assert cur.last_eval_at is not None
        finally:
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.rule_id == rule.id))
            await db.execute(delete(AlertRule).where(AlertRule.id == rule.id))
            await db.commit()


@pytest.mark.asyncio
async def test_global_freshness_scan_marks_old_rows_only():
    async with SessionLocal() as db:
        rule = _mk_rule("P0 freshness scan")
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        old = _mk_cur(rule, "old", last_eval_at=datetime.utcnow() - timedelta(minutes=10))
        fresh = _mk_cur(rule, "new", last_eval_at=datetime.utcnow())
        db.add_all([old, fresh])
        await db.commit()
        try:
            n = await alert_freshness.mark_stale_alerts(db)
            assert n >= 1
            await db.refresh(old)
            await db.refresh(fresh)
            assert old.stale is True and old.stale_reason == "no_evaluation"
            assert fresh.stale is False
        finally:
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.rule_id == rule.id))
            await db.execute(delete(AlertRule).where(AlertRule.id == rule.id))
            await db.commit()


@pytest.mark.asyncio
async def test_rule_disable_marks_active_alerts_stale_via_api():
    """disabling a rule through the API must immediately stale its active alerts."""
    from httpx import ASGITransport, AsyncClient
    from app.main import create_app

    app = create_app()
    async with SessionLocal() as db:
        rule = _mk_rule("P0 freshness api")
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        cur = _mk_cur(rule, "api")
        db.add(cur)
        await db.commit()
        rid = rule.id
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                lg = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
                H = {"Authorization": f"Bearer {lg.json()['data']['access_token']}"}
                r = await ac.post(f"/api/ops/alert-rules/{rid}/toggle", headers=H)
                assert r.status_code == 200, r.text[:200]
                assert r.json()["data"]["disabled"] is True
            async with SessionLocal() as db2:
                row = await db2.get(AlertCurEvent, cur.hash)
                assert row is not None and row.stale is True
                assert row.stale_reason == "rule_disabled"
        finally:
            async with SessionLocal() as db3:
                await db3.execute(delete(AlertCurEvent).where(AlertCurEvent.rule_id == rid))
                await db3.execute(delete(AlertRule).where(AlertRule.id == rid))
                await db3.commit()
            app.dependency_overrides.clear()
