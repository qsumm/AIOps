"""D59 全局表单校验回归（用户报：渠道名称可空保存 + 保存后不能编辑）。

口径：**刚需字段必须必填**，空值/超长/重名拦在写库前给可读 400，不落 DB 撞 500。
覆盖三层：
  ① 空值/超长 → 400（渠道/LLM/数据源/规则/技能/巡检/mute/License）
  ② 重名 → 400 人话（原先撞 unique 索引 500）
  ③ 编辑能力：渠道 PUT（新接口）合并语义——留空 = 保留旧值（出参是掩码，不能写回）
"""
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.alert import AlertRule, NotificationChannel
from app.models.shell import User


async def _client():
    app = create_app()
    async with SessionLocal() as db:
        admin = await db.get(User, 1)
    app.dependency_overrides[get_current_user] = lambda: admin
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _cleanup(tbl, col, val):
    async with SessionLocal() as db:
        await db.execute(delete(tbl).where(col == val))
        await db.commit()


# ── ① 渠道：名称/类型/配置 完整性 ──
@pytest.mark.asyncio
async def test_channel_required_fields():
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        # 空名（用户报的原样场景）
        r = await ac.post("/api/ops/notify-channels", json={"name": "  ", "type": "webhook",
                                                            "config_json": {"webhook_url": "http://x/y"}})
        assert r.status_code == 400 and "必填" in r.text, r.text[:160]
        # 名称超长（列宽 64）
        r = await ac.post("/api/ops/notify-channels", json={"name": "长" * 65, "type": "webhook",
                                                            "config_json": {"webhook_url": "http://x/y"}})
        assert r.status_code == 400 and "64" in r.text, r.text[:160]
        # 空类型
        r = await ac.post("/api/ops/notify-channels", json={"name": "d59-t", "type": "",
                                                            "config_json": {"webhook_url": "http://x/y"}})
        assert r.status_code == 400, r.text[:160]
        # 友好类型但一个投递配置都没有 → 400（死渠道拦在门外）
        r = await ac.post("/api/ops/notify-channels", json={"name": "d59-t", "type": "feishu", "config_json": {}})
        assert r.status_code == 400 and "投递配置" in r.text, r.text[:160]
        # webhook_url 不是 http(s)
        r = await ac.post("/api/ops/notify-channels", json={"name": "d59-t", "type": "webhook",
                                                            "config_json": {"webhook_url": "ftp://x/y"}})
        assert r.status_code == 400 and "http" in r.text, r.text[:160]
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_channel_duplicate_name_and_edit():
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        await _cleanup(NotificationChannel, NotificationChannel.name, "d59-ch")
        body = {"name": "d59-ch", "type": "webhook", "config_json": {"webhook_url": "http://127.0.0.1:1/hook", "secret": "s1"}}
        r = await ac.post("/api/ops/notify-channels", json=body)
        assert r.status_code == 200, r.text[:160]
        cid = r.json()["data"]["id"]
        try:
            # 重名 → 409 人话（D8 起全局口径；原先撞 unique 索引 500）
            r = await ac.post("/api/ops/notify-channels", json={**body, "config_json": {"webhook_url": "http://x/z"}})
            assert r.status_code == 409 and "已存在" in r.text, r.text[:160]
            # 编辑：名称保留、URL 留空 = 保留旧值、secret 覆盖
            r = await ac.put(f"/api/ops/notify-channels/{cid}",
                             json={"name": "d59-ch", "type": "webhook",
                                   "config_json": {"webhook_url": "", "secret": "s2"}, "enabled": True})
            assert r.status_code == 200, r.text[:160]
            async with SessionLocal() as db:
                ch = await db.get(NotificationChannel, cid)
                assert ch.config_json["webhook_url"] == "http://127.0.0.1:1/hook", ch.config_json
                assert ch.config_json["secret"] == "s2", ch.config_json
            # 编辑重名（撞别的渠道）→ 400
            r2 = await ac.post("/api/ops/notify-channels", json={"name": "d59-ch-2", "type": "webhook",
                                                                 "config_json": {"webhook_url": "http://x/z"}})
            assert r2.status_code == 200
            cid2 = r2.json()["data"]["id"]
            r = await ac.put(f"/api/ops/notify-channels/{cid2}",
                             json={"name": "d59-ch", "type": "webhook", "config_json": {}})
            assert r.status_code == 409 and "已存在" in r.text, r.text[:160]
            await _cleanup(NotificationChannel, NotificationChannel.name, "d59-ch-2")
            # 不存在 → 404
            r = await ac.put("/api/ops/notify-channels/99999999",
                             json={"name": "x", "type": "webhook", "config_json": {"webhook_url": "http://x/z"}})
            assert r.status_code == 404
        finally:
            await _cleanup(NotificationChannel, NotificationChannel.name, "d59-ch")
    app.dependency_overrides.clear()


