"""D0 冒烟测试：健康端点 + hosts 表 CRUD（经真实 SeekDB）。

依据：06-测试计划 §2（M1 前置冒烟）；pytest-asyncio。
运行：cd backend && ../.venv/bin/pytest tests -q
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.main import app
from app.models.host import Host


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200 and r.json()["data"]["status"] == "up"


@pytest.mark.asyncio
async def test_host_crud_on_seekdb():
    """直连真实 SeekDB：插入→查询→清理（冒烟：ORM 建模与方言正确性）。"""
    async with SessionLocal() as s:
        s.add(Host(ident="smoke-host-001", name="冒烟机", ip="127.0.0.1", status="up"))
        await s.commit()
        rows = (await s.execute(select(Host).where(Host.ident == "smoke-host-001"))).scalars().all()
        assert len(rows) == 1 and rows[0].name == "冒烟机"  # utf8mb4 中文往返
        await s.execute(delete(Host).where(Host.ident == "smoke-host-001"))
        await s.commit()
