"""D155 守卫：主机下线/恢复的**提示与状态语义**。

用户实测场景：自己把主机下线了 10 小时，期间 Agent 一直被平台 403 拒收；
恢复上线后主机已经「在线 / 认证正常 / 最近成功上报 xx:xx」，但详情页仍红字显示
「最近错误：403 Forbidden…」——用户看不懂"这是历史还是现在"。

本文件锁死四件事：
  ① **平台的 403 只有一个出处**（主机被下线/移出），且提示正文要讲清"预期行为 +
     数据在本地缓存 + 恢复后自动续传"——这句话 Agent 会原样带给用户；
  ② 平台上��**收到一次成功上报**就清掉 `last_agent_error`
     （语义从「最后一次错误」改成「**当前仍存在**的错误」，消除 Agent 那一轮滞后）；
  ③ Agent 侧：403 时读平台返回的 msg，**不要**抛 httpx 的原始异常（URL + 状态码）；
  ④ 前端：下线/恢复/移出三个抽屉的提示文案必须把后果讲全（守卫防回退）。
"""
from __future__ import annotations

import pathlib

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ── ① 平台的 403：只有一个出处，且提示要说清后果 ──
@pytest.mark.asyncio
async def test_offline_host_gets_explanatory_403():
    """下线的机器上报 → 403，且正文要说清"预期行为 + 本地缓存 + 恢复后续传"。"""
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.services import ingest

    ident = "d155-offline-host"
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        db.add(Host(ident=ident, name="d155-offline", agent_token="tok-155",
                    status="up", offline=True))
        await db.commit()

    from fastapi import HTTPException
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            await ingest.auth_agent(db, ident, "tok-155")
    assert ei.value.status_code == 403
    msg = str(ei.value.detail)
    assert "预期行为" in msg, msg
    assert "缓存" in msg, msg
    assert "续传" in msg, msg
    # 401 才是凭证失效 —— 两者不能混
    assert "凭证" not in msg, msg

    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


@pytest.mark.asyncio
async def test_removed_host_403_says_removed():
    """移出（退役）的机器 403 正文要说"已移出"，与"下线"区分。"""
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.services import ingest

    ident = "d155-removed-host"
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        db.add(Host(ident=ident, name="d155-removed", agent_token="tok-155r",
                    status="up", removed=True))
        await db.commit()

    from fastapi import HTTPException
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            await ingest.auth_agent(db, ident, "tok-155r")
    assert ei.value.status_code == 403
    assert "移出" in str(ei.value.detail)

    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


# ── ② 成功上报 → 清掉 last_agent_error ──
@pytest.mark.asyncio
async def test_successful_report_clears_last_agent_error():
    """收到一次成功上报 → 立刻清掉 last_agent_error（不再滞后一轮、也不再留历史红字）。

    真实场景：主机下线 10 小时 → 恢复后界面仍显示"在线 + 认证正常 + 403 错误"三者并存，
    用户无法判断此刻有没有问题。语义改成"当前仍存在的错误"即可自洽。
    """
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.services import ingest

    ident = "d155-clear-host"
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        db.add(Host(ident=ident, name="d155-clear", agent_token="tok-155c",
                    status="up", last_agent_error="上报失败：HTTPStatusError: 403"))
        await db.commit()

    # 模拟 Agent 恢复后的第一次成功上报（meta 里仍带着上一轮的旧错误——Agent 侧清空滞后一轮）
    async with SessionLocal() as db:
        await ingest.ingest_metrics(
            db, ident, "tok-155c",
            {"ts": "2026-10-21T01:43:22Z", "metrics": [],
             "agent_meta": {"version": "0.3.6", "last_error": "上报失败：HTTPStatusError: 403"}})
        await db.commit()

    async with SessionLocal() as db:
        h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        assert h is not None
        assert h.last_agent_error == "", "成功上报后错误应被清掉（否则界面会一直红）"
        assert h.last_report_ok_at is not None
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


@pytest.mark.asyncio
async def test_heartbeat_clears_last_agent_error():
    """心跳成功同样清错（Agent 可能只发心跳不发指标）。"""
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.services import ingest

    ident = "d155-hb-host"
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        db.add(Host(ident=ident, name="d155-hb", agent_token="tok-155h",
                    status="up", last_agent_error="上报失败：HTTPStatusError: 403"))
        await db.commit()

    async with SessionLocal() as db:
        await ingest.heartbeat(db, ident, "tok-155h", {})
        await db.commit()

    async with SessionLocal() as db:
        h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        assert h is not None and h.last_agent_error == ""
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


@pytest.mark.asyncio
async def test_failed_report_keeps_error():
    """反过来：上报**失败**时错误必须留着（这是"当前仍存在"的语义，不能一律清）。"""
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.host import Host

    ident = "d155-keep-host"
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        db.add(Host(ident=ident, name="d155-keep", agent_token="tok-155k",
                    status="up", last_agent_error="上报失败：网络不通"))
        await db.commit()

    # 不发起成功上报 → 错误保持原样（平台侧没有任何"收到成功"的证据）
    async with SessionLocal() as db:
        h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        assert h is not None and h.last_agent_error == "上报失败：网络不通"
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


