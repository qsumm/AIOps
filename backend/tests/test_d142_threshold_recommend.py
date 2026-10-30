"""D142/D143 守卫：AI 阈值推荐 + 预测型自愈。

D142（用户原话：「阈值都是我看历史数据拍脑门定的阈值，其实可以用AI推测出一个比较合适的」）：
  推荐必须是**可解释的统计推断**（分位数 + 趋势），不是黑盒——运维要敢把推荐值用于自动自愈。
  锁：样本不足必须明确说"不行"（宁可不推荐也不给拍脑袋的数）、方向语义（>= 上尾 / <= 下尾）、
  推荐值必须落在历史区间内（不超过峰值——超过峰值意味着"从未发生过"）。

D143（用户原话：「目前这些都是出问题后的补救措施，那是不是可以用AI提前预测，提前处理？」）：
  第一版刻意保守：**只通知不动手**（预测有误报，自动动作跑在误报上比错过真故障更危险）。
  锁：趋势外推命中 → predicted 留痕 + 全渠道通知；未命中 → 静默；冷却防每分钟重复喊；
  挂规则的策略不做预测（规则的告警自己会触发）；样本不足不外推。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.host import Host
from app.models.ops import HealPolicy, HealPolicyRun
from app.services import heal_policy as hp

pytestmark = pytest.mark.asyncio


# ── D142 阈值推荐 ────────────────────────────────────────
async def test_recommend_insufficient_samples_is_honest():
    """指标没有历史数据 → 明确返回"不行"，不给拍脑袋的数。"""
    async with SessionLocal() as db:
        out = await hp.recommend_threshold(db, metric="no.such.metric.d142", op=">=", days=7)
    assert out["ok"] is False
    assert "数据点" in out["reason"]


async def test_recommend_uses_real_history_and_stays_within_peak():
    """有历史的指标：推荐值必须落在 (历史正常区间, 峰值] 内——超过峰值=「从未发生过」的数。

    D144 补强：改用**自播种数据**（独立指标名 + 固定取值曲线）。
    原实现依赖测试库恰好在 7 天窗口内有 ≥60 个真实点——演示库数据随时间老化后
    （如今天只有 36 点）该用例假红，环境依赖不属于本守卫的断言对象。"""
    metric = f"d142.up.{__import__('uuid').uuid4().hex[:8]}"
    await _seed_metric(metric, [40, 42, 44, 46, 48, 50, 52, 54] * 15 + [90])  # 121 点：正态水位 + 单峰
    async with SessionLocal() as db:
        out = await hp.recommend_threshold(db, metric=metric, op=">=", days=7)
    assert out["ok"] is True
    assert out["samples"] >= 60
    # 分布感知断言：
    #   正常分布（峰值明显高于 p99）→ 推荐值压在峰值之下（触发 = 冲出历史区间）；
    #   极平分布（历史从未越线）→ 任何触发阈值都高于全部历史，此时推荐 p99*1.05
    #   并在依据里明说「历史波动极小」——比给一个永远不触发的数诚实。
    if out["peak"] > out["p99"] * 1.02:
        assert out["recommended"] <= out["peak"], (
            f"推荐阈值 {out['recommended']} 超过历史峰值 {out['peak']}（且峰值明显高于 p99）"
            "——超过峰值的数从未发生过，不能用于触发")
    else:
        assert out["recommended"] >= out["peak"], (
            "极平分布下推荐值应高于历史峰值并在依据中说明")
        assert "高位长驻" in out["rationale"], "极平分布的依据必须说明「高位长驻」语义"
    assert out["recommended"] >= out["p95"], (
        f"推荐阈值 {out['recommended']} 低于 p95 {out['p95']}——会天天误触发")
    assert "推荐触发阈值" in out["rationale"]


async def test_recommend_lower_direction_uses_lower_tail():
    """低于型（op=<=，如剩余空间）：推荐值必须落在 [谷底, p5) 内。（同样改自播种）"""
    metric = f"d142.free.{__import__('uuid').uuid4().hex[:8]}"
    await _seed_metric(metric, [70, 72, 74, 76, 78, 80] * 20 + [20])   # 121 点：正常水位 + 单谷
    async with SessionLocal() as db:
        out = await hp.recommend_threshold(db, metric=metric, op="<=", days=7)
    assert out["ok"] is True
    assert out["recommended"] >= out["trough"], "低于型推荐值不应低于历史谷底"


async def _seed_metric(metric: str, values: list[float]) -> None:
    """注入 1m 序列点（最近 len(values) 分钟，独立指标名与库中真实数据隔离）。"""
    from datetime import datetime, timedelta
    from sqlalchemy import text as sa_text
    ident = "d142-seed-host"
    now = datetime.utcnow().replace(microsecond=0)
    async with SessionLocal() as db:
        for i, v in enumerate(values):
            ts = now - timedelta(minutes=len(values) - i)
            await db.execute(sa_text(
                "INSERT INTO metric_agg (ident, metric, granularity, ts, value_avg, value_min, value_max) "
                "VALUES (:i, :m, '1m', :t, :v, :v, :v)"),
                {"i": ident, "m": metric, "t": ts.strftime("%Y-%m-%d %H:%M:%S"), "v": v})
        await db.commit()


# ── D143 预测型自愈 ──────────────────────────────────────
@pytest.fixture
async def predict_env():
    """造一条直填指标 + predict_min=10 的策略，用完清场。"""
    import uuid
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        pol = HealPolicy(name=f"d143-pol-{tag}", enabled=True,
                         trigger_rule_id=None, trigger_metric="cpu.percent",
                         trigger_op=">=", trigger_threshold=1,   # 低阈值 → 几乎必然预测命中
                         trigger_severity=2, action="reconnect_pool",
                         mode="suggest", cooldown_min=5, predict_min=10)
        db.add(pol)
        await db.commit()
        yield {"policy_id": pol.id, "tag": tag, "threshold": 1}
    async with SessionLocal() as db:
        for r in (await db.execute(select(HealPolicyRun).where(
                HealPolicyRun.policy_name == f"d143-pol-{tag}"))).scalars().all():
            await db.delete(r)
        for p in (await db.execute(select(HealPolicy).where(
                HealPolicy.name == f"d143-pol-{tag}"))).scalars().all():
            await db.delete(p)
        await db.commit()


async def test_predict_creates_predicted_run_and_cooldown(predict_env):
    """预测命中 → predicted 留痕 + 策略状态推进；冷却内第二次预测不重复。"""
    env = predict_env
    # 造新鲜数据（预测回看最近 30 分钟的 1m 点——测试库旧点不参与）
    from datetime import datetime, timedelta
    from sqlalchemy import text as sa_text
    ident_row = None
    async with SessionLocal() as db:
        row = (await db.execute(sa_text(
            "SELECT ident FROM metric_agg WHERE metric='cpu.percent' ORDER BY ts DESC LIMIT 1"))).first()
        ident = row[0] if row else "d143-host"
        now = datetime.utcnow()
        for i in range(30):
            ts = now - timedelta(minutes=30 - i)
            await db.execute(sa_text(
                "INSERT INTO metric_agg (ident, metric, granularity, ts, value_avg, value_min, value_max) "
                "VALUES (:i, 'cpu.percent', '1m', :t, 3.0, 2.8, 3.2)"),
                {"i": ident, "t": ts.strftime("%Y-%m-%d %H:%M:%S")})
        await db.commit()
    async with SessionLocal() as db:
        await hp._predict_loop(db)          # 第一轮：cpu.percent 恒定 3.0 > 阈值 1 → 外推 3.0 >= 1 → 预测命中
    runs = await _runs(env["policy_id"])
    assert len(runs) >= 1, "预测命中应留痕"
    assert runs[0].status == "predicted"
    assert "预测命中" in runs[0].guard_reason
    # 冷却：立即第二轮不重复
    async with SessionLocal() as db:
        await hp._predict_loop(db)
    runs2 = await _runs(env["policy_id"])
    assert len(runs2) == len(runs), "冷却期内不应重复预测留痕"


async def test_predict_skipped_for_rule_linked_policies():
    """挂规则的策略不做预测（规则告警自己会触发）——predict_loop 直接跳过。"""
    import uuid
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        pol = HealPolicy(name=f"d143-rule-{tag}", enabled=True, trigger_rule_id=1,
                         trigger_metric="", trigger_op=">=", trigger_threshold=99,
                         trigger_severity=2, action="restart_service", mode="suggest",
                         cooldown_min=5, predict_min=10)
        db.add(pol)
        await db.commit()
        pid = pol.id
    try:
        async with SessionLocal() as db:
            await hp._predict_loop(db)
        async with SessionLocal() as db:
            runs = (await db.execute(select(HealPolicyRun).where(
                HealPolicyRun.policy_id == pid))).scalars().all()
        assert len(runs) == 0, "挂规则的策略不应走预测（双通道重复）"
    finally:
        async with SessionLocal() as db:
            p = await db.get(HealPolicy, pid)
            if p:
                await db.delete(p)
                await db.commit()


async def _runs(pid):
    async with SessionLocal() as db:
        return (await db.execute(select(HealPolicyRun).where(
            HealPolicyRun.policy_id == pid).order_by(HealPolicyRun.id))).scalars().all()
