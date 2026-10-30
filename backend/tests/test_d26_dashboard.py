"""D26 大盘总览增强的回归测试。

覆盖：
  1. events_24h / events_by_hour 必须随可见域过滤（demo 前缀隔离）——修复前 demo 能看到全平台事件数
  2. 活跃告警计数不再被 limit(20) 截断（此前 >20 条时卡片计数偏小）
  3. 新增字段存在且类型正确：down_hosts / top_cpu / incidents_open / mttr_minutes / events_by_hour / range
  4. range=6h / 24h（metric_agg 路径）可用；非法 range 回落 1h
  5. MTTR：唯一已恢复样本 → 平均恢复时长 ≈ 样本时长
"""
import hashlib

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.alert import AlertCurEvent, AlertHisEvent
from app.models.host import Host
from app.models.shell import User

VIS = "d26-visible"
HID = "d26-hidden"


def _ident(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:32]


async def _client_as(username: str = "admin"):
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == username))).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: u
    return app


@pytest.mark.asyncio
async def test_events24h_respects_visibility(monkeypatch):
    """demo + 前缀隔离时，24h 事件计数只能统计可见主机（D26 前此查询漏套过滤）。"""
    from app.core import config as cfg

    s = cfg.get_settings()
    monkeypatch.setattr(s, "demo_data_prefixes", VIS, raising=False)
    async with SessionLocal() as db:
        await db.execute(delete(AlertHisEvent).where(AlertHisEvent.host_ident.in_([_ident(VIS), _ident(HID)])))
        await db.execute(delete(Host).where(Host.ident.in_([_ident(VIS), _ident(HID)])))
        db.add(Host(ident=_ident(VIS), name=VIS, status="up"))
        db.add(Host(ident=_ident(HID), name=HID, status="up"))
        for host in (VIS, HID):
            db.add(AlertHisEvent(hash=hashlib.sha256(f"d26-{host}".encode()).hexdigest()[:32],
                                 rule_id=0, rule_name="d26-t", host_ident=_ident(host),
                                 metric="cpu.percent", severity=3, trigger_value=1.0,
                                 trigger_time=__import__('datetime').datetime.utcnow()))
        await db.commit()
    app = await _client_as()
    try:
        async with SessionLocal() as db:
            demo = (await db.execute(select(User).where(User.role == "demo"))).scalars().first()
        if demo is None:
            pytest.skip("无 demo 用户")
        app.dependency_overrides[get_current_user] = lambda: demo
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/api/ops/dashboard/overview")
            assert r.status_code == 200
            d = r.json()["data"]
            assert d["events_24h"] == 1, \
                f"demo 应只看到可见主机的 1 条事件，实际 {d['events_24h']}（可见域过滤失效？）"
            assert sum(d["events_by_hour"]) == d["events_24h"], "直方图与 24h 计数应一致（同源派生）"
        # admin 视角不过滤，应看到 2 条
        async with SessionLocal() as db:
            admin = (await db.execute(select(User).where(User.role == "admin"))).scalars().first()
        app.dependency_overrides[get_current_user] = lambda: admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            d = (await ac.get("/api/ops/dashboard/overview")).json()["data"]
            assert d["events_24h"] >= 2, f"admin 应看到 2 条测试事件，实际 {d['events_24h']}"
    finally:
        app.dependency_overrides.clear()
        async with SessionLocal() as db:
            await db.execute(delete(AlertHisEvent).where(AlertHisEvent.host_ident.in_([_ident(VIS), _ident(HID)])))
            await db.execute(delete(Host).where(Host.ident.in_([_ident(VIS), _ident(HID)])))
            await db.commit()



