"""D4 护栏确定性单测：预算五件套 + 循环护栏 + 终态收敛（用假 LLM 驱动，不依赖真模型）。

对应 03 §1.13 的实现要点；这些护栏是"防无限调工具/防跑飞"的地基，必须可回归。
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.event import ExecutionStep, Incident, IncidentThought, RCAFinding
from app.services import rca as rca_svc

VALID_MD = """---
agent_id: rca-1
purpose: 护栏测试
status: succeeded
tools_used: [query_metrics]
citations: [step_1]
self_assessed_strength: weak
confidence: INCONCLUSIVE
impact:
  affected: []
  checked_healthy: []
  changed_other_reason: []
  not_checked: [全部]
coverage:
  error_signal: checked
  recent_change: not_checked: 无部署数据
  runtime_state: checked
  history: checked
  blast_radius: checked
---

## 结论

测试用结论。

## 证据

- query_metrics 返回 NO_DATA（step_1）

## 推理

无数据可推理。

## 已排除的假设

- 一切（无数据）
"""


class FakeToolCall:
    def __init__(self, name, args, cid="call_1"):
        self.id = cid
        self.function = type("F", (), {"name": name, "arguments": json.dumps(args)})()


class FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeResp:
    """与 llm_gateway.LLMResult 同形状（content/tool_calls/usage/model/finish_reason）。"""

    def __init__(self, msg):
        self.content = msg.content or ""
        self.tool_calls = list(msg.tool_calls or [])
        self.finish_reason = "tool_calls"
        self.model = "fake"
        self.usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}


async def _make_incident(tag: str) -> int:
    async with SessionLocal() as db:
        inc = Incident(title=f"guard-{tag}", severity=3, status="open",
                       alert_hashes=[], source_key=f"g{tag}"[:32], host_ident="guard-host")
        db.add(inc)
        await db.commit()
        await db.refresh(inc)
        return inc.id


async def _cleanup(iid: int):
    async with SessionLocal() as db:
        await db.execute(delete(ExecutionStep).where(ExecutionStep.incident_id == iid))
        await db.execute(delete(IncidentThought).where(IncidentThought.incident_id == iid))
        await db.execute(delete(RCAFinding).where(RCAFinding.incident_id == iid))
        await db.execute(delete(Incident).where(Incident.id == iid))
        await db.commit()


@pytest.mark.asyncio
async def test_same_params_deduped_and_tools_removed_last_round(monkeypatch):
    """同参重复必须被拒；且最后一轮只提供 write_findings（Holmes 预算件 2/3）。"""
    iid = await _make_incident("dedup")
    seen_offered: list[int] = []
    calls = {"n": 0}

    async def fake_call(db, purpose, messages, tools=None, stream=False, model_role="main", temperature=None,
                        timeout_s=600, quota_role=""):
        calls["n"] += 1
        seen_offered.append(len(tools or []))
        names = [t["function"]["name"] for t in (tools or [])]
        if calls["n"] == 1:
            return FakeResp(FakeMsg("先查指标", [FakeToolCall("query_metrics", {"ident": "h", "metric": "m"})]))
        if calls["n"] == 2:
            # 同参重复 → 应被拒绝（不产生第 2 个 execution_step）
            return FakeResp(FakeMsg("再查一次同样的", [FakeToolCall("query_metrics", {"ident": "h", "metric": "m"})]))
        if names == ["write_findings"]:
            return FakeResp(FakeMsg("", [FakeToolCall("write_findings", {"findings_md": VALID_MD})]))
        return FakeResp(FakeMsg("继续"))

    monkeypatch.setattr(rca_svc, "call_llm", fake_call)
    try:
        r = await rca_svc.diagnose(iid, max_steps=3)
        assert r["status"] == "succeeded", r
        assert r["confidence"] == "INCONCLUSIVE"
        async with SessionLocal() as db:
            steps = (await db.execute(select(ExecutionStep).where(ExecutionStep.incident_id == iid))).scalars().all()
            f = (await db.execute(select(RCAFinding).where(RCAFinding.incident_id == iid))).scalars().first()
        assert len(steps) == 1, f"同参重复应被拒绝，实际产生了 {len(steps)} 个 step"
        assert f.findings_md.startswith("---")
        assert 1 in seen_offered and 1 in seen_offered[1:] or seen_offered[-1] == 1, \
            f"最后一轮应只提供 write_findings，实际 tools 数序列={seen_offered}"
    finally:
        await _cleanup(iid)


@pytest.mark.asyncio
async def test_loop_guard_forces_convergence_on_empty_rounds(monkeypatch):
    """连续空转：3 次给提示，5 次强制收敛（Aurora 护栏）。"""
    iid = await _make_incident("loop")
    hints: list[str] = []
    calls = {"n": 0}

    async def fake_call(db, purpose, messages, tools=None, stream=False, model_role="main", temperature=None,
                        timeout_s=600, quota_role=""):
        calls["n"] += 1
        names = [t["function"]["name"] for t in (tools or [])]
        # 前几轮故意什么都不调用（空转）；一旦只剩 write_findings 就收敛
        if names == ["write_findings"]:
            return FakeResp(FakeMsg("", [FakeToolCall("write_findings", {"findings_md": VALID_MD})]))
        return FakeResp(FakeMsg("我还在思考"))

    monkeypatch.setattr(rca_svc, "call_llm", fake_call)

    class Emit:
        async def emit(self, kind, payload):
            if kind == "hint":
                hints.append(payload["text"])

    try:
        r = await rca_svc.diagnose(iid, emitter=Emit(), max_steps=10)
        assert r["status"] == "succeeded", r
        assert any("强制收敛" in h for h in hints), f"应有强制收敛提示，实际 hints={hints}"
        assert calls["n"] < 10, f"应在空转达阈值后收敛，实际调用 {calls['n']} 次"
    finally:
        await _cleanup(iid)


@pytest.mark.asyncio
async def test_validate_retry_then_stub(monkeypatch):
    """报告一直不合规 → 重试 2 次后落 failed stub（服务不挂，报告标注可重试）。"""
    iid = await _make_incident("stub")
    calls = {"n": 0}

    async def fake_call(db, purpose, messages, tools=None, stream=False, model_role="main", temperature=None,
                        timeout_s=600, quota_role=""):
        calls["n"] += 1
        names = [t["function"]["name"] for t in (tools or [])]
        if names == ["write_findings"]:
            return FakeResp(FakeMsg("", [FakeToolCall("write_findings", {"findings_md": "## 结论\n没有 frontmatter"})]))
        return FakeResp(FakeMsg("查一下", [FakeToolCall("host_info", {"ident": "h"})]))

    monkeypatch.setattr(rca_svc, "call_llm", fake_call)
    try:
        r = await rca_svc.diagnose(iid, max_steps=4)
        assert r["status"] == "failed", r
        assert "status: failed" in r["findings_md"] and "可重新发起诊断" in r["findings_md"]
    finally:
        await _cleanup(iid)
