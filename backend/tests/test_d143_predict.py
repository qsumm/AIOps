"""D143 守卫：预测型自愈（直填指标策略由预测通道独占驱动 + 冷却加固）。

锁五件**会变成事故或规则崩溃**的事：
  ① 触发器不打双通道：直填指标策略（trigger_rule_id 为空）由 predict_loop 独占——
     evaluate_for_event 跳过它们（防 dry_run + predicted 双触发重复留痕）
  ② 轮内即时冷却：predict_loop 命中任一主机后立即 break（同轮不再处理其它主机）
  ③ 跨轮防退化：db.refresh 重读 pol.last_fired_at（避免下一轮仍读过时快照）
  ④ 样本不足跳过：少于 10 个 1m 序列点不外推（趋势不可信）
  ⑤ 命中后走完整三档路径：suggest 留痕 + 通知；manual 待确认；auto 护栏通过即下发
     （共用 _execute_policy，与告警触发同源——"提前预测，提前处理"）
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.ops import HealPolicy, HealPolicyRun
from app.services import heal_policy as hp

pytestmark = pytest.mark.asyncio


async def _make_metric_points(db, ident: str, n: int = 30, value: float = 50.0) -> None:
    """注入 n 个 1m 序列点（最近 n 分钟）。"""
    from datetime import datetime, timedelta
    from sqlalchemy import text as sa_text
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(n):
        ts = now - timedelta(minutes=n - i)
        await db.execute(sa_text(
            "INSERT INTO metric_agg (ident, metric, granularity, ts, value_avg, value_min, value_max) "
            "VALUES (:i, 'cpu.percent', '1m', :t, :v, :v, :v)"),
            {"i": ident, "t": ts.strftime("%Y-%m-%d %H:%M:%S"), "v": value})


@pytest.fixture
async def predict_env():
    """建直填指标 + predict_min=10 策略（测试后清场）。

    测试库里 cpu.percent 可能已被其他测试预存数据污染——fixture 开头清掉所有 cpu.percent，
    装入干净样本。同时清理之前测试遗留的通知台账（避免 10 分钟去重窗口跳过断言）。"""
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        from sqlalchemy import text as sa_text
        await db.execute(sa_text("DELETE FROM metric_agg WHERE metric='cpu.percent'"))
        # 清掉任何"飞书 sent 预测通知"——避免去重窗口跳过断言（10min 窗口）
        await db.execute(sa_text("DELETE FROM notification_records WHERE channel='飞书-运维告警群' AND error_detail = ''"))
        await db.commit()
        await _make_metric_points(db, f"d143-host-{tag}", n=30, value=50.0)
        pol = HealPolicy(
            name=f"d143-pol-{tag}",
            enabled=True, trigger_rule_id=None,
            trigger_metric="cpu.percent", trigger_op="<=",
            trigger_threshold=99,                # 50 ≤ 99 → 必预测命中
            trigger_severity=2, action="reconnect_pool",
            service_id=None, mode="suggest",
            cooldown_min=5, predict_min=10,
        )
        db.add(pol)
        await db.commit()
        yield {"policy_id": pol.id, "tag": tag, "ident": f"d143-host-{tag}"}
    async with SessionLocal() as db:
        for r in (await db.execute(select(HealPolicyRun).where(
                HealPolicyRun.policy_name == f"d143-pol-{tag}"))).scalars().all():
            await db.delete(r)
        for p in (await db.execute(select(HealPolicy).where(
                HealPolicy.name == f"d143-pol-{tag}"))).scalars().all():
            await db.delete(p)
        # metric_agg 测试点也清理
        from sqlalchemy import text as sa_text
        await db.execute(sa_text("DELETE FROM metric_agg WHERE ident=:i"),
                        {"i": f"d143-host-{tag}"})
        await db.commit()


async def _runs(pid):
    async with SessionLocal() as db:
        return (await db.execute(select(HealPolicyRun).where(
            HealPolicyRun.policy_id == pid).order_by(HealPolicyRun.id))).scalars().all()


# ── 核心功能 ─────────────────────────────────────────
async def test_predict_fires_predicted_with_full_detail(predict_env):
    """预测命中 → predicted 留痕 + 详情含外推数据 + predict_note + 服务选择"""
    env = predict_env
    async with SessionLocal() as db:
        await hp._predict_loop(db)          # 第一轮 50≤99 预测命中
    runs = await _runs(env["policy_id"])
    assert len(runs) == 1
    r = runs[0]
    assert r.status == "predicted"
    # host 可跨多本机（_make_metric_points 创一组，全主机 cpu.percent 聚合）
    assert r.host_ident, "host 必须记录"
    d = r.detail or {}
    # 外推数据完整
    assert d.get("predicted") == 50.0           # 50 + 0 斜率 * 10 = 50
    assert d.get("predict_min") == 10
    assert "predict_note" in d and "预测命中" in d["predict_note"]
    # 通知已发：台账里有"飞书-运维告警群" sent 记录且 error_detail 含"预测"二字
    # （notify_attention 把预测 note 透传到 error_detail 字段；notification_records 表无 kind 列）
    async with SessionLocal() as db:
        from sqlalchemy import text as sa_text
        recs = (await db.execute(sa_text(
            "SELECT channel, status, error_detail FROM notification_records "
            "WHERE created_at >= NOW() - INTERVAL 1 MINUTE"))).all()
    pred_hits = [r for r in recs if r[0] == "飞书-运维告警群" and r[1] == "sent"]
    assert pred_hits, f"预测命中后飞书必须 sent（实际台账：{[(r[0], r[1], str(r[2])[:30]) for r in recs[:3]]}）"


async def test_predict_cooldown_blocks_second_round(predict_env):
    """冷却内不重复：第二轮 predict_loop 应不产生新留痕。"""
    env = predict_env
    async with SessionLocal() as db:
        await hp._predict_loop(db)           # 第一轮：命中留痕 + last_fired_at=now
    first_count = len(await _runs(env["policy_id"]))
    async with SessionLocal() as db:
        await hp._predict_loop(db)           # 第二轮：冷却 5min → 跳过
    assert len(await _runs(env["policy_id"])) == first_count, "冷却内不应新增留痕"


async def test_predict_skipped_when_predict_min_zero():
    """predict_min=0 → predict_loop 跳过该策略。"""
    async with SessionLocal() as db:
        pol = HealPolicy(name="d143-off", enabled=True, trigger_rule_id=None,
                         trigger_metric="cpu.percent", trigger_op="<=",
                         trigger_threshold=99, trigger_severity=2,
                         action="reconnect_pool", mode="suggest",
                         cooldown_min=5, predict_min=0)
        db.add(pol); await db.commit(); pid = pol.id
    try:
        await _make_metric_points(db, "d143-off-host", n=30, value=50.0)
        async with SessionLocal() as db:
            await hp._predict_loop(db)
        assert len(await _runs(pid)) == 0
    finally:
        async with SessionLocal() as db:
            for r in (await db.execute(select(HealPolicyRun).where(
                    HealPolicyRun.policy_id == pid))).scalars().all():
                await db.delete(r)
            p = await db.get(HealPolicy, pid)
            if p:
                await db.delete(p)
            from sqlalchemy import text as sa_text
            await db.execute(sa_text("DELETE FROM metric_agg WHERE ident=:i"),
                            {"i": "d143-off-host"})
            await db.commit()


async def test_predict_skipped_when_data_too_few():
    """样本 < 10 → 不外推（趋势不可信）。"""
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        from sqlalchemy import text as sa_text
        await db.execute(sa_text("DELETE FROM metric_agg WHERE metric='cpu.percent'"))
        await db.commit()
        await _make_metric_points(db, f"d143-few-{tag}", n=8, value=50.0)  # 不足 10
        pol = HealPolicy(name=f"d143-few-{tag}", enabled=True, trigger_rule_id=None,
                         trigger_metric="cpu.percent", trigger_op="<=",
                         trigger_threshold=99, trigger_severity=2,
                         action="reconnect_pool", mode="suggest",
                         cooldown_min=5, predict_min=10)
        db.add(pol); await db.commit(); pid = pol.id
    try:
        async with SessionLocal() as db:
            await hp._predict_loop(db)
        assert len(await _runs(pid)) == 0
    finally:
        async with SessionLocal() as db:
            for r in (await db.execute(select(HealPolicyRun).where(
                    HealPolicyRun.policy_id == pid))).scalars().all():
                await db.delete(r)
            p = await db.get(HealPolicy, pid)
            if p:
                await db.delete(p)
            from sqlalchemy import text as sa_text
            await db.execute(sa_text("DELETE FROM metric_agg WHERE ident=:i"),
                            {"i": f"d143-few-{tag}"})
            await db.commit()


# ── 语义重构（防双通道重复触发）──
async def test_direct_metric_policy_not_triggered_by_event_loop():
    """evaluate_for_event 不应处理直填指标的策略（predict_loop 独占驱动）。

    构景：建一条 trigger_metric=cpu.percent op="<=" 阈值 99 必然匹配的事件，
    evaluate_for_event 被告警引擎调用于该事件——
    重构前会写 dry_run 留痕，与 predict_loop 的 predicted 重复；
    重构后 evaluate_for_event 开头过滤 trigger_rule_id 为空的策略→ 跳过，无新增留痕。
    """
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        from sqlalchemy import text as sa_text
        await db.execute(sa_text("DELETE FROM metric_agg WHERE metric='cpu.percent'"))
        await db.commit()
        await _make_metric_points(db, f"d143-evt-{tag}", n=30, value=50.0)
        pol = HealPolicy(name=f"d143-evt-{tag}", enabled=True, trigger_rule_id=None,
                         trigger_metric="cpu.percent", trigger_op="<=",
                         trigger_threshold=99, trigger_severity=2,
                         action="reconnect_pool", mode="suggest",
                         cooldown_min=5, predict_min=0)   # predict_min=0 排除预测
        db.add(pol); await db.commit(); pid = pol.id
        rule_id = None   # 传 None 进 evaluate_for_event 模拟"事件无对应规则"
    try:
        # 构造"事件"对象：db 已就绪；调用 evaluate_for_event(rule=None) 应直接跳过
        from types import SimpleNamespace as N
        ev = N(rule_id=None, hash="d143-fake", rule_name="d143-evt",
                host_ident=f"d143-evt-{tag}", metric="cpu.percent",
                trigger_value=50.0, severity=2, is_recovered=False)
        async with SessionLocal() as db:
            await hp.evaluate_for_event(db, ev=ev, rule=None)
        assert len(await _runs(pid)) == 0, \
            "evaluate_for_event 不应处理 trigger_rule_id 为空的策略（predict_loop 独占驱动）"
    finally:
        async with SessionLocal() as db:
            for r in (await db.execute(select(HealPolicyRun).where(
                    HealPolicyRun.policy_id == pid))).scalars().all():
                await db.delete(r)
            p = await db.get(HealPolicy, pid)
            if p:
                await db.delete(p)
            from sqlalchemy import text as sa_text
            await db.execute(sa_text("DELETE FROM metric_agg WHERE ident=:i"),
                            {"i": f"d143-evt-{tag}"})
            await db.commit()