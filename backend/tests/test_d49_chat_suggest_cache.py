"""D49：AI 助手建议问题「定时预生成 + 缓存 + 手动刷新」。

用户诉求（原话要点）：每次点对话框才生成会有延迟，慢就看不到问题 → 后端定时每小时生成
（北京时间 23:00–08:00 每 3 小时），界面加刷新按钮，点击立即更新。

本文件锁住 5 条契约：
① 夜间窗口：白天每小时跑；北京 23/02/05 点跑、其余夜间整点跳过；
② 状态指纹不变 → 不调模型（省 token）；状态变了 → 重新生成；
③ GET 只读缓存/规则版，**绝不**同步调模型（打开对话框必须零等待）；
④ 缓存为空先给规则版；手动刷新后 GET 能读到模型版，并带生成时间；
⑤ 手动刷新冷却（30s 内不重复烧 token）。
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from app.db.session import SessionLocal
from app.services import chat_suggest as cs

_RULE = [{"q": "规则问题一", "why": "w", "tone": "info"},
         {"q": "规则问题二", "why": "w", "tone": "warn"},
         {"q": "规则问题三", "why": "w", "tone": "ok"}]
_MODEL = [{"q": "模型问题一", "why": "m", "tone": "urgent"},
          {"q": "模型问题二", "why": "m", "tone": "warn"},
          {"q": "模型问题三", "why": "m", "tone": "info"}]


def _reset() -> None:
    cs._CACHE.clear()
    cs._LAST_BG = 0.0
    cs._LAST_MANUAL = 0.0


def test_should_tick_night_window():
    """白天每小时；北京 23:00–08:00 只跑 23/02/05 点。"""
    for h_utc in range(24):
        h_cn = (h_utc + 8) % 24
        expect = (8 <= h_cn <= 22) or (h_cn % 3 == 2)
        got = cs.should_tick(datetime(2026, 10, 13, h_utc, 7))
        assert got == expect, f"UTC {h_utc} 点（北京 {h_cn} 点）：期望 {expect} 实际 {got}"
    # 关键点写死，防止公式被"顺手改坏"后测试仍自洽
    assert cs.should_tick(datetime(2026, 10, 13, 15, 7)) is True     # 北京 23:00 → 跑
    assert cs.should_tick(datetime(2026, 10, 13, 16, 7)) is False    # 北京 00:00 → 跳
    assert cs.should_tick(datetime(2026, 10, 13, 18, 7)) is True     # 北京 02:00 → 跑
    assert cs.should_tick(datetime(2026, 10, 13, 21, 7)) is True     # 北京 05:00 → 跑
    assert cs.should_tick(datetime(2026, 10, 13, 23, 7)) is False    # 北京 07:00 → 跳
    assert cs.should_tick(datetime(2026, 10, 14, 0, 7)) is True      # 北京 08:00 → 跑


@pytest.mark.asyncio
async def test_scheduler_skips_unchanged_state(monkeypatch):
    """指纹不变 → 跳过（省 token）；状态变化 → 重新生成；夜间非打点 → 直接跳过。"""
    _reset()
    facts = ["有 2 条 S1 紧急告警未确认（另有 S2 0 条）"]
    calls: list[int] = []

    async def fake_collect(db, user):
        return list(facts), list(_RULE), "*"

    async def fake_model(db, f, quota_role="engine"):
        calls.append(1)
        return list(_MODEL), "模型版标题"

    monkeypatch.setattr(cs, "collect_state", fake_collect)
    monkeypatch.setattr(cs, "model_suggestions", fake_model)
    day = datetime(2026, 10, 13, 2, 7)      # 北京 10:07
    night = datetime(2026, 10, 13, 17, 7)   # 北京 01:07

    r1 = await cs.scheduler_tick(day)
    assert r1["skipped"] is False and len(calls) == 1, r1
    assert cs._CACHE["*"]["items"][0]["q"] == "模型问题一", "结果必须写入缓存"
    r2 = await cs.scheduler_tick(day)
    assert r2["skipped"] is True and r2["reason"] == "state-unchanged" and len(calls) == 1, r2
    facts.append("纳管 3 台主机，其中 1 台离线：demo-web-01")     # 状态变了
    r3 = await cs.scheduler_tick(day)
    assert r3["skipped"] is False and len(calls) == 2, "状态变化后必须重新生成"
    r4 = await cs.scheduler_tick(night)
    assert r4["skipped"] is True and r4["reason"] == "night-window" and len(calls) == 2, \
        "夜间非打点不得调模型"


@pytest.mark.asyncio
async def test_get_never_calls_model(monkeypatch):
    """GET 绝不同步调模型——它是"打开对话框等十几秒"的根因（用户明确不接受）。"""
    _reset()

    async def boom(*a, **k):
        raise AssertionError("GET 建议接口不允许同步调用大模型（D49）")

    monkeypatch.setattr(cs, "model_suggestions", boom)
    async with SessionLocal() as db:
        d = await cs.snapshot(db, None)      # 真实收集状态（user=None = 调度器视角）
    assert d["source"] == "rule", d
    assert d["suggestions"], "规则版也必须给出建议（界面不能空）"
    assert d["generated_at"] == "", "规则版没有生成时间，不能假装是模型生成"
    for x in d["suggestions"]:
        assert x.get("q") and x.get("tone") in ("urgent", "warn", "info", "ok"), x


@pytest.mark.asyncio
async def test_refresh_updates_cache_and_snapshot_reads_it(monkeypatch):
    """手动刷新 → 缓存写入模型版 → GET 命中缓存（带生成时间）；冷却期内不重复调模型。"""
    _reset()

    async def fake_collect(db, user):
        return ["固定事实"], list(_RULE), "*"

    async def fake_model(db, f, quota_role="engine"):
        return list(_MODEL), "模型版标题"

    monkeypatch.setattr(cs, "collect_state", fake_collect)
    monkeypatch.setattr(cs, "model_suggestions", fake_model)
    async with SessionLocal() as db:
        before = await cs.snapshot(db, None)
        assert before["source"] == "rule" and before["headline"] == cs.headline_of(_RULE)
        after = await cs.refresh_manual(db, None)
        assert after["source"] == "model" and after["suggestions"][0]["q"] == "模型问题一", after
        assert after.get("cooldown") is None
        again = await cs.snapshot(db, None)
        assert again["source"] == "cache" and again["suggestions"][0]["q"] == "模型问题一", again
        assert again["generated_at"], "缓存版必须带生成时间（界面显示「更新于 HH:MM」）"
        # 冷却：立刻再点刷新 → 直接用当前快照，不重复烧 token
        calls: list[int] = []

        async def fake2(db, f, quota_role="engine"):
            calls.append(1)
            return list(_MODEL), "x"

        monkeypatch.setattr(cs, "model_suggestions", fake2)
        cool = await cs.refresh_manual(db, None)
        assert cool.get("cooldown") is True and not calls, "冷却期内不得重复调模型"
        cs._LAST_MANUAL = time.time() - cs.MANUAL_COOLDOWN_S - 1      # 冷却结束
        fresh = await cs.refresh_manual(db, None)
        assert fresh.get("cooldown") is None and len(calls) == 1, "冷却结束后应能再次强制生成"


@pytest.mark.asyncio
async def test_visibility_scoped_cache(monkeypatch):
    """可见域不同 → 缓存分开（demo 前缀隔离配置下不能把别人的主机名串到他的问题里）。"""
    _reset()
    seen: list[str] = []

    async def fake_collect(db, user):
        key = "*" if user is None else getattr(user, "username", "*")
        return [f"可见域 {key}"], list(_RULE), key

    async def fake_model(db, f, quota_role="engine"):
        seen.append(f[0])
        return list(_MODEL), "h"

    monkeypatch.setattr(cs, "collect_state", fake_collect)
    monkeypatch.setattr(cs, "model_suggestions", fake_model)
    async with SessionLocal() as db:
        await cs.refresh(db, None)
        assert cs._CACHE["*"]["sig"]
        await cs.refresh(db, None)                     # 同一可见域：仍会生成（refresh 是强制的）
        assert len(seen) == 2
        assert "demo" not in cs._CACHE, "未见过的可见域不应有缓存（会串数据）"


@pytest.mark.asyncio
async def test_endpoint_contract(monkeypatch):
    """接口契约：GET 即时返回并带 source；POST refresh 返回新列表（真调模型路径用假模型替换）。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.core.security import get_current_user
    from app.main import create_app
    from app.models.shell import User

    _reset()

    async def fake_model(db, f, quota_role="engine"):
        return list(_MODEL), "模型版标题"

    monkeypatch.setattr(cs, "model_suggestions", fake_model)
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "admin"))).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: u
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            t0 = time.time()
            r = await ac.get("/api/ops/chat/suggestions")
            assert r.status_code == 200, r.text[:200]
            d = r.json()["data"]
            assert set(d) >= {"headline", "suggestions", "source", "generated_at"}
            assert d["source"] in ("rule", "cache"), d["source"]
            assert time.time() - t0 < 2.0, "GET 必须即时返回（读缓存/规则版，不等模型）"
            r = await ac.post("/api/ops/chat/suggestions/refresh")
            assert r.status_code == 200, r.text[:200]
            d2 = r.json()["data"]
            assert d2["source"] == "model" and d2["suggestions"][0]["q"] == "模型问题一", d2
            r = await ac.get("/api/ops/chat/suggestions")
            assert r.json()["data"]["source"] == "cache", "刷新后 GET 应命中缓存"
    finally:
        app.dependency_overrides.clear()
        _reset()
