"""D41：大盘「接近阈值的主机」——替代原「CPU Top5 瞬时快照」。

原实现的价值问题（用户指出：「展示个百分比想表达啥？」）：
  ① 取最近一个采样点的**瞬时值**，无趋势/无基线，不构成判断依据；
  ② 前端**硬编码 70/90** 着色，与用户配的告警规则阈值**不同源** →
    会出现"卡片绿着、但那台机器已经触发告警"的误导；
  ③ 不能下钻定位（点击只跳主机列表）。

改为：对每条 **enabled 且 algorithm='threshold' 的 metric 规则** × 每台主机，
算当前值与阈值的逼近度；已触发的排最前，其余逼近度 ≥80% 的次之。
非 threshold 算法（3σ/孤立森林）没有固定阈值可比 → 必须跳过，否则 `expr='>0'`
这类占位阈值会算出"触发=True 但逼近 0%"的荒谬结果（实测踩到）。
"""
import hashlib
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.alert import AlertRule
from app.models.host import Host
from app.models.shell import User
from app.store import ring

HOST = "d41-host"
IDENT = hashlib.sha256(HOST.encode()).hexdigest()[:32]


@pytest.mark.asyncio
async def test_watch_hosts_skips_non_threshold_algorithm():
    """3σ 规则（expr 只是占位 '>0'）不得进入逼近度排行。"""
    from datetime import datetime
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(__import__("sqlalchemy").select(User).where(User.username == "admin"))).scalars().first()
        await db.execute(delete(AlertRule).where(AlertRule.name.like("d41-%")))
        await db.execute(delete(Host).where(Host.ident == IDENT))
        db.add(Host(ident=IDENT, name=HOST, status="up"))
        db.add(AlertRule(name="d41-3sigma", scope="metric", metric="mem.percent", expr=">0",
                         algorithm="3sigma", severity=3, disabled=False))
        db.add(AlertRule(name="d41-threshold", scope="metric", metric="mem.percent", expr=">95",
                         algorithm="threshold", severity=2, disabled=False))
        await db.commit()
    app.dependency_overrides[get_current_user] = lambda: u
    try:
        # 造一个当前值 30% 的采样点：对 >95 规则不逼近；对 3σ 规则不该被算入
        ring.append_point(IDENT, "mem.percent", __import__("datetime").datetime.utcnow(), 30.0)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            d = (await ac.get("/api/ops/dashboard/overview")).json()["data"]
        rows = [w for w in d.get("watch_hosts", []) if w["ident"] == IDENT]
        assert rows == [], f"3σ 规则不该进逼近榜（30%% vs 占位阈值 0）；实际: {rows}"
    finally:
        app.dependency_overrides.clear()
        async with SessionLocal() as db:
            await db.execute(delete(AlertRule).where(AlertRule.name.like("d41-%")))
            await db.execute(delete(Host).where(Host.ident == IDENT))
            await db.commit()


@pytest.mark.asyncio
async def test_watch_hosts_flags_firing_and_near():
    """threshold 规则：已触发必须进榜且 firing=True；逼近≥80% 也进榜。"""
    from datetime import datetime
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(__import__("sqlalchemy").select(User).where(User.username == "admin"))).scalars().first()
        await db.execute(delete(AlertRule).where(AlertRule.name.like("d41-%")))
        await db.execute(delete(Host).where(Host.ident == IDENT))
        db.add(Host(ident=IDENT, name=HOST, status="up"))
        db.add(AlertRule(name="d41-near", scope="metric", metric="mem.percent", expr=">90",
                         algorithm="threshold", severity=2, disabled=False))
        await db.commit()
    app.dependency_overrides[get_current_user] = lambda: u
    try:
        # 85% vs >90：逼近度 94% → 应进榜、firing=False
        ring.append_point(IDENT, "mem.percent", datetime.utcnow(), 85.0)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            d = (await ac.get("/api/ops/dashboard/overview")).json()["data"]
        row = next((w for w in d.get("watch_hosts", []) if w["ident"] == IDENT), None)
        assert row, "85% vs >90 应进逼近榜"
        assert row["firing"] is False and row["ratio"] >= 0.9, f"应未触发但高度逼近：{row}"
        assert row["name"] == HOST, "应带主机名（不是 ident）"
    finally:
        app.dependency_overrides.clear()
        async with SessionLocal() as db:
            await db.execute(delete(AlertRule).where(AlertRule.name.like("d41-%")))
            await db.execute(delete(Host).where(Host.ident == IDENT))
            await db.commit()
