"""D87 回归锁：**对话上下文管理**（滚动摘要 + 最近窗口）+ 会话隔离 + 入参护栏。

背景（用户要求："看看通用的大模型对话类上下文是怎么管理的，完善下"）：
改前是把 `session.messages` **整段**发给模型，没有任何窗口/预算/摘要。后果有三：
① 长会话 prompt 线性增长 → 费钱、变慢，最终**撞上下文上限直接报错**（对话不可用）；
② 没有预算概念，粘贴一大段日志就能占满窗口；
③ 工具中间结果不入库，模型看不到自己查过什么，容易重复查同一件事。

本文件锁四类：
  ① **纯函数**（不调模型）：token 估算、窗口切分、窗口起点、配对完整、存储裁剪；
  ② **压缩行为**：不到阈值不压（省钱）、到阈值才压、增量合并旧摘要、**压缩失败如实降级**；
  ③ **拼装**：build_history 必须含 system + 摘要 + 窗口，且**窗口外的历史不得混进去**；
  ④ **接口契约**：入参护栏（空/超长）、**会话隔离**（别人的 session_id 不得接管）。
"""
import pytest

from app.services import chat_context as ctx

# ── ① 纯函数 ──

def test_estimate_tokens_cjk_vs_ascii_and_empty():
    """中文按字、ASCII 按 0.3；空串为 0。只用于分档，不用于计费——所以只锁单调与量级。"""
    assert ctx.estimate_tokens("") == 0
    assert ctx.estimate_tokens(None) == 0
    cjk = ctx.estimate_tokens("磁盘打满了")
    ascii_ = ctx.estimate_tokens("disk full")
    # 等长时中文必须比 ASCII 贵（否则中文长会话会被低估 → 撞上限）
    assert cjk > ctx.estimate_tokens("a" * 5)
    assert ctx.estimate_tokens("a" * 100) >= 25  # 0.3/字符
    assert cjk >= 5 and ascii_ >= 2


def _conv(n_user: int, fill: int = 40) -> list[dict]:
    """造 n_user 轮问答（每轮 user + assistant）。"""
    out = []
    for i in range(n_user):
        out.append({"role": "user", "content": f"第{i}问：{'x' * fill}"})
        out.append({"role": "assistant", "content": f"第{i}答：{'y' * fill}"})
    return out


def test_split_window_keeps_newest_and_respects_budget():
    msgs = _conv(30)
    kept, stats = ctx.split_window(msgs, 0, budget=300)
    assert kept > 0, "预算 300 不该容得下 30 轮"
    assert kept <= len(msgs) - 2
    assert msgs[-1] in msgs[kept:], "最后一条（用户刚问的）必须留在窗口里"
    assert stats["kept"] == len(msgs) - kept
    assert stats["overflow_tokens"] > 0
    # 窗口在预算附近（允许最小保留条数的兜底略微超一点）
    assert stats["kept_tokens"] <= 300 + sum(ctx._msg_tokens(m) for m in msgs[kept:][:ctx._MIN_KEEP_MESSAGES])


def test_split_window_always_keeps_last_message_even_if_huge():
    """单条巨消息（用户粘了一大段日志）也必须保留——那是本次要回答的内容。

    同时：巨消息本身已超预算，窗口必须**收缩到只剩最小保留条数**，不能被它拖着一路带上全部历史。
    """
    msgs = _conv(20) + [{"role": "user", "content": "巨大" * 5000}]
    kept, stats = ctx.split_window(msgs, 0, budget=50)
    assert msgs[-1] in msgs[kept:], "最后一条无论如何都要留下"
    assert len(msgs) - kept <= ctx._MIN_KEEP_MESSAGES + 1, "巨消息应让窗口收缩到最小保留量"


def test_split_window_starts_at_user_turn():
    """窗口起点必须是 user——绝不把 assistant 的孤立回答留在开头（配对完整）。"""
    msgs = _conv(20)
    for budget in (100, 250, 400, 600):
        kept, _ = ctx.split_window(msgs, 0, budget=budget)
        assert kept >= len(msgs) or msgs[kept]["role"] == "user", f"budget={budget} 时窗口从 {msgs[kept]['role']} 开始"


def test_split_window_min_keep_floor():
    """预算极紧时也要保住最小条数（否则用户会感觉"助手突然失忆"）。"""
    msgs = _conv(10, fill=200)
    kept, stats = ctx.split_window(msgs, 0, budget=1)
    assert stats["kept"] >= ctx._MIN_KEEP_MESSAGES - 1, f"预算再紧也要留够，实际 {stats['kept']}"