# ── ② LLM 配置：名称/主模型必填 + 重名 ──
@pytest.mark.asyncio
async def test_llm_config_required_and_duplicate():
    from app.models.llm import LLMConfig

    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/llm-configs", json={"name": " ", "model": "m"})
        assert r.status_code == 400 and "必填" in r.text, r.text[:160]
        r = await ac.post("/api/llm-configs", json={"name": "d59-llm", "model": ""})
        assert r.status_code == 400 and "主模型" in r.text, r.text[:160]
        r = await ac.post("/api/llm-configs", json={"name": "d59-llm", "model": "m", "base_url": "ftp://bad"})
        assert r.status_code == 400 and "http" in r.text, r.text[:160]
        await _cleanup(LLMConfig, LLMConfig.name, "d59-llm")
        r = await ac.post("/api/llm-configs", json={"name": "d59-llm", "model": "m"})
        assert r.status_code == 200, r.text[:160]
        r = await ac.post("/api/llm-configs", json={"name": "d59-llm", "model": "m"})
        assert r.status_code == 409 and "已存在" in r.text, r.text[:160]  # 原先 500
        await _cleanup(LLMConfig, LLMConfig.name, "d59-llm")
    app.dependency_overrides.clear()


# ── ③ Prom 数据源：名称/地址必填 + scheme ──
@pytest.mark.asyncio
async def test_prom_ds_required():
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/ops/prom-datasources", json={"name": "", "base_url": "http://127.0.0.1:9090"})
        assert r.status_code == 400 and "必填" in r.text, r.text[:160]
        r = await ac.post("/api/ops/prom-datasources", json={"name": "d59-ds", "base_url": ""})
        assert r.status_code == 400 and "必填" in r.text, r.text[:160]
        r = await ac.post("/api/ops/prom-datasources", json={"name": "d59-ds", "base_url": "127.0.0.1:9090"})
        assert r.status_code == 400 and "http" in r.text, r.text[:160]
    app.dependency_overrides.clear()


# ── ④ 规则：重名 400（原先 500）+ 轨 A 指标名必填 ──
@pytest.mark.asyncio
async def test_rule_duplicate_and_metric_required():
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        await _cleanup(AlertRule, AlertRule.name, "d59-rule")
        r = await ac.post("/api/ops/alert-rules", json={"name": "d59-rule", "metric": "cpu.percent", "expr": ">90"})
        assert r.status_code == 200, r.text[:160]
        rid = r.json()["data"]["id"]
        try:
            r = await ac.post("/api/ops/alert-rules", json={"name": "d59-rule", "metric": "mem.percent", "expr": ">90"})
            assert r.status_code == 409 and "已存在" in r.text, r.text[:160]
            # 改名撞自己 = 放行（exclude_id 生效）
            r = await ac.put(f"/api/ops/alert-rules/{rid}", json={"name": "d59-rule", "metric": "cpu.percent", "expr": ">95"})
            assert r.status_code == 200, r.text[:160]
        finally:
            await _cleanup(AlertRule, AlertRule.name, "d59-rule")
        r = await ac.post("/api/ops/alert-rules", json={"name": "d59-rule-b", "scope": "metric", "metric": " ", "expr": ">90"})
        assert r.status_code == 400 and "指标名" in r.text, r.text[:160]
        r = await ac.post("/api/ops/alert-rules", json={"name": "d59-rule-c", "scope": "prom", "queries_json": [], "expr": ">90"})
        assert r.status_code == 400 and "PromQL" in r.text, r.text[:160]
    app.dependency_overrides.clear()


# ── ⑤ 技能：名称必填；巡检：任务名必填；mute：原因必填；License：空激活码 ──
@pytest.mark.asyncio
async def test_misc_required_fields():
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/ops/skills", json={"name": " ", "description": "d59"})
        assert r.status_code == 422 and "技能名" in r.text, r.text[:160]
        r = await ac.post("/api/ops/inspections", json={"name": "", "cron_expr": "0 9 * * *"})
        assert r.status_code == 400 and "必填" in r.text, r.text[:160]
        r = await ac.post("/api/ops/alert-mutes", json={"cause": "  ", "tag_filters": [], "mute_type": 0})
        assert r.status_code == 400 and "必填" in r.text, r.text[:160]
        r = await ac.post("/api/ops/alert-mutes", json={"cause": "d59", "tag_filters": [{"k": "metric", "op": "==", "v": ""}], "mute_type": 0})
        assert r.status_code == 400 and "匹配值" in r.text, r.text[:160]
        r = await ac.post("/api/license/activate", json={"license_key": " "})
        assert r.status_code == 400 and "激活码" in r.text, r.text[:160]
    app.dependency_overrides.clear()
