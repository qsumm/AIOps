"""D149 守卫：告警详情必须能解释「为什么这个值也告警」。

用户实测：磁盘使用率 **7.9%** 收到 S2 告警，详情只显示规则名 → 看起来像平台误报。
查规则发现 expr=`>1`（阈值写错）。产品侧要保证三件事：
  A 告警列表/详情带出**规则触发条件**（trigger_expr），不再只有规则名；
  B 百分比指标 + 明显偏离常识的阈值 → 详情与规则编辑器给出**阈值提醒**（只提示不拦截）；
  C AI 摘要的提示词必须包含触发条件，并能点出「阈值可能配置过低」。
"""
from __future__ import annotations

import pathlib
import uuid
from datetime import datetime

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"


@pytest.mark.asyncio
async def test_alert_list_exposes_rule_expr():
    """列表接口必须带 trigger_expr（详情抽屉直接用它展示触发条件）。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.main import app
    from app.models.alert import AlertCurEvent, AlertRule

    tag = uuid.uuid4().hex[:8]
    name = f"d149-rule-{tag}"
    hsh = f"d149{uuid.uuid4().hex[:28]}"
    async with SessionLocal() as db:
        rule = AlertRule(name=name, scope="metric", metric="disk.percent", expr=">1",
                         severity=2, eval_interval_s=30, window_s=60)
        db.add(rule)
        await db.commit()
        db.add(AlertCurEvent(hash=hsh, rule_id=rule.id, rule_name=name, host_ident="d149-host",
                             metric="disk.percent", severity=2, trigger_value=7.9,
                             trigger_time=datetime.utcnow(), first_trigger_time=datetime.utcnow()))
        await db.commit()
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
            tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
            items = (await ac.get("/api/ops/alerts?keyword=" + name,
                                  headers={"Authorization": f"Bearer {tok}"})).json()["data"]["items"]
        assert items, "应按关键字查到刚造的告警"
        assert items[0]["trigger_expr"] == ">1", f"详情缺少触发条件：{items[0]}"
    finally:
        async with SessionLocal() as db:
            row = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash == hsh))).scalar_one_or_none()
            if row is not None:
                await db.delete(row)
            rr = (await db.execute(select(AlertRule).where(AlertRule.name == name))).scalar_one_or_none()
            if rr is not None:
                await db.delete(rr)
            await db.commit()


def test_frontend_shows_condition_and_hint():
    alerts = (STATIC / "js" / "pages" / "alerts.js").read_text(encoding="utf-8")
    assert "trigger_expr" in alerts and "触发条件" in alerts, "详情必须展示规则触发条件"
    assert "阈值提醒" in alerts, "百分比指标的异常阈值必须在详情里提示"
    assert "触发值" in alerts and '"当前值"' not in alerts, "字段名应与列表口径一致（触发值）"
    rules = (STATIC / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
    assert "syncExprWarn" in rules and "阈值提醒" in rules, "规则编辑器必须在保存前提示异常阈值"
    assert "80~90" in rules


def test_ai_summary_prompt_includes_condition():
    prompts = (ROOT / "backend" / "app" / "services" / "prompts.py").read_text(encoding="utf-8")
    assert "rule_expr" in prompts, "摘要提示词必须包含触发条件"
    assert "阈值可能配置过低" in prompts, "摘要必须能点出可疑阈值（否则会放大误报感）"
    core = (ROOT / "backend" / "app" / "engine" / "core.py").read_text(encoding="utf-8")
    assert '"rule_expr"' in core, "生成摘要时必须把规则的 expr 传进去"
