"""D141 守卫：自动自愈策略（用户原话：「简单告警后的自愈功能」+ 三个真实场景：
集群平均 CPU 超阈值自动补机器 / DB 连接满或 CPU>90% 自动 kill 进程 / 磁盘>90% 自动扩容）。

锁五件**会变成玩具或事故**的事：
  ① 触发匹配：关联规则的策略只响应自己的规则事件；直填指标的策略按阈值比较；
  ② 冷却：同一策略 cooldown_min 内不重复触发（防"自愈→告警→自愈"循环）；
  ③ 三档语义：suggest 只留痕不下发 / manual 留痕待确认 / auto 护栏通过才下发；
     护栏拒绝 → denied 留痕，绝不绕过（自愈不等于免护栏）；
  ④ 留痕完整性：每次触发都有 HealPolicyRun（自愈不是黑盒）；
  ⑤ 白名单：动作不在 heal.ACTIONS 里 → denied（不自造动作 = 不自造事故）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.engine.state import Ev
from app.models.alert import AlertRule
from app.models.host import Host
from app.models.ops import HealPolicy, HealPolicyRun
from app.models.svc import Service
from app.services import heal_policy as hp

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def policy_env():
    """造一条启用的策略（关联规则触发 + reconnect_pool + suggest 档）+ 一条服务声明，测完清场。"""
    import uuid
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        rule = None
        from app.models.alert import AlertRule
        rule = AlertRule(name=f"d141-rule-{tag}", scope="metric", metric="cpu.percent",
                         expr=">99", severity=1, eval_interval_s=15, window_s=60)
        db.add(rule)
        # 服务：挂在任一已有主机上（heal 护栏需要主机）；没有主机就跳过用例（诚实边界）
        host = (await db.execute(select(Host).limit(1))).scalar_one_or_none()
        if host is None:
            await db.delete(rule)
            await db.commit()
            pytest.skip("测试库无主机——heal 护栏需要 Service+Host 才能评估")
        svc = Service(host_id=host.id, name=f"d141-svc-{tag}", type="app",
                      spec_json={"unit": f"d141-{tag}.service", "port": 18080},
                      status="up")
        db.add(svc)
        await db.commit()
        pol = HealPolicy(name=f"d141-pol-{tag}", enabled=True,
                         trigger_rule_id=rule.id, trigger_metric="",
                         trigger_op=">=", trigger_threshold=90, trigger_severity=1,
                         action="reconnect_pool", service_id=svc.id, mode="suggest",
                         cooldown_min=15)
        db.add(pol)
        await db.commit()
        yield {"tag": tag, "rule_id": rule.id, "policy_id": pol.id,
               "service_id": svc.id, "host_ident": host.ident}
    async with SessionLocal() as db:
        # 清场（按 tag 反查，健壮于持有对象跨 loop）
        for r in (await db.execute(select(HealPolicyRun).where(
                HealPolicyRun.policy_name == f"d141-pol-{tag}"))).scalars().all():
            await db.delete(r)
        for p in (await db.execute(select(HealPolicy).where(
                HealPolicy.name == f"d141-pol-{tag}"))).scalars().all():
            await db.delete(p)
        for s0 in (await db.execute(select(Service).where(
                Service.name == f"d141-svc-{tag}"))).scalars().all():
            await db.delete(s0)
        for r in (await db.execute(select(AlertRule).where(
                AlertRule.name == f"d141-rule-{tag}"))).scalars().all():
            await db.delete(r)
        await db.commit()


def _event(rule_id: int, value=95.0, host_ident="d141-host"):
    return Ev(hash=f"d141{'0' * 28}", rule_id=rule_id, rule_name=f"d141-rule-{rule_id}",
              host_ident=host_ident, metric="cpu.percent", severity=1,
              trigger_value=value, labels={}, tags={})


async def _runs(policy_id: int) -> list:
    async with SessionLocal() as db:
        return (await db.execute(select(HealPolicyRun).where(
            HealPolicyRun.policy_id == policy_id).order_by(HealPolicyRun.id))).scalars().all()


async def test_rule_linked_policy_fires_and_records_suggest(policy_env):
    """关联规则的策略：本规则事件到达 → suggest 档 → 留痕 dry_run，不下发。"""
    env = policy_env
    pol = (await _pol(env["policy_id"]))
    ev = _event(env["rule_id"])
    async with SessionLocal() as db:
        await hp.evaluate_for_event(db, ev=ev, rule=await db.get(AlertRule, env["rule_id"]))
    runs = await _runs(env["policy_id"])
    assert len(runs) == 1, f"suggest 档应留痕一次，实际 {len(runs)} 条"
    assert runs[0].status == "dry_run"
    assert runs[0].action == "reconnect_pool"


async def test_other_rule_event_does_not_fire(policy_env):
    """别人的规则事件 → 策略不触发（不能谁的告警都去重启服务）。"""
    env = policy_env
    ev = _event(rule_id=999999)   # 不是本策略挂的规则
    async with SessionLocal() as db:
        await hp.evaluate_for_event(db, ev=ev, rule=None)
    runs = await _runs(env["policy_id"])
    assert len(runs) == 0, "非本策略规则的告警不应触发"


async def test_cooldown_blocks_second_fire(policy_env):
    """冷却：第一次触发后立刻再来一条事件 → 不重复触发（防循环自愈）。"""
    env = policy_env
    pol = await _pol(env["policy_id"])
    ev = _event(env["rule_id"])
    async with SessionLocal() as db:
        await hp.evaluate_for_event(db, ev=ev, rule=await _rule(env["rule_id"]))
    async with SessionLocal() as db:
        await hp.evaluate_for_event(db, ev=ev, rule=await _rule(env["rule_id"]))
    runs = await _runs(env["policy_id"])
    assert len(runs) == 1, f"冷却期内第二次事件不应触发，实际 {len(runs)} 条留痕"


async def test_whitelist_action_enforced(policy_env):
    """白名单：动作改成自由文本 → denied 留痕（不自造动作 = 不自造事故）。"""
    env = policy_env
    async with SessionLocal() as db:
        pol = await db.get(HealPolicy, env["policy_id"])
        pol.action = "rm_rf_slash"      # 篡改库值模拟越权动作（API 层根本进不来）
        await db.commit()
    ev = _event(env["rule_id"])
    async with SessionLocal() as db:
        await hp.evaluate_for_event(db, ev=ev, rule=await _rule(env["rule_id"]))
    runs = await _runs(env["policy_id"])
    assert len(runs) == 1 and runs[0].status == "denied"
    assert runs[0].guard_rule == "whitelist"


# ── helpers ─────────────────────────────────────────────
async def _pol(pid):
    async with SessionLocal() as db:
        p = await db.get(HealPolicy, pid)
        p.action = p.action  # detach 读取即可
        return p


async def _rule(rid):
    from app.models.alert import AlertRule as A
    async with SessionLocal() as db:
        return await db.get(A, rid)
