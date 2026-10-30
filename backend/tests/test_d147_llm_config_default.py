"""D147 守卫：模型配置「默认只能有一套」+ 可删除 + 删默认自动提升。

实测背景（公网）：用户换供应商时新建「通义千问 qwen3.8-max」并设为默认，但
pick_config 取的是**按主键排序的第一个 is_default** → 实际仍打到旧 MiniMax（已欠费），
测试报「服务商限流 / 额度用完」。根因是允许存在多套 is_default=True。
"""
from __future__ import annotations

import uuid

import pytest
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _client():
    """已登录的 ASGI 客户端。httpx 的 AsyncClient 不能"先请求再 async with 重开"。"""
    from app.main import app
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        ac.headers["Authorization"] = f"Bearer {tok}"
        yield ac


def _cfg(name: str, *, default: bool, key: str = "") -> dict:
    return {"name": name, "provider": "openai_compatible", "base_url": "https://example.invalid/v1",
            "api_key": key or "sk-d147-dummy", "model": "m1", "fast_model": "m1",
            "enabled": True, "is_default": default}


async def test_only_one_default_after_setting_new_default():
    """把 B 设为默认后，A 必须自动不再是默认（否则调用仍走 A）。"""
    tag = uuid.uuid4().hex[:6]
    a, b = f"d147-a-{tag}", f"d147-b-{tag}"
    async with _client() as ac:
        r = await ac.post("/api/llm-configs", json=_cfg(a, default=True))
        assert r.status_code == 200, r.text
        aid = r.json()["data"]["id"]
        r = await ac.post("/api/llm-configs", json=_cfg(b, default=True))
        assert r.status_code == 200, r.text
        bid = r.json()["data"]["id"]
        items = {x["name"]: x for x in (await ac.get("/api/llm-configs")).json()["data"]}
        assert items[b]["is_default"] is True
        assert items[a]["is_default"] is False, "旧默认必须被自动取消（多默认会让 pick_config 取错）"
        # cleanup
        for cid in (aid, bid):
            await ac.delete(f"/api/llm-configs/{cid}")


async def test_delete_default_promotes_next_enabled():
    """删除默认配置 → 剩余第一套启用配置自动成为默认（不能出现"没有默认"）。"""
    tag = uuid.uuid4().hex[:6]
    a, b = f"d147-c-{tag}", f"d147-d-{tag}"
    async with _client() as ac:
        aid = (await ac.post("/api/llm-configs", json=_cfg(a, default=True))).json()["data"]["id"]
        bid = (await ac.post("/api/llm-configs", json=_cfg(b, default=False))).json()["data"]["id"]
        r = await ac.delete(f"/api/llm-configs/{aid}")
        assert r.status_code == 200, r.text
        promoted = r.json()["data"]["promoted_default"]
        # 测试库里可能还有别的启用配置（如本地 demo 配置），提升的按主键顺序可能不是 b；
        # 断言"确实提升了一套 + 全局只有一个默认"才是本用例的口径。
        assert promoted is not None, "删默认后必须提升一套启用配置（不能没有默认）"
        items = (await ac.get("/api/llm-configs")).json()["data"]
        defaults = [x for x in items if x["is_default"]]
        assert len(defaults) == 1, f"默认必须唯一，实际 {[(x['id'], x['name']) for x in defaults]}"
        assert defaults[0]["id"] == promoted
        await ac.delete(f"/api/llm-configs/{bid}")


async def test_delete_missing_config_404():
    async with _client() as ac:
        r = await ac.delete("/api/llm-configs/999999999")
        assert r.status_code == 404