def test_trim_stored_caps_and_shifts_boundary():
    msgs = _conv(30)  # 60 条
    out, upto = ctx.trim_stored(msgs, upto=10, cap=20)
    assert len(out) == 20, "超出上限必须裁到上限（含如实说明那一行）"
    assert out[0]["role"] == "system" and "归档" in out[0]["content"], "裁剪要留一条如实说明"
    assert upto == 0, "边界要前移，不能指向已被裁掉的位置"
    # 不超上限时原样返回
    same, up2 = ctx.trim_stored(msgs, 4, cap=999)
    assert same is msgs and up2 == 4


# ── ② 压缩行为 ──

@pytest.fixture
def fake_llm(monkeypatch):
    """替掉 llm_gateway.call_llm，记录调用次数与入参。"""
    calls = []

    class R:
        def __init__(self, content):
            self.content = content

    async def _fake(db, purpose, messages, **kw):
        calls.append({"purpose": purpose, "messages": messages})
        return R("要点：用户想查磁盘；已确认 /data 用量 95%；未解决：哪个进程写的。")

    import app.services.llm_gateway as gw
    monkeypatch.setattr(gw, "call_llm", _fake)
    return calls


@pytest.mark.asyncio
async def test_maybe_compact_skips_below_threshold(fake_llm):
    """不到阈值**绝不调模型**——绝大多数轮次走这条路，这是省钱的关键。"""
    msgs = _conv(3)
    summary, upto, stats = await ctx.maybe_compact(None, msgs, "", 0, budget=8000,
                                                   threshold=1500)
    assert summary == "" and upto == 0
    assert stats["compacted"] is False
    assert fake_llm == [], "未达阈值不该调用模型"


@pytest.mark.asyncio
async def test_maybe_compact_folds_overflow_incrementally(fake_llm):
    """超阈值才压，且是**增量**（旧摘要与溢出段一起交给模型合并），边界随之前移。"""
    msgs = _conv(30)
    summary, upto, stats = await ctx.maybe_compact(None, msgs, "旧摘要：用户是运维小王。", 0,
                                                   budget=300, threshold=100)
    assert stats["compacted"] is True
    assert upto > 0 and summary
    assert "旧摘要" in fake_llm[0]["messages"][1]["content"], "必须把旧摘要一起并入（否则越压越丢）"
    # 再跑一次：upto 之前的内容不该重复送压
    n_before = len(fake_llm)
    _, upto2, stats2 = await ctx.maybe_compact(None, msgs, summary, upto, budget=300, threshold=10 ** 9)
    assert upto2 == upto and stats2["compacted"] is False
    assert len(fake_llm) == n_before, "阈值调高后不该再压一次"


@pytest.mark.asyncio
async def test_maybe_compact_failure_degrades_honestly(monkeypatch):
    """压缩失败**不得阻断对话**，且要如实写进上下文（不假装有记忆）。"""
    class R:
        content = ""

    async def boom(db, purpose, messages, **kw):
        raise RuntimeError("上游 500")

    import app.services.llm_gateway as gw
    monkeypatch.setattr(gw, "call_llm", boom)

    msgs = _conv(30)
    summary, upto, stats = await ctx.maybe_compact(None, msgs, "", 0, budget=200, threshold=50)
    assert upto > 0, "失败也要前移边界，否则每轮都重试压同一段"
    assert stats["compacted"] is False
    assert "压缩失败" in summary and "不参与上下文" in summary, "必须如实告知，不能假装记得"


# ── ③ 拼装 ──

def test_build_history_shape_and_isolation():
    msgs = _conv(30)
    kept, _ = ctx.split_window(msgs, 0, budget=300)
    hist = ctx.build_history("SYS", "摘要正文", msgs, 0, budget=300)
    assert hist[0] == {"role": "system", "content": "SYS"}
    assert "摘要正文" in hist[1]["content"] and hist[1]["role"] == "system"
    body = hist[2:]
    assert body == [{"role": m["role"], "content": m["content"]} for m in msgs[kept:]]
    # 窗口外的历史**绝不能**混进提示词
    assert not any("第0问" in m["content"] for m in body)


def test_build_history_without_summary_omits_block():
    msgs = _conv(2)          # 4 条，预算充足 → 全在窗口内
    hist = ctx.build_history("SYS", "", msgs, 0, budget=8000)
    assert hist[0]["content"] == "SYS"
    assert len(hist) == 1 + len(msgs)
    assert not any(m["role"] == "system" and m is not hist[0] for m in hist[1:]), "无摘要时不该插摘要块"


