"""D123 告警中心批量删除 + 关键词筛选回归锁。

bulk delete：彻底删除 cur 行（不写 his）、500 条上限、审计留 hash 清单；
keyword 筛选：rule_name / metric LIKE 命中。
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models.alert import AlertCurEvent


async def _mk_alert(rule: str, metric: str, ident: str = "d123-host") -> str:
    async with SessionLocal() as db:
        cur = AlertCurEvent(hash=f"d123-{rule}-{metric}"[:32], rule_id=1, rule_name=rule,
                            host_ident=ident, metric=metric, severity=2,
                            trigger_time=__import__("datetime").datetime.utcnow(), trigger_value=1.0)
        db.add(cur)
        await db.commit()
        return cur.hash


async def _rm(hash_: str):
    async with SessionLocal() as db:
        cur = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash == hash_))).scalar_one_or_none()
        if cur is not None:
            await db.delete(cur)
            await db.commit()


@pytest.mark.asyncio
async def test_bulk_delete_and_keyword_filter():
    h1 = await _mk_alert("D123-CPU 异常", "cpu.percent")
    h2 = await _mk_alert("D123-磁盘异常", "disk.percent")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            # keyword 筛选：按指标名命中
            r = await ac.get("/api/ops/alerts", params={"keyword": "cpu.percent"}, headers=hdr)
            hashes = [x["hash"] for x in r.json()["data"]["items"]]
            assert h1 in hashes and h2 not in hashes
            # bulk delete
            r = await ac.post("/api/ops/alerts/bulk", json={"hashes": [h1], "action": "delete"}, headers=hdr)
            assert r.status_code == 200 and r.json()["data"]["affected"] == 1
            async with SessionLocal() as db:
                gone = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash == h1))).scalar_one_or_none()
                assert gone is None, "delete 必须彻底删除 cur 行"
            # 上限 500
            r = await ac.post("/api/ops/alerts/bulk",
                              json={"hashes": [f"x{i}" for i in range(501)], "action": "delete"}, headers=hdr)
            assert r.status_code == 422
    finally:
        await _rm(h2)