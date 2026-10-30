"""D21 回归：演示站 token 配额必须只算「demo 自己」的用量。

用户实测报障（2026-10-14）：demo 用户在新会话问「现在有哪些活跃告警？」→ 429
    `LLM_QUOTA_EXCEEDED：演示模式今日 token 已用尽（760070/200000）`

限额 20 万、却记了 76 万。查库得到当日构成：

| purpose | 次数 | token | 谁产生的 |
|---|---|---|---|
| rca     | 53   | 423,652 | 管理员做验收测试时的 AI 诊断 |
| summary | 593  | 280,993 | **后台引擎自动**生成的告警 AI 摘要（无任何用户触发） |
| chat    | 16   | 35,856  | 管理员的 ChatOps 测试 |
| inspect | 30   | 19,528  | 巡检（后台/测试） |
| embed   | 41   | 41      | 向量检索 |

**根因**：`tokens_used_today()` 统计的是**全平台当天总量**，不区分归属。
于是"演示站防刷额度"被引擎开销与管理员自己的测试吃光 —— demo 用户自己几乎没用却被拦住，
而且拦住时数字本身就不自洽（760070 > 200000 已超 3.8 倍）。

**修法**：`llm_usage` 增加 `role`（用户角色 / "engine" 后台任务）；
demo 口径只统计 `role="demo"`，全局限额仍统计全平台（那才是"平台成本上限"的本义）。
"""
import pytest
from sqlalchemy import delete, select, text

from app.db.session import SessionLocal
from app.models.llm import LLMUsage
from app.services import llm_gateway as G


@pytest.mark.asyncio
async def test_demo_usage_counts_only_demo_rows():
    """引擎/管理员产生的用量**不得**计入 demo 口径；demo 自己的用量必须计入。"""
    marker = "d21test"
    async with SessionLocal() as db:
        base_demo = await G.tokens_used_today(db, role="demo")
        base_all = await G.tokens_used_today(db)
        try:
            db.add_all([
                LLMUsage(purpose="summary", model=marker, role="engine", total_tokens=1000),
                LLMUsage(purpose="rca", model=marker, role="admin", total_tokens=2000),
                LLMUsage(purpose="inspect", model=marker, role="member", total_tokens=400),
            ])
            await db.commit()
            assert await G.tokens_used_today(db, role="demo") == base_demo, \
                "引擎/管理员/成员的用量被错算到 demo 头上"
            assert await G.tokens_used_today(db) == base_all + 3400, \
                "全平台口径应统计所有归属"

            db.add(LLMUsage(purpose="chat", model=marker, role="demo", total_tokens=500))
            await db.commit()
            assert await G.tokens_used_today(db, role="demo") == base_demo + 500, \
                "demo 自己的用量必须计入 demo 口径"
        finally:
            await db.execute(delete(LLMUsage).where(LLMUsage.model == marker))
            await db.commit()


@pytest.mark.asyncio
async def test_engine_usage_does_not_exhaust_demo_quota(monkeypatch):
    """引擎大量消耗不得把 demo 名额吃掉（本次报障的最小复现）。"""
    marker = "d21test2"
    async with SessionLocal() as db:
        monkeypatch.setattr(G.settings, "demo_daily_token_limit", 10, raising=False)
        try:
            # 引擎狂烧 100 万 token
            db.add(LLMUsage(purpose="summary", model=marker, role="engine", total_tokens=1_000_000))
            await db.commit()
            st = await G.quota_state(db, "demo")
            assert st["scope"] == "demo"
            assert st["used"] == await G.tokens_used_today(db, role="demo")
            # demo 自己没用量 → 不应被判定超限（修前这里会是 exceeded=True → 429）
            if st["used"] < st["limit"]:
                assert st["exceeded"] is False, "引擎用量不应导致 demo 配额超限"
                await G.enforce_quota(db, "demo")   # 不应抛
        finally:
            await db.execute(delete(LLMUsage).where(LLMUsage.model == marker))
            await db.commit()