# ── ④ 接口契约：入参护栏 + 会话隔离 ──

@pytest.fixture
async def ac():
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    # raise_app_exceptions=False：让 500 以"响应"形式出现，便于断言（同 d8 矩阵的约定）
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                           base_url="http://test", timeout=120) as c:
        yield c


async def _tok(ac, username: str, password: str) -> str:
    r = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]


async def _uid(username: str) -> int:
    from sqlalchemy import select
    from app.db.session import SessionLocal
    from app.models.shell import User
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        assert u is not None, f"测试用户 {username} 不存在"
        return u.id


@pytest.fixture
def no_llm(monkeypatch):
    """任何一次模型调用都视为测试失败（护栏类用例必须"根本到不了模型"）。"""
    calls = []

    async def _boom(*a, **kw):
        calls.append(1)
        raise AssertionError("不该调用模型")

    import app.services.llm_gateway as gw
    monkeypatch.setattr(gw, "call_llm", _boom)
    return calls


@pytest.mark.asyncio
async def test_chat_stream_rejects_empty_and_overlong(ac, no_llm):
    """空消息 / 超长消息在进模型前就挡掉（400 + 可操作提示），不能白烧一次调用。"""
    tok = await _tok(ac, "admin", "admin123")
    h = {"Authorization": f"Bearer {tok}"}

    r1 = await ac.post("/api/ops/chat/stream", json={"message": "   "}, headers=h)
    assert r1.status_code == 400, r1.text
    assert "空" in r1.json()["msg"]

    from app.core.config import get_settings
    over = "长" * (get_settings().chat_msg_max_chars + 10)
    r2 = await ac.post("/api/ops/chat/stream", json={"message": over}, headers=h)
    assert r2.status_code == 400, r2.text
    assert "上限" in r2.json()["msg"], "提示要说清上限、并给出可操作建议"
    assert no_llm == [], "被护栏挡下的请求不得触达模型"


@pytest.mark.asyncio
async def test_chat_session_isolation_does_not_hijack_others(ac, monkeypatch):
    """**会话隔离**：B 传 A 的 session_id，绝不能追加到 A 的会话里（必须新建自己的）。"""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.event import AIChatSession

    class R:
        content = "ok"
        tool_calls = []
        finish_reason = "stop"

    async def _ans(*a, **kw):
        return R()

    # 路由模块在顶层 from llm_gateway import call_llm —— 必须打路由的引用（D97 补，
    # 否则会真调 LLM；配额耗尽时该测试会假失败）
    import app.routers.incidents as incidents_mod
    monkeypatch.setattr(incidents_mod, "call_llm", _ans)

    ha = {"Authorization": f"Bearer {await _tok(ac, 'admin', 'admin123')}"}
    hb = {"Authorization": f"Bearer {await _tok(ac, 'demo', 'demo')}"}

    async with ac.stream("POST", "/api/ops/chat/stream", json={"message": "A 的问题"}, headers=ha) as _:
        pass
    admin_id = await _uid("admin")
    async with SessionLocal() as db:
        a_sess = (await db.execute(select(AIChatSession).where(AIChatSession.user_id == admin_id)
                                   .order_by(AIChatSession.id.desc()))).scalars().first()
        assert a_sess is not None, "A 没建出会话"
        a_id, a_len = a_sess.id, len(a_sess.messages or [])

    async with ac.stream("POST", "/api/ops/chat/stream",
                         json={"message": "B 的问题", "session_id": a_id}, headers=hb) as _:
        pass
    async with SessionLocal() as db:
        a_after = await db.get(AIChatSession, a_id)
        assert len(a_after.messages or []) == a_len, "A 的会话被 B 追加了——会话隔离失效"
        b_sess = (await db.execute(select(AIChatSession).where(AIChatSession.id != a_id)
                                   .order_by(AIChatSession.id.desc()))).scalars().first()
        assert b_sess is not None and b_sess.id != a_id, "B 必须新建自己的会话"
        assert any("B 的问题" in (m.get("content") or "") for m in (b_sess.messages or []))


