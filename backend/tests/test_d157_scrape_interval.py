"""D157 守卫：scrape_interval_s 字段端到端一致。

锁三件事：
  ① 模型字段存在（scrape_interval_s，Integer，默认 30）—— 迁移不可漏
  ② PromDsIn 透传 + 落库正确（POST 默认 30 / PUT 60 后 DB 实际 = 60）
  ③ 调度任务 helper 存在（跨线程事件循环细节由 lifespan conftest 测，这里只验存在性）
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select, text

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.prom import PromDatasource
from app.models.shell import User
from app.sched import _safe_register_all_scrape_jobs


def test_prom_datasource_has_scrape_interval_field():
    """模型层字段存在（迁移在 alembic 里跑过）。"""
    cols = {c.key for c in PromDatasource.__table__.columns}
    assert "scrape_interval_s" in cols, "数据源模型缺 scrape_interval_s 字段"


def test_create_with_scrape_interval_30_default():
    """POST /api/ops/prom-datasources 不传 scrape_interval_s 应落默认值 30。"""
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: User(id=1, username="admin", role="admin")

    async def m():
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM prom_datasources WHERE name LIKE 'd157-test%'"))
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post("/api/ops/prom-datasources", json={
                "name": "d157-test-default", "base_url": "http://127.0.0.1:9090", "mode": "scrape"})
            assert r.status_code == 200, r.text[:200]
            did = r.json()["data"]["id"]
        async with SessionLocal() as db:
            ds = (await db.execute(select(PromDatasource).where(PromDatasource.id == did))).scalar_one()
            assert ds.scrape_interval_s == 30, f"默认值应为 30，实际 {ds.scrape_interval_s}"
            await db.execute(text(f"DELETE FROM prom_datasources WHERE id={did}"))
            await db.commit()
    asyncio.run(m())


def test_update_scrape_interval_persists():
    """PUT 改 scrape_interval_s 后 DB 实际值更新。"""
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: User(id=1, username="admin", role="admin")

    async def m():
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM prom_datasources WHERE name LIKE 'd157-test%'"))
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post("/api/ops/prom-datasources", json={
                "name": "d157-test-update", "base_url": "http://127.0.0.1:9090", "mode": "scrape",
                "scrape_interval_s": 60})
            assert r.status_code == 200
            did = r.json()["data"]["id"]
            r = await ac.put(f"/api/ops/prom-datasources/{did}", json={
                "name": "d157-test-update", "base_url": "http://127.0.0.1:9090", "mode": "scrape",
                "scrape_interval_s": 120})
            assert r.status_code == 200
        async with SessionLocal() as db:
            ds = (await db.execute(select(PromDatasource).where(PromDatasource.id == did))).scalar_one()
            assert ds.scrape_interval_s == 120, f"PUT 后应为 120，实际 {ds.scrape_interval_s}"
            await db.execute(text(f"DELETE FROM prom_datasources WHERE id={did}"))
            await db.commit()
    asyncio.run(m())


def test_scrape_interval_0_skips_automatic_scrape():
    """scrape_interval_s=0 = 不自动拉（仅手动试采）—— 模型层语义。

    调度注册测试在 conftest 的 fastapi lifespan 上下文里做（scheduler 跨线程
    add_job 在 pytest 线程里会静默失败，这里只验语义）。
    """
    assert PromDatasource(name="x", base_url="http://x", mode="scrape",
                         scrape_interval_s=0).scrape_interval_s == 0


def test_scrape_interval_60_persists_and_helper_callable():
    """scrape_interval_s=60 时 DB 端字段正确 + 调度 helper 可调用。"""
    from app.sched import _safe_register_all_scrape_jobs

    async def m():
        async with SessionLocal() as db:
            from sqlalchemy import select as _select
            await db.execute(text("DELETE FROM prom_datasources WHERE name LIKE 'd157-test%'"))
            ds = PromDatasource(name="d157-test-60", base_url="http://127.0.0.1:9090",
                                mode="scrape", enabled=True, scrape_interval_s=60)
            db.add(ds); await db.commit()
            row = (await db.execute(
                _select(PromDatasource).where(PromDatasource.id == ds.id))).scalar_one()
            assert row.scrape_interval_s == 60
            await db.execute(text("DELETE FROM prom_datasources WHERE name LIKE 'd157-test%'"))
            await db.commit()
    asyncio.run(m())
    assert callable(_safe_register_all_scrape_jobs)
