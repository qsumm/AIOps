"""D61 规则生命周期回归（用户报：规则停用后不能再启用）。

锁三件事：
  ① toggle 双向：停用 ↔ 启用（原先停用即"永 bury"，无任何恢复入口）
  ② purge 硬删：废弃规则可真删（原先只有软删，停用行永远占着列表——
     用户截图里 smoke/回归测试留下的十来行已停用垃圾规则）
  ③ 默认软删语义不回归（DELETE 不带 purge 仍是停用，引擎对账口径不变）
"""
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.alert import AlertRule
from app.models.shell import User


async def _client():
    app = create_app()
    async with SessionLocal() as db:
        admin = await db.get(User, 1)
    app.dependency_overrides[get_current_user] = lambda: admin
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _cleanup(name: str):
    async with SessionLocal() as db:
        await db.execute(delete(AlertRule).where(AlertRule.name == name))
        await db.commit()


@pytest.mark.asyncio
async def test_rule_toggle_and_purge_lifecycle():
    app, _ = await _client()
    name = "d61-生命周期"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        await _cleanup(name)
        r = await ac.post("/api/ops/alert-rules", json={"name": name, "metric": "cpu.percent", "expr": ">90"})
        assert r.status_code == 200, r.text[:160]
        rid = r.json()["data"]["id"]
        try:
            # 停用
            r = await ac.post(f"/api/ops/alert-rules/{rid}/toggle")
            assert r.status_code == 200 and r.json()["data"]["disabled"] is True, r.text[:160]
            async with SessionLocal() as db:
                row = await db.get(AlertRule, rid)
                assert row.disabled is True
            # 再启用（用户报的核心：原先无路可走）
            r = await ac.post(f"/api/ops/alert-rules/{rid}/toggle")
            assert r.status_code == 200 and r.json()["data"]["disabled"] is False, r.text[:160]
            async with SessionLocal() as db:
                row = await db.get(AlertRule, rid)
                assert row.disabled is False
            # 不存在的规则 → 404
            r = await ac.post("/api/ops/alert-rules/99999999/toggle")
            assert r.status_code == 404
            # 默认 DELETE = 软删（停用）
            r = await ac.delete(f"/api/ops/alert-rules/{rid}")
            assert r.status_code == 200
            async with SessionLocal() as db:
                row = await db.get(AlertRule, rid)
                assert row is not None and row.disabled is True, "不带 purge 的 DELETE 必须仍是软删"
            # purge=true = 硬删
            r = await ac.delete(f"/api/ops/alert-rules/{rid}?purge=true")
            assert r.status_code == 200 and r.json()["data"]["purged"] is True, r.text[:160]
            async with SessionLocal() as db:
                assert await db.get(AlertRule, rid) is None, "purge 后行必须真删"
        finally:
            await _cleanup(name)
    app.dependency_overrides.clear()