@pytest.mark.asyncio
async def test_alert_count_not_capped_at_20():
    """活跃告警 >20 条时计数必须给全量（此前 limit(20) 截断后再分桶）。"""
    app = await _client_as()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            before = (await ac.get("/api/ops/dashboard/overview")).json()["data"]["alerts"]["total"]
        async with SessionLocal() as db:
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.host_ident == _ident(VIS)))
            await db.execute(delete(Host).where(Host.ident == _ident(VIS)))
            db.add(Host(ident=_ident(VIS), name=VIS, status="up"))
            for i in range(25):
                h = hashlib.sha256(f"d26-cap-{i}".encode()).hexdigest()[:32]
                db.add(AlertCurEvent(hash=h, rule_id=0, rule_name="d26-cap", host_ident=_ident(VIS),
                                     metric="cpu.percent", severity=2, trigger_value=1.0))
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            d = (await ac.get("/api/ops/dashboard/overview")).json()["data"]
        assert d["alerts"]["total"] == before + 25, \
            f"插入 25 条后应计满 {before + 25}，实际 {d['alerts']['total']}（limit(20) 截断回归？）"
        assert d["alerts"]["by_severity"].get("2", 0) >= 25
        assert len(d["active_alerts"]) == 20, "明细列表保持 20 条截断"
    finally:
        app.dependency_overrides.clear()
        async with SessionLocal() as db:
            await db.execute(delete(AlertCurEvent).where(AlertCurEvent.host_ident == _ident(VIS)))
            await db.execute(delete(Host).where(Host.ident == _ident(VIS)))
            await db.commit()


@pytest.mark.asyncio
async def test_overview_new_fields_and_ranges():
    """新字段存在且合法；6h/24h（metric_agg 路径）可用；非法 range 回落 1h。"""
    app = await _client_as()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            d = (await ac.get("/api/ops/dashboard/overview")).json()["data"]
            assert isinstance(d["hosts"].get("down_hosts"), list)
            assert isinstance(d["watch_hosts"], list)   # D41：top_cpu 已重构为「接近阈值的主机」
            assert isinstance(d["incidents_open"], int)
            assert d["mttr_minutes"] is None or isinstance(d["mttr_minutes"], (int, float))
            assert isinstance(d["events_by_hour"], list) and len(d["events_by_hour"]) == 24
            assert d["range"] == "1h"

            d6 = (await ac.get("/api/ops/dashboard/overview?range=6h")).json()["data"]
            assert d6["range"] == "6h"
            d24 = (await ac.get("/api/ops/dashboard/overview?range=24h")).json()["data"]
            assert d24["range"] == "24h"
            dbad = (await ac.get("/api/ops/dashboard/overview?range=bogus")).json()["data"]
            assert dbad["range"] == "1h", "非法 range 应回落 1h"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_d45_overview_field_types_stable_across_ranges():
    """D45：切换 range 后 overview 各字段**类型必须稳定**。

    缺陷（用户实测：切到 6 小时 → 大盘半屏卡在"加载中"）：
    6h/24h 分支里局部变量重用了 `buckets`（上面直方图已用该名），把 events_by_hour
    从「24 长度列表」覆盖成「dict」→ 前端 `.map()` 抛 TypeError → 后续渲染全部中断
    （活跃告警停在"加载中"、接近阈值与事件分布空白）。

    本测试对三个 range 逐一断言字段类型，防同类覆盖再发生。
    """
    from app.core.security import get_current_user
    from app.main import create_app
    from app.models.shell import User
    from sqlalchemy import select as _select

    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(_select(User).where(User.username == "admin"))).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: u
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            for rng in ("1h", "6h", "24h"):
                d = (await ac.get(f"/api/ops/dashboard/overview?range={rng}")).json()["data"]
                assert isinstance(d["events_by_hour"], list), \
                    f"range={rng}: events_by_hour 必须是列表（实际 {type(d['events_by_hour']).__name__}）"
                assert len(d["events_by_hour"]) == 24, \
                    f"range={rng}: 逐小时直方图必须恒为 24 项（实际 {len(d['events_by_hour'])}）"
                assert isinstance(d["series"], list) and isinstance(d["watch_hosts"], list)
                assert isinstance(d["active_alerts"], list) and isinstance(d["hosts"], dict)
                assert d["range"] == rng
    finally:
        app.dependency_overrides.clear()