@pytest.mark.asyncio
async def test_demo_quota_still_enforced_for_demo_own_usage(monkeypatch):
    """不放松：demo 自己用超了，仍然要 429（配额本身必须有效）。"""
    marker = "d21test3"
    async with SessionLocal() as db:
        monkeypatch.setattr(G.settings, "demo_daily_token_limit", 1, raising=False)
        try:
            db.add(LLMUsage(purpose="chat", model=marker, role="demo", total_tokens=999))
            await db.commit()
            st = await G.quota_state(db, "demo")
            assert st["exceeded"] is True
            with pytest.raises(Exception) as ei:
                await G.enforce_quota(db, "demo")
            assert "LLM_QUOTA_EXCEEDED" in str(getattr(ei.value, "detail", ei.value))
            assert "演示模式" in str(getattr(ei.value, "detail", ei.value))
        finally:
            await db.execute(delete(LLMUsage).where(LLMUsage.model == marker))
            await db.commit()


@pytest.mark.asyncio
async def test_global_scope_still_counts_everything(monkeypatch):
    """全局限额口径不变：它统计全平台（含 demo 与引擎）——这是"平台成本上限"的本义。"""
    marker = "d21test4"
    async with SessionLocal() as db:
        monkeypatch.setattr(G.settings, "llm_daily_token_limit", 1, raising=False)
        try:
            db.add(LLMUsage(purpose="summary", model=marker, role="engine", total_tokens=123))
            await db.commit()
            st = await G.quota_state(db, "")
            assert st["scope"] == "global"
            assert st["exceeded"] is True
            assert st["used"] >= await G.tokens_used_today(db, role="demo")
        finally:
            await db.execute(delete(LLMUsage).where(LLMUsage.model == marker))
            await db.commit()


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class _FakeMessage:
    content = "ok"
    tool_calls = None
    reasoning_content = ""


class _FakeChoice:
    message = _FakeMessage()
    finish_reason = "stop"


class _FakeResp:
    usage = _FakeUsage()
    choices = [_FakeChoice()]


class _FakeCompletions:
    async def create(self, **_kw):
        return _FakeResp()


class _FakeChat:
    completions = _FakeCompletions()


class _FakeClient:
    def __init__(self, **_kw):
        self.chat = _FakeChat()


class _FakeCfg:
    model = "fake-main"
    fast_model = ""
    base_url = ""
    extra_config = {}
    api_key_encrypted = ""


@pytest.mark.asyncio
async def test_call_llm_records_role_on_usage_row(monkeypatch):
    """落库必须带归属——否则配额永远只能按全平台算（这就是原缺陷的根因）。

    不真的调 LLM：把 openai 客户端与配置挑选替换成假实现，只验证**写入的归属**。
    """
    import openai

    from app.models.llm import LLMUsage as U

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)
    async def _fake_pick(*a, **k):
        return _FakeCfg()
    monkeypatch.setattr(G, "pick_config", _fake_pick)
    monkeypatch.setattr(G, "_api_key_of", lambda *a, **k: "fake")

    last_id = None
    async with SessionLocal() as db:
        try:
            for quota_role, expect in (("demo", "demo"), ("admin", "admin"), ("", "engine")):
                await G.call_llm(db, "chat", [{"role": "user", "content": "hi"}], quota_role=quota_role)
                row = (await db.execute(select(U).order_by(U.id.desc()).limit(1))).scalar_one()
                last_id = row.id
                assert row.role == expect, f"quota_role={quota_role!r} 应落库为 {expect!r}，实际 {row.role!r}"
        finally:
            if last_id:
                await db.execute(delete(U).where(U.model.in_(["fake-main"])))
                await db.commit()


@pytest.mark.asyncio
async def test_role_column_matches_migration_shape():
    """模型字段与迁移一致（防"加了模型没加迁移"或反过来）。"""
    from sqlalchemy import inspect as sa_inspect

    async with SessionLocal() as db:
        def _check(sync_session):
            insp = sa_inspect(sync_session.get_bind())
            cols = {c["name"]: c for c in insp.get_columns("llm_usage")}
            assert "role" in cols, "llm_usage 缺 role 列（迁移未执行？）"
            assert cols["role"]["type"].length == 16
        await db.run_sync(_check)

        # 索引不能用 SQLAlchemy 反射查：**SeekDB 不参与索引反射**（04 §12 已知陷阱，
        # 实测 get_indexes() 返回空并打印 "Unknown schema content: KEY ... BLOCK_SIZE"）。
        # 故用 raw SQL 验证索引确实建上了。
        names = {(r[1] if len(r) > 1 else "") for r in
                 (await db.execute(text("SHOW INDEX FROM llm_usage"))).all()}
        idx_names = {r[2] for r in (await db.execute(text("SHOW INDEX FROM llm_usage"))).all()}
        assert "ix_llm_usage_role" in idx_names, f"缺 role 索引，现有：{sorted(idx_names)}"
