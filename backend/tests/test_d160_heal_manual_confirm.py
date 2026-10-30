"""D160：人工确认档自愈不能 500（真机验收发现：确认执行直接 TypeError→500）。

**现场**：公网平台上 `POST /api/ops/heal-policy-runs/{rid}/confirm` 必 500——
`services/heal_policy.py::confirm_run` 调用 `_dispatch(..., ev=None, ...)`，而
`_dispatch` 的签名里**没有 ev** → 每次人工确认都 TypeError。
后果：**「人工确认执行」这条自愈路径 100% 不可用**（mode=manual 策略永远执行不了）。
之前没暴露，是因为演示与 e2e 里的自愈策略全是 `suggest`（仅建议）/ `auto` 档，manual 档没人点。

本套件钉住：确认执行**不得抛 TypeError**；放行时必须真的下发一条 svc_action 任务。
"""
from __future__ import annotations

import types

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.ops import HealPolicy, HealPolicyRun
from app.models.svc import AgentTask, Service


@pytest.mark.asyncio
async def test_manual_confirm_does_not_500_and_dispatches():
    """人工确认执行：不得 TypeError；护栏放行时必须下发 svc_action 任务。"""
    from app.models.host import Host
    from app.services import heal_policy as heal_svc

    async with SessionLocal() as db:
        # 幂等清理：上一轮若中途失败会留下同名行（ident 唯一）
        await db.execute(HealPolicyRun.__table__.delete().where(
            HealPolicyRun.policy_name == "d160-manual-confirm"))
        await db.execute(HealPolicy.__table__.delete().where(
            HealPolicy.name == "d160-manual-confirm"))
        await db.execute(Service.__table__.delete().where(Service.name == "D160Svc"))
        await db.execute(Host.__table__.delete().where(Host.ident == "d160-heal-host"))
        await db.commit()
        h = Host(ident="d160-heal-host", name="d160-heal-host", status="up")
        db.add(h)
        await db.commit()
        host_id = h.id
        svc = Service(host_id=host_id, name="D160Svc", type="app", status="down",
                      spec_json={"port": 80, "unit": "d160-svc"}, enabled=True)
        db.add(svc)
        await db.commit()
        svc_id = svc.id
        pol = HealPolicy(name="d160-manual-confirm", action="restart_service", mode="manual",
                         service_id=svc_id, trigger_rule_id=None, enabled=True, cooldown_min=1)
        db.add(pol)
        await db.commit()
        pol_id = pol.id
        run = HealPolicyRun(policy_id=pol_id, policy_name=pol.name, status="waiting_confirm",
                            host_ident="d160-heal-host", action="restart_service", mode="manual",
                            detail={"service": svc_id, "service_id": svc_id})
        db.add(run)
        await db.commit()
        run_id = run.id

    try:
        user = types.SimpleNamespace(username="d160-tester")
        dispatched_task = None
        async with SessionLocal() as db:
            try:
                res = await heal_svc.confirm_run(db, run_id=run_id, user=user)
            except ValueError as e:
                # 护栏拒绝是**合法结论**（比如环境里有正在执行的变更单）；TypeError 才是缺陷
                assert "TypeError" not in str(e), f"确认执行不得 TypeError：{e}"
                res = None
            if res:
                dispatched_task = res.get("task_id")
                assert dispatched_task, f"放行后必须返回 task_id：{res}"
        if dispatched_task:
            async with SessionLocal() as db:
                t = (await db.execute(select(AgentTask).where(AgentTask.id == dispatched_task))).scalar_one_or_none()
                assert t is not None, "放行后必须真的下发一条任务"
                assert t.kind == "svc_action", f"任务类型应为 svc_action，实际 {t.kind}"
                assert (t.host_id == host_id), "任务必须发给该服务所在主机"
            async with SessionLocal() as db:
                r = await db.get(HealPolicyRun, run_id)
                assert r.status == "dispatched", f"留痕状态应为 dispatched，实际 {r.status}"
    finally:
        async with SessionLocal() as db:
            await db.execute(HealPolicyRun.__table__.delete().where(HealPolicyRun.id == run_id))
            await db.execute(HealPolicy.__table__.delete().where(HealPolicy.id == pol_id))
            await db.execute(Service.__table__.delete().where(Service.id == svc_id))
            await db.execute(AgentTask.__table__.delete().where(AgentTask.origin == f"heal_policy:d160-manual-confirm"))
            from app.models.host import Host
            await db.execute(Host.__table__.delete().where(Host.id == host_id))
            await db.commit()


def test_confirm_run_call_signature_matches_dispatch():
    """源码级：_dispatch 的调用不得再传它不接受的参数（ev= 就是那个 TypeError）。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "heal_policy.py").read_text(encoding="utf-8")
    assert "ev=None" not in src, "_dispatch 调用里残留 ev=None（签名无此参数 → 必 TypeError）"
    assert "async def _dispatch(db, *, pol: HealPolicy, svc, host_name: str)" in src, \
        "_dispatch 签名若变更，测试需同步"
