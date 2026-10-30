"""D148 守卫：历史会话不能是空的（用户实测：打开历史对话看不到消息）。

根因链：
  ① 会话在**调用模型之前**创建并提交（此时 messages=[]）；
  ② 用户消息此前只在整轮回答成功后才落库；
  ③ 模型报错/超时/客户端断开时 → 会话保留但一条消息都没有 → 历史里出现「0 条」空会话，
     点进去一片空白（公网实测：MiniMax 欠费期间留下多条）。
锁三件：
  A 用户消息**立即落库**（失败也不丢"我问过什么"）；
  B 失败时补一条助手错误消息（回看能知道"这轮为什么没答上"）；
  C 会话列表**不展示 0 条空会话**。
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "app" / "routers" / "incidents.py"


def test_user_message_persisted_before_llm_call():
    src = INCIDENTS.read_text(encoding="utf-8")
    # 在 msgs.append(user) 之后必须紧跟落库（sess.messages = msgs + commit），
    # 且出现在 call_llm 之前
    i_append = src.index('msgs.append({"role": "user", "content": text')
    i_persist = src.index("sess.messages = msgs", i_append)
    i_llm = src.index("call_llm(db, \"chat\"", i_append)
    assert i_append < i_persist < i_llm, \
        "用户消息必须在调用模型之前落库（否则失败时历史为空）"


def test_failure_appends_assistant_error_and_hides_empty_sessions():
    src = INCIDENTS.read_text(encoding="utf-8")
    assert "_persist_chat_failure(sess" in src, "失败路径必须把错误写入会话"
    assert "0 条消息的空会话不展示" in src, "列表必须过滤空会话"
    assert "for r in items" in src and "if r.messages" in src, "过滤逻辑必须按 messages 非空"


def test_frontend_empty_session_has_explicit_state():
    js = (ROOT.parent / "static" / "js" / "pages" / "copilot.js").read_text(encoding="utf-8")
    assert "这段会话没有消息" in js, "空会话必须有明确空态，不能一片白"


@pytest.mark.asyncio
async def test_failed_model_call_leaves_question_and_error_in_history(monkeypatch):
    """模型调用失败 → 历史里仍有「问题 + 失败说明」，而不是 0 条空会话。"""
    import asyncio as _a

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.main import app
    from app.models.event import AIChatSession

    import app.routers.incidents as m

    async def _boom(*a, **kw):
        raise RuntimeError("model down (d148 simulated)")

    monkeypatch.setattr(m, "call_llm", _boom)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        async with ac.stream("POST", "/api/ops/chat/stream",
                             json={"message": "D148 失败用例"}, headers=h) as r:
            await r.aread()
        await _a.sleep(0.2)
        rows = (await ac.get("/api/ops/chat/sessions", headers=h)).json()["data"]["items"]
        target = next((x for x in rows if x["title"].startswith("D148 失败用例")), None)
        assert target is not None, "失败的会话也必须出现在历史里（有问有答，不是空壳）"
    async with SessionLocal() as db:
        s = (await db.execute(select(AIChatSession).where(
            AIChatSession.id == target["id"]))).scalar_one()
        msgs = s.messages or []
        assert [x.get("role") for x in msgs] == ["user", "assistant"], f"实际消息：{msgs}"
        assert "失败" in (msgs[-1].get("content") or ""), "失败原因必须写进回答"