# ── ③ Agent 侧：403 要读平台正文，别抛原始 HTTP 异常 ──
def test_agent_reports_platform_reason_not_raw_http_error():
    """Agent 侧守卫：403 必须翻成人话，不能是 httpx 的原始异常（URL + 状态码）。

    用户此前看到的是：`上报失败：HTTPStatusError: Client error '403 Forbidden' for
    url 'http://203.0.113.11:8848/api/ops/agents/metrics' For more information check:
    https://developer.mozilla.org/...` —— 一串 URL，看不出真正原因。
    """
    src = (ROOT / "agent/agent_main.py").read_text(encoding="utf-8")
    assert "_upload_error" in src, "Agent 没有把平台拒绝翻成人话的能力"
    assert "_platform_msg" in src, "Agent 没读平台错误体里的 msg"
    assert "PlatformRejected" in src, "Agent 没有区分「平台明确拒绝」与「网络异常」"
    # 上传路径必须走这套
    assert src.count("_upload_error(") >= 2, "上传与缓存续传两条路径都要用"
    # 两条路径都要在 raise_for_status 之前先判状态码（否则原始异常先抛出去了）
    assert src.count("status_code != 200") >= 2, "非 200 没先判状态码就 raise_for_status 了"
    # 人话文案要包含关键信息
    assert "已被下线或移出" in src, "403 的人话提示里没有说明是下线/移出"
    assert "自动续传" in src, "403 的人话提示里没有说明数据会续传"


def test_agent_error_message_has_no_raw_url():
    """Agent 上报的 last_error 不应包含长 URL（占满 255 字符且对用户无意义）。"""
    src = (ROOT / "agent/agent_main.py").read_text(encoding="utf-8")
    # 上报失败的人话文案里不带 url=
    assert '上报失败：{type(e).__name__}: {e}' not in src, "last_error 仍在塞原始异常"
    assert "PlatformRejected" in src


# ── ④ 前端：三个抽屉的提示文案 ──
def test_frontend_prompts_explain_consequences():
    """下线/恢复/移出三个抽屉的提示必须把后果讲全（守卫防回退）。"""
    src = (ROOT / "static/js/pages/hosts.js").read_text(encoding="utf-8")
    # 下线：403 是预期行为 / 数据缓存在本地 / 恢复后自动续传
    assert "预期行为" in src, "下线提示没说明 403 是预期行为"
    assert "缓存" in src, "下线提示没说明数据会缓存在本地"
    assert "自动续传" in src, "下线提示没说明恢复后会自动续传"
    assert "确认恢复上线" in src, "恢复上线没有独立提示（用户不知道会续传）"
    # 移出：与下线区分（需重新接入）
    assert "重新接入" in src, "移出提示没说明需要重新接入"


def test_host_detail_labels_error_as_current():
    """详情页的字段名要表达"当前仍存在"而不是"最近发生过"。

    用户困惑的根源：同一屏上「在线 / 认证正常 / 最近成功上报 xx:xx」都是**当前事实**，
    唯独「最近错误」是历史遗留，却用同样的红字排版。
    """
    src = (ROOT / "static/js/host_detail.js").read_text(encoding="utf-8")
    assert "当前错误" in src, "详情页还叫「最近错误」（历史语义，与当前状态混淆）"
    assert "最近错误" not in src, "「最近错误」这个标签还没改掉"


# ── ③ Agent 侧：把平台拒绝翻成人话（真实加载 agent_main 验证） ──
def _load_agent():
    import importlib.util
    import os
    import sys

    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "agent", "agent_main.py"))
    spec = importlib.util.spec_from_file_location("agent_main_d155", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("agent_main_d155", m)
    spec.loader.exec_module(m)
    return m


class _FakeResp:
    """模拟 httpx.Response：带 status_code 与平台错误体。"""

    def __init__(self, status_code: int, body: dict | None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_agent_403_message_is_actionable():
    """403（被下线/移出）→ 人话：说清"预期行为 + 数据缓存在本地 + 恢复后自动续传"。"""
    m = _load_agent()
    resp = _FakeResp(403, {"code": 403, "msg": "主机已被下线，平台暂不接收其上报；"
                                             "这是预期行为，不是故障。Agent 会把数据缓存在本地，"
                                             "恢复上线后自动续传。", "data": None})
    out = m._upload_error(resp, "上报")
    assert "预期行为" in out, out
    assert "缓存" in out, out
    assert "续传" in out, out
    # 不能出现原始 httpx 异常特征（URL / Client error / 状态文档链接）
    assert "for url" not in out, out
    assert "Client error" not in out, out
    assert "developer.mozilla.org" not in out, out
    assert len(out) <= 255, "Agent 的 last_error 只有 255 字符"


def test_agent_403_without_body_still_actionable():
    """平台没给 body（网关错误页）也不能退化成原始异常。"""
    m = _load_agent()
    out = m._upload_error(_FakeResp(403, None), "上报")
    assert "下线" in out or "移出" in out, out
    assert "for url" not in out, out


def test_agent_404_and_other_codes():
    m = _load_agent()
    out404 = m._upload_error(_FakeResp(404, {"code": 404, "msg": "资源不存在"}), "上报")
    assert "重新接入" in out404, out404
    out500 = m._upload_error(_FakeResp(500, {"code": 500, "msg": "服务器内部错误"}), "缓存续传")
    assert "缓存续传" in out500, out500
    assert "500" in out500, out500


def test_agent_reads_platform_msg_field():
    """平台错误体是 {"code":…,"msg":…,"data":null} —— Agent 必须读 msg（不是 detail）。"""
    m = _load_agent()
    resp = _FakeResp(400, {"code": 400, "msg": "模板参数不合法", "data": None})
    assert m._platform_msg(resp) == "模板参数不合法"
    # 兼容 detail 形态
    assert m._platform_msg(_FakeResp(400, {"detail": "另一种形态"})) == "另一种形态"