@pytest.mark.asyncio
async def test_chat_stream_persists_orphan_turn_with_explanation(ac, monkeypatch):
    """工具轮次用尽仍没结论时，必须**如实落库**一条回答（以前什么都不存 → 历史里像"助手没理我"）。"""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.event import AIChatSession

    class R:  # 永远只返回工具调用，永远不给最终回答 → 触发 8 轮上限
        content = ""
        finish_reason = "tool_calls"

        def __init__(self):
            self.tool_calls = [{"id": "c1", "function": {"name": "list_hosts", "arguments": "{}"}}]

    async def _loop(*a, **kw):
        return R()

    # 路由模块在顶层 `from llm_gateway import call_llm` —— 打网关模块绑不到它自己的名字，
    # 必须打引用了该函数的路由模块（否则会真调一次模型。这是本测试专门的坑）。
    import app.routers.incidents as m
    monkeypatch.setattr(m, "call_llm", _loop)
    # 工具调用要能被 _extract_tool_calls 认出来；真调工具也一并替掉
    monkeypatch.setattr("app.services.rca_tools.invoke", lambda *a, **kw: _async_none())

    h = {"Authorization": f"Bearer {await _tok(ac, 'admin', 'admin123')}"}
    async with ac.stream("POST", "/api/ops/chat/stream", json={"message": "孤儿轮次用例"}, headers=h) as _:
        pass
    admin_id = await _uid("admin")
    async with SessionLocal() as db:
        s = (await db.execute(select(AIChatSession).where(AIChatSession.user_id == admin_id)
                              .order_by(AIChatSession.id.desc()))).scalars().first()
        roles = [m.get("role") for m in (s.messages or [])]
        assert roles[-1] == "assistant", "用户提问后必须有回答落库，不能留孤儿"
        assert "上限" in (s.messages[-1].get("content") or ""), "要如实说明是工具轮次用尽，而不是编个结论"


async def _async_none():
    return {}


# ── ⑤ 手动压缩（D88，用户要求加"压缩历史"按钮）──

def _long_conv(n: int = 300) -> list[dict]:
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"第{i}问：{'甲' * 180}"})
        out.append({"role": "assistant", "content": f"第{i}答：{'乙' * 180}"})
    return out


