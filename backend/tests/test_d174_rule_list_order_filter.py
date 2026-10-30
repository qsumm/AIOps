"""D174：告警规则列表的两个用户可见缺陷（e2e C7 逮住，用户也会撞上）。

缺陷一（排序）：`GET /api/ops/alert-rules` 此前按 `id` **升序**返回，页面按页切片。
规则数超过一页后，**刚创建的规则（id 最大）排在最后一页**——用户建完规则回到列表
「找不到刚建的规则」。实测库里 101 条规则时必现。

缺陷二（筛选）：「启用中 / 已停用 / 全部」此前是**前端**在当前页数据上过滤 →
跨页漏规则，且计数行显示的是"当前页条数"而不是全库条数（页面自己说的数就是错的）。
现改为服务端过滤（`show` 参数）+ 回传全量计数。

守卫要点：
  ① 新建的规则必须出现在**第一页**；
  ② `show=enabled|disabled` 过滤在服务端生效（返回的 total 是过滤后的条数）；
  ③ `counts` 回传全库启用/停用数（页面计数行不许再说假话）。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models.alert import AlertRule

PRE = "d174-rule"


async def _mk(name: str, disabled: bool = False) -> int:
    async with SessionLocal() as db:
        r = AlertRule(name=name, scope="metric", metric="cpu.percent", expr=">99",
                      severity=2, disabled=disabled)
        db.add(r)
        await db.commit()
        await db.refresh(r)
        return r.id


async def _rm(ids: list[int]) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(AlertRule).where(AlertRule.id.in_(ids)))
        await db.commit()


@pytest.mark.asyncio
async def test_new_rule_appears_on_first_page():
    """① 新建规则必须落在第一页（id 倒序）。"""
    ids = []
    try:
        for i in range(3):
            ids.append(await _mk(f"{PRE}-ord-{i}"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            r = await ac.get("/api/ops/alert-rules", params={"page": 1, "page_size": 20}, headers=hdr)
            assert r.status_code == 200
            items = r.json()["data"]["items"]
            names = [x["name"] for x in items]
            assert f"{PRE}-ord-2" in names, (
                f"最新建的规则不在第一页——用户建完看不到（当前第一页 {names[:8]}…）")
            # 倒序：页内 id 递减
            ids_in_page = [x["id"] for x in items]
            assert ids_in_page == sorted(ids_in_page, reverse=True), "规则应按 id 倒序（新的在前）"
    finally:
        await _rm(ids)


@pytest.mark.asyncio
async def test_show_filter_is_server_side():
    """②③ show 过滤在服务端生效，且 counts 是全库计数。"""
    ids = []
    try:
        ids.append(await _mk(f"{PRE}-en-1", disabled=False))
        ids.append(await _mk(f"{PRE}-dis-1", disabled=True))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            r = await ac.get("/api/ops/alert-rules",
                             params={"page": 1, "page_size": 200, "show": "disabled"}, headers=hdr)
            d = r.json()["data"]
            assert all(x["disabled"] for x in d["items"]), "show=disabled 不得返回启用中的规则"
            assert any(x["name"] == f"{PRE}-dis-1" for x in d["items"])
            assert not any(x["name"] == f"{PRE}-en-1" for x in d["items"])

            r2 = await ac.get("/api/ops/alert-rules",
                              params={"page": 1, "page_size": 200, "show": "enabled"}, headers=hdr)
            d2 = r2.json()["data"]
            assert all(not x["disabled"] for x in d2["items"]), "show=enabled 不得返回停用规则"
            assert any(x["name"] == f"{PRE}-en-1" for x in d2["items"])

            # counts 是全库计数（与过滤无关）
            c = d2["counts"]
            assert c["all"] == c["enabled"] + c["disabled"], c
            assert c["all"] >= 2
    finally:
        await _rm(ids)


@pytest.mark.asyncio
async def test_show_rejects_invalid_value():
    """非法 show 值直接 422（不做静默兜底）。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        r = await ac.get("/api/ops/alert-rules", params={"show": "weird"}, headers=hdr)
        assert r.status_code == 422
