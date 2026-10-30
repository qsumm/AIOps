"""D171（交付规范化）：从 demo 到生产的可追溯性。

**用户要求**（2026-10-22）："我们是一个工程化的产品，所有都需要可追溯、可归档。"

三件套：
  ① request_id——每请求生成/透传 X-Request-ID，JSON 日志自动带，500 响应体带
     （用户报障时报这个号，运维 grep 日志即可定位全部上下文）；
  ② 可编辑配置类表 updated_at——"这条配置最后一次什么时候改的"必须可答
     （9 张配置表迁移 d0e1f2a3b4c5；时序/事件类表语义上不可变，不加）；
  ③ onupdate——UPDATE 时自动写，编辑端点漏写也不丢。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import app

EDITABLE_TABLES = ["alert_mutes", "notification_channels", "llm_configs", "users",
                   "alert_subscribes", "busi_groups", "busi_group_members",
                   "inspections", "heal_policies"]


def test_d171_request_id_generated_and_passthrough():
    """响应头必有 X-Request-ID；上游传入的优先透传。"""
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r1 = await c.get("/api/health")
                r2 = await c.get("/api/health", headers={"X-Request-ID": "my-trace-1"})
                return r1, r2
    r1, r2 = asyncio.run(run())
    assert r1.headers.get("X-Request-ID"), "响应头缺 X-Request-ID"
    assert len(r1.headers["X-Request-ID"]) >= 8
    assert r2.headers.get("X-Request-ID") == "my-trace-1", "上游 X-Request-ID 必须透传"


def test_d171_json_logs_carry_request_id():
    """JSON 日志（log_json=true）的每条记录自动带 request_id 字段。"""
    from app.main import _JsonFormatter, current_request_id
    import logging as _lg

    rec = _lg.LogRecord("t", _lg.WARNING, "f", 1, "hello", None, None)
    line = _JsonFormatter().format(rec)
    assert '"request_id"' in line, line


def test_d171_editable_tables_have_updated_at():
    """9 张可编辑配置表必须有 updated_at（模型层，防新表再漏）。"""
    from app.models import alert, llm, notify_rule, ops, shell

    models = {m.__name__: m for m in (
        alert.AlertMute, alert.NotificationChannel, llm.LLMConfig, shell.User,
        notify_rule.AlertSubscribe, notify_rule.BusiGroup, notify_rule.BusiGroupMember,
        ops.Inspection, ops.HealPolicy)}
    for name, m in models.items():
        cols = m.__table__.columns
        assert "updated_at" in cols, f"{name} 缺 updated_at（配置可变必须可追溯）"
        assert cols["updated_at"].default is not None or cols["updated_at"].onupdate is not None \
            or cols["updated_at"].server_default is not None, f"{name}.updated_at 无任何默认/更新来源"
