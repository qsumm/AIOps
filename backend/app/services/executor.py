# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""D110 Remote executor：把已校验的枚举动作下发给 Agent 并等待真实回执。

设计边界：
- 平台侧**不保存任何目标机凭据**（无 SSH 密钥），复用 Agent 的拉取式通道；
- 只接受 EXEC_ACTIONS 中的枚举 action（runbook.py 已校验，Agent 端再校验一次）；
- 任务落 agent_tasks 全程可回放；回执经 _mask_result 脱敏（由 tasks 路由完成）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.host import Host
from app.models.svc import AgentTask

log = logging.getLogger("executor")

TASK_TTL_MIN = 10


async def enqueue_and_wait(host_ident: str, action: str, params: dict,
                           timeout_s: int = 90, poll_s: float = 2.0) -> dict:
    """入队 svc_action 任务并轮询等待 Agent 回执。返回 {ok, task_id, result|error}。"""
    async with SessionLocal() as db:
        host = (await db.execute(select(Host).where(Host.ident == host_ident))).scalars().first()
        if host is None:
            return {"ok": False, "error": f"HOST_NOT_FOUND: {host_ident}"}
        now = datetime.utcnow()
        pend = (await db.execute(
            select(AgentTask).where(AgentTask.host_id == host.id, AgentTask.status == "pending",
                                    AgentTask.expires_at > now))).scalars().first()
        if pend is not None:
            return {"ok": False, "error": "HOST_BUSY: 该主机已有待领取任务（约 15 秒内执行）"}
        t = AgentTask(host_id=host.id, kind="svc_action",
                      payload_json={"action": action, **(params or {})},
                      origin="runbook-executor", created_by="executor",
                      expires_at=now + timedelta(minutes=TASK_TTL_MIN))
        db.add(t)
        await db.commit()
        await db.refresh(t)
        task_id = t.id
    log.info("已下发 %s → host=%s task=%s", action, host_ident[:12], task_id)

    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as db:
            t = await db.get(AgentTask, task_id)
            if t is not None and t.status in ("done", "failed"):
                return {"ok": t.status == "done", "task_id": task_id,
                        "result": t.result_json or {}, "error": t.error if t.status == "failed" else None}
        await asyncio.sleep(poll_s)
    return {"ok": False, "task_id": task_id, "error": f"TIMEOUT: Agent 在 {timeout_s}s 内未回执"}
