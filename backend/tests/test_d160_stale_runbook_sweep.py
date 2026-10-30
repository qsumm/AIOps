"""D160：僵死变更单不得永久挡住自愈（真机验收发现的第三层根因）。

**现场**：公网平台上 12 张变更单自 2026-10-17 起一直停在 `executing`（验收遗留、
执行中断后再没人推进）。自愈护栏⑧「发布/变更期间禁止自愈」按
`status IN ('approved','executing')` 计数 → **一张僵死的单子把全平台自愈挡了 4 天**，
任何服务挂了都只会留痕 `denied(deploy_window)`，不会真的恢复。

本套件锁死两条防线：
  ① 兜底收敛：`services/runbook.py::sweep_stale_running` 把超时无活动的
     executing/approved 收敛为 aborted（留 action_audit 痕迹，不删数据）；
  ② 护栏口径：`services/heal.py::collect_facts` 只把**近期有活动**（30 分钟内更新过）
     的变更单算作发布窗口——僵死的单子不算。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.ops import ActionAudit, Runbook


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _mk_runbook(status: str, age_minutes: int) -> int:
    async with SessionLocal() as db:
        rb = Runbook(name=f"d160-僵死变更单-{_uid()}", host_ident="d160-test",
                     status=status, execution_mode="manual", risk_level="low",
                     steps=[], pre_checks=[], created_at=datetime.utcnow() - timedelta(days=1),
                     updated_at=datetime.utcnow() - timedelta(minutes=age_minutes))
        db.add(rb)
        await db.commit()
        return rb.id


async def _rm(ids: list[int]) -> None:
    async with SessionLocal() as db:
        for i in ids:
            await db.delete(await db.get(Runbook, i))
        await db.commit()


@pytest.mark.asyncio
async def test_sweep_aborts_stale_running_runbook():
    """① 超时无活动的 executing → 收敛为 aborted，并留审计。"""
    from app.services import runbook as runbook_svc

    rid = await _mk_runbook("executing", age_minutes=180)
    try:
        async with SessionLocal() as db:
            n = await runbook_svc.sweep_stale_running(db, stale_minutes=30)
            assert n >= 1, "僵死变更单应被收敛"
        async with SessionLocal() as db:
            rb = await db.get(Runbook, rid)
            assert rb.status == "aborted", f"状态应收敛为 aborted，实际 {rb.status}"
            audit = (await db.execute(select(ActionAudit).where(
                ActionAudit.runbook_id == rid, ActionAudit.action == "runbook_auto_abort"))).scalars().all()
            assert audit, "收敛必须留审计痕迹（不能悄悄改状态）"
    finally:
        await _rm([rid])


@pytest.mark.asyncio
async def test_sweep_keeps_fresh_running_runbook():
    """① 近期还有活动的 executing **不动**（真在执行的变更单不能被误杀）。"""
    from app.services import runbook as runbook_svc

    rid = await _mk_runbook("executing", age_minutes=1)
    try:
        async with SessionLocal() as db:
            await runbook_svc.sweep_stale_running(db, stale_minutes=30)
        async with SessionLocal() as db:
            rb = await db.get(Runbook, rid)
            assert rb.status == "executing", f"近期活跃的变更单不应被收敛，实际 {rb.status}"
    finally:
        await _rm([rid])


@pytest.mark.asyncio
async def test_heal_guard_ignores_stale_runbooks():
    """② 护栏只认近期活跃的变更单——僵死的单子不构成"发布窗口"。"""
    from app.services import heal as heal_svc

    stale_id = await _mk_runbook("executing", age_minutes=180)
    fresh_id = await _mk_runbook("executing", age_minutes=1)
    try:
        async with SessionLocal() as db:
            facts = await heal_svc.collect_facts(db, service_id=999999, host_id=999999,
                                                 kill_switch=False, upstream_ok=True)
        # 两条都在库里时，"近期活跃"计数应为 1（只有 fresh 那张；stale 不算发布窗口）
        assert facts.get("active_runbooks") == 1, \
            f"护栏只应统计近期活跃的变更单，实际 active_runbooks={facts.get('active_runbooks')}"
    finally:
        await _rm([stale_id, fresh_id])