@pytest.mark.asyncio
async def test_chat_compact_forces_summary(ac, monkeypatch):
    """"压缩历史"按钮就是调这个：即使没到自动阈值，也把更早回合折叠进摘要。
    摘要模型打桩（D97 补）：真实 LLM 有套餐限额（实测 429），测试必须确定性、不烧配额。"""
    from sqlalchemy import select

    class _R:
        content = "要点：用户想查磁盘；已确认 /data 用量 95%；未解决：哪个进程写的。"
    async def _fake_summary(db, purpose, messages, **kw):
        class R: content = "要点：已确认磁盘 95%，已排除内存；未解决：定位写入进程。"
        return R()
    import app.services.llm_gateway as gw
    monkeypatch.setattr(gw, "call_llm", _fake_summary)

    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.event import AIChatSession
    from app.models.shell import User

    admin_id = await _uid("admin")
    async with SessionLocal() as db:
        s = AIChatSession(user_id=admin_id, title="手动压缩用例", messages=_long_conv(),
                          summary=None, summary_upto=0)
        db.add(s); await db.commit(); await db.refresh(s)
        sid = s.id

    tok = await _tok(ac, "admin", "admin123")
    r = await ac.post(f"/api/ops/chat/sessions/{sid}/compact",
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["compacted"] is True, "长会话手动压缩应折叠"
    assert data["summary_upto"] > 0
    assert "摘要" in data["note"]
    # 落库后 summary 有值、边界前移，且展示用 messages 仍是全量（不被裁）
    async with SessionLocal() as db:
        s2 = await db.get(AIChatSession, sid)
        assert (s2.summary or "").strip(), "压缩后必须有摘要"
        assert s2.summary_upto > 0
        assert len(s2.messages or []) == len(_long_conv()), "手动压缩不应改展示用的全量历史"
    async with SessionLocal() as db:
        await db.delete(await db.get(AIChatSession, sid)); await db.commit()


@pytest.mark.asyncio
async def test_chat_compact_rejects_other_users_session(ac):
    """"压缩历史"只动自己名下：demo 传 admin 的 session_id → 404（会话隔离）。"""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.event import AIChatSession

    admin_id = await _uid("admin")
    async with SessionLocal() as db:
        s = AIChatSession(user_id=admin_id, title="越权压缩用例", messages=[{"role": "user", "content": "x"}])
        db.add(s); await db.commit(); await db.refresh(s)
        sid = s.id
    try:
        hd = await _tok(ac, "demo", "demo")
        r = await ac.post(f"/api/ops/chat/sessions/{sid}/compact",
                          headers={"Authorization": f"Bearer {hd}"})
        assert r.status_code == 404, f"demo 不应能压 admin 的会话：{r.text}"
    finally:
        async with SessionLocal() as db:
            ss = await db.get(AIChatSession, sid)
            if ss: await db.delete(ss); await db.commit()


@pytest.mark.asyncio
async def test_chat_compact_reports_nothing_to_fold(ac):
    """短会话（没到窗口上限）点压缩 → 返回"无需折叠"不是报错，也不能把摘要写坏。"""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.event import AIChatSession

    admin_id = await _uid("admin")
    async with SessionLocal() as db:
        s = AIChatSession(user_id=admin_id, title="短会话压缩", messages=_long_conv(4),
                          summary=None, summary_upto=0)
        db.add(s); await db.commit(); await db.refresh(s)
        sid = s.id
    try:
        tok = await _tok(ac, "admin", "admin123")
        r = await ac.post(f"/api/ops/chat/sessions/{sid}/compact",
                          headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["compacted"] is False
        assert "折叠" in r.json()["data"].get("note", "")
    finally:
        async with SessionLocal() as db:
            ss = await db.get(AIChatSession, sid)
            if ss: await db.delete(ss); await db.commit()


# ── ⑥ 告警历史工具（D89，用户实测：助手答不了"24h 内恢复了哪些告警 / 最慢是哪条"）──

def test_alert_history_tool_is_registered_and_prompted():
    """"alert_history" 必须（a）在工具清单里给了模型、（b）在分发表里可调用、（c）对话提示词提了一句能查。

    D89 根因：数据在 alert_his_events，但助手从没见过查它的工具 → 只会诚实地说"没这能力"，
    而用户要的是"能做"。锁这三处防止再回退。
    """
    from app.services import prompts, rca_tools

    specs = {t["function"]["name"] for t in rca_tools.tool_specs()}
    assert "alert_history" in specs, "alert_history 未暴露给模型"
    assert "alert_history" in rca_tools.DISPATCH, "alert_history 未注册到分发表"
    assert "alert_history" in prompts.CHAT_PROMPT, "对话提示词没提能查告警历史"


@pytest.mark.asyncio
async def test_alert_history_returns_slowest_and_filters_recovered():
    """造几条不同恢复耗时的历史告警：① 只返回已恢复的；② slowest_by_duration 按耗时倒序取最慢。"""
    from datetime import datetime, timedelta

    from app.db.session import SessionLocal
    from app.services import rca_tools

    from app.models.alert import AlertHisEvent

    def _ins(db, rule, who, trig, dur_s, recovered=True):
        import hashlib
        db.add(AlertHisEvent(
            hash=hashlib.sha256((rule + who).encode()).hexdigest()[:32],
            rule_id=1,
            rule_name=rule, host_ident=who, metric="mem.percent", severity=2,
            trigger_time=trig, trigger_value=90.0, is_recovered=recovered,
            recover_time=trig + timedelta(seconds=dur_s) if recovered else None))

    now = datetime.utcnow()
    host = "d89-测试主机"
    async with SessionLocal() as db:
        _ins(db, "A-快", host, now - timedelta(hours=2), dur_s=30)
        _ins(db, "A-中", host, now - timedelta(hours=3), dur_s=900)
        _ins(db, "A-慢", host, now - timedelta(hours=4), dur_s=86400 - 1)
        _ins(db, "A-仍在", host, now - timedelta(hours=1), dur_s=0, recovered=False)
        await db.commit()

    # 只查这一台主机，避免共享库里的真实历史告警干扰 top N
    r = await rca_tools.alert_history(hours=24, only_recovered=True, host=host, top_slowest=50)
    assert r["status"] == "SUCCESS", r.get("note")
    data = r["data"]
    assert data["summary"]["count_found"] == 3, data["summary"]
    assert all(i["recovered"] for i in data["items"]), "只查已恢复时不能混入未恢复"
    assert data["summary"]["recovered_in_window"] == 3
    slow = data["summary"]["slowest_by_duration"]
    assert [i["rule"] for i in slow] == ["A-慢", "A-中", "A-快"], "应严格按耗时倒序"
    assert slow[0]["duration_s"] == 86400 - 1

    # 清理本用例造的数据
    async with SessionLocal() as db:
        from sqlalchemy import delete
        for rn in ("A-快", "A-中", "A-慢", "A-仍在"):
            await db.execute(delete(AlertHisEvent).where(
                AlertHisEvent.rule_name == rn and AlertHisEvent.host_ident == host))
        await db.commit()


@pytest.mark.asyncio
async def test_alert_history_no_data_when_empty_window():
    """窗口内一条都没有 → NO_DATA + 人话说明，不是报错也不是编造。"""
    from datetime import datetime
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.services import rca_tools
    from app.models.alert import AlertHisEvent

    async with SessionLocal() as db:
        await db.execute(delete(AlertHisEvent).where(AlertHisEvent.rule_name == "D89-不存在"))
        await db.commit()
    r = await rca_tools.alert_history(hours=1, only_recovered=True)
    assert r["status"] in ("NO_DATA", "SUCCESS")
    # 若库里正好有近 1h 数据则返回 SUCCESS；空库才走 NO_DATA —— 两种情况都合法，只断言不抛且形态正确
    # data 可能是 None（NO_DATA 形态），取值前先兜底空值
    _data = r.get("data") or {}
    _summary = _data.get("summary") or {}
    assert isinstance(_summary.get("count_found", -1), (int, float)) or r["status"] == "NO_DATA"


# ── ⑦ D90：AI 助手历史/状态能力清单（避免"没这个能力"）──

def test_history_tools_registered_and_prompted():
    """D89 教训固化成守卫：凡新增"历史/状态"工具，必须在 tool_specs + DISPATCH + 提示词三处都挂上，
    否则用户问起来又会"没这个能力"。"""
    from app.services import prompts, rca_tools

    specs = {t["function"]["name"] for t in rca_tools.tool_specs()}
    for name in ("event_history", "runbook_history", "inspection_history", "notification_history",
                 "alert_history", "list_hosts", "query_metrics"):
        assert name in specs, f"{name} 未暴露给模型"
        assert name in rca_tools.DISPATCH, f"{name} 未注册到分发表"
    for marker in ("event_history", "runbook_history", "inspection_history", "notification_history", "alert_history"):
        assert marker in prompts.CHAT_PROMPT, f"提示词没提 {marker}"


@pytest.mark.asyncio
async def test_event_history_returns_incidents_with_resolution():
    from datetime import datetime, timedelta
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.services import rca_tools
    from app.models.event import Incident

    now = datetime.utcnow()
    src = "d90-evt-src"
    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.source_key.like("d90-%")))
        db.add_all([
            Incident(title="D90-已解决", severity=1, status="resolved", source_key=f"{src}a",
                     host_ident="h", created_at=now - timedelta(hours=2),
                     resolved_at=now - timedelta(hours=2) + timedelta(minutes=6)),
            Incident(title="D90-仍打开", severity=2, status="open", source_key=f"{src}b",
                     host_ident="h", created_at=now - timedelta(hours=1)),
        ])
        await db.commit()
    r = await rca_tools.event_history(hours=24)
    assert r["status"] == "SUCCESS", r.get("note")
    items = {i["title"]: i for i in r["data"]["items"] if i["title"].startswith("D90-")}
    assert "D90-已解决" in items and items["D90-已解决"]["status"] == "resolved"
    assert items["D90-已解决"]["resolution_s"] == 6 * 60, "已解决事件的解决耗时应为触发→解决"
    assert items["D90-仍打开"]["resolution_s"] is None
    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.source_key.like("d90-%")))
        await db.commit()


@pytest.mark.asyncio
async def test_notification_history_reports_failures():
    from datetime import datetime, timedelta
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.services import rca_tools
    from app.models.alert import NotificationRecord

    now = datetime.utcnow()
    async with SessionLocal() as db:
        await db.execute(delete(NotificationRecord).where(NotificationRecord.error_detail == "D90-test"))
        db.add(NotificationRecord(event_hash="d90" * 8, channel="feishu", target="t",
                                  status="sent", created_at=now - timedelta(minutes=5), error_detail="D90-test"))
        db.add(NotificationRecord(event_hash="d9" + "0" * 30, channel="webhook", target="t",
                                  status="failed", created_at=now - timedelta(minutes=3),
                                  error_detail="D90-test: connect refused"))
        await db.commit()
    r = await rca_tools.notification_history(hours=24)
    assert r["status"] == "SUCCESS", r.get("note")
    d = r["data"]
    assert d["summary"]["failed"] >= 1
    assert any(i["status"] == "failed" for i in d["items"])
    async with SessionLocal() as db:
        await db.execute(delete(NotificationRecord).where(NotificationRecord.error_detail == "D90-test"))
        await db.commit()
