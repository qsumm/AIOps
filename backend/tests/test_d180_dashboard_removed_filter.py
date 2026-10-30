"""D180 大盘聚合必须过滤已移出（removed=1）主机的回归锁。

缺陷（台账 v1.90，隔离演示栈实测实锤）：D155「移出」语义只隐藏主机列表，
但大盘总览（/api/ops/dashboard/overview）的聚合仍统计 removed 行——
  ① 健康率分母虚增：移出 1 台，100% 立刻掉到 50%；
  ② 宕机名单（down_hosts 是裸 ident）与磁盘榜/资源 Top5 直接显示
     `b2b72d3aeeb` 这类哈希名。
修法：overview 的主机查询统一 `removed == False`——一处过滤管四处聚合
（健康率 up/total、宕机名单、磁盘分区明细、资源 Top5）。
"""
import hashlib
from datetime import datetime

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, func, select

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.host import Host
from app.models.shell import User
from app.store import ring

PFX = "d180"


def _ident(name: str) -> str:
    return hashlib.sha256(f"{PFX}-{name}".encode()).hexdigest()[:32]


async def _client_as_admin():
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "admin"))).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: u
    return app


async def _seed_hosts() -> list[str]:
    """三台：正常在线 / 已移出但仍 up（对齐实测形态）/ 真实宕机。"""
    idents = [_ident(f"{PFX}-{n}") for n in ("up-ok", "removed-up", "down-real")]
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident.in_(idents)))
        db.add(Host(ident=idents[0], name=f"{PFX}-up-ok", status="up"))
        db.add(Host(ident=idents[1], name=f"{PFX}-removed-up", status="up", removed=True))
        db.add(Host(ident=idents[2], name=f"{PFX}-down-real", status="down"))
        await db.commit()
    return idents


async def _cleanup(idents: list[str]) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident.in_(idents)))
        await db.commit()


async def _expected_counts() -> tuple[int, int]:
    """与修后口径一致的期望值：可见主机 = 全库 removed=False 的行。"""
    async with SessionLocal() as db:
        total = (await db.execute(
            select(func.count()).select_from(Host).where(Host.removed == False))).scalar()  # noqa: E712
        up = (await db.execute(
            select(func.count()).select_from(Host)
            .where(Host.removed == False, Host.status == "up"))).scalar()  # noqa: E712
    return total, up


@pytest.mark.asyncio
async def test_removed_host_excluded_from_health_rate_and_down_list():
    """移出主机不得计入健康率分母，也不得出现在宕机名单（down_hosts）。"""
    idents = await _seed_hosts()
    expected_total, expected_up = await _expected_counts()
    app = await _client_as_admin()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/api/ops/dashboard/overview")
        assert r.status_code == 200
        hosts = r.json()["data"]["hosts"]
        assert hosts["total"] == expected_total, \
            f"健康率分母混入 removed 行：overview={hosts['total']} 期望={expected_total}"
        assert hosts["up"] == expected_up, \
            f"up 计数混入 removed 行：overview={hosts['up']} 期望={expected_up}"
        assert idents[1] not in (hosts.get("down_hosts") or []), "宕机名单混入 removed 行"
        assert idents[2] in (hosts.get("down_hosts") or []), "真实宕机主机应在名单里"
    finally:
        await _cleanup(idents)


@pytest.mark.asyncio
async def test_removed_host_excluded_from_disks_and_top5():
    """移出主机即便 status=up，也不得出现在磁盘分区明细/资源 Top5（裸哈希不入大盘）。"""
    idents = await _seed_hosts()
    now = datetime.utcnow()
    ring.set_disks(idents[0], [{"mount": "/", "used": 40.0, "size": 100.0, "percent": 40.0}])
    ring.set_disks(idents[1], [{"mount": "/", "used": 90.0, "size": 100.0, "percent": 90.0}])
    ring.append_point(idents[0], "mem.percent", now, 55.0)
    ring.append_point(idents[1], "mem.percent", now, 95.0)
    app = await _client_as_admin()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/api/ops/dashboard/overview")
        assert r.status_code == 200
        data = r.json()["data"]
        disk_labels = " ".join(d.get("host", "") for d in data.get("disks", []))
        assert f"{PFX}-removed-up" not in disk_labels, f"磁盘榜混入 removed 行：{disk_labels}"
        assert idents[1][:8] not in disk_labels, f"磁盘榜混入 removed 裸哈希：{disk_labels}"
        for key in ("mem", "disk", "net"):
            labels = " ".join(x.get("name", "") for x in data.get("top5", {}).get(key, []))
            assert f"{PFX}-removed-up" not in labels, f"top5.{key} 混入 removed 行：{labels}"
    finally:
        ring.set_disks(idents[0], [])
        ring.set_disks(idents[1], [])
        await _cleanup(idents)
