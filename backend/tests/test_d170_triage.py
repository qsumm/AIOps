"""D170（FR-426）：发现分诊闭环——诊断报告的人工处置 + 经验降噪。

**需求来源**（用户，2026-10-22）：AI 给的建议太零散，不知道哪些可以忽略、哪些必须
跟进——这依赖经验丰富的人。评估结论：巡检/日志取证/诊断产品已有，缺的是**处置
闭环**（发现没有跟进/忽略动作、忽略不沉淀经验）。

设计：以「一次诊断报告」（rca_findings，粒度=一次运行）为分诊单位：
  · followup（跟进）/ observing（观察）/ dismissed（忽略+必填理由）
  · dismissed 留痕后，同主机的后续报告自动带降噪提示（理由引用）
  · 报告 API 输出 strength → 建议档位（strong=建议必跟 … weak/inconclusive=可忽略区），
    分级只是初筛，最终以人工处置为准
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.alert import AlertHisEvent
from app.models.event import FindingTriage, Incident, RCAFinding


class _U:
    username = "tester"


class _Req:
    headers = {}
    client = None
    url = type("U", (), {"path": "/test"})()

    async def json(self):
        return {}

PRE = "d170-triage"


def _h(n: str) -> str:
    return hashlib.sha256(f"{PRE}{n}".encode()).hexdigest()[:32]


async def _mk_incident_with_finding(strength: str = "moderate"):
    async with SessionLocal() as db:
        await db.execute(delete(FindingTriage).where(FindingTriage.incident_id.in_(
            select(Incident.id).where(Incident.title.like(f"{PRE}%")))))
        await db.execute(delete(RCAFinding).where(RCAFinding.incident_id.in_(
            select(Incident.id).where(Incident.title.like(f"{PRE}%")))))
        await db.execute(delete(Incident).where(Incident.title.like(f"{PRE}%")))
        await db.execute(delete(AlertHisEvent).where(AlertHisEvent.hash == _h("1")))
        inc = Incident(title=f"{PRE}内存异常", severity=2, status="open",
                       alert_hashes=[_h("1")], source_key=_h("1"), host_ident="d170-host")
        db.add(inc)
        await db.commit()
        await db.refresh(inc)
        his = AlertHisEvent(hash=_h("1"), rule_id=1, rule_name=f"{PRE}规则", host_ident="d170-host",
                            metric="mem.percent", severity=2, trigger_value=91.0,
                            trigger_time=inc.created_at, tags={"host": "d170-host"})
        db.add(his)
        f = RCAFinding(incident_id=inc.id, agent_id="rca-1", role_name="root_cause_analyst",
                       status="succeeded", findings_md="---\nconfidence: LIKELY\nself_assessed_strength: "
                       f"{strength}\n---\n# 报告\n测试内容", self_assessed_strength=strength)
        db.add(f)
        await db.commit()
        await db.refresh(inc)
        await db.refresh(f)
        return inc.id, f.id


async def _cleanup():
    async with SessionLocal() as db:
        await db.execute(delete(FindingTriage).where(FindingTriage.incident_id.in_(
            select(Incident.id).where(Incident.title.like(f"{PRE}%")))))
        await db.execute(delete(RCAFinding).where(RCAFinding.incident_id.in_(
            select(Incident.id).where(Incident.title.like(f"{PRE}%")))))
        await db.execute(delete(Incident).where(Incident.title.like(f"{PRE}%")))
        await db.execute(delete(AlertHisEvent).where(AlertHisEvent.hash == _h("1")))
        await db.commit()


@pytest.mark.asyncio
async def test_triage_dismissed_requires_reason():
    """忽略必须填理由——经验复用的依据，缺理由 400。"""
    from fastapi import HTTPException

    import app.routers.incidents as ri

    async def _noop(*a, **k):
        return None
    ri.audit = _noop

    iid, fid = await _mk_incident_with_finding()

    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            await ri.triage_finding(iid, fid, {"action": "dismissed", "reason": ""}, _Req(), db, _U())
    assert ei.value.status_code == 400
    assert "理由" in str(ei.value.detail)
    await _cleanup()


@pytest.mark.asyncio
async def test_triage_actions_and_latest_wins():
    """三态写入合法；同报告多次处置取最新（幂等重判）。"""
    import app.routers.incidents as ri

    async def _noop(*a, **k):
        return None
    ri.audit = _noop

    iid, fid = await _mk_incident_with_finding()

    async with SessionLocal() as db:
        await ri.triage_finding(iid, fid, {"action": "observing", "reason": ""}, _Req(), db, _U())
        await ri.triage_finding(iid, fid, {"action": "followup", "reason": "磁盘确实快满"}, _Req(), db, _U())
        await ri.triage_finding(iid, fid, {"action": "dismissed", "reason": "常态水位"}, _Req(), db, _U())

    async with SessionLocal() as db:
        rows = (await db.execute(select(FindingTriage).where(
            FindingTriage.finding_id == fid).order_by(FindingTriage.id))).scalars().all()
        assert len(rows) == 3
        assert rows[-1].action == "dismissed" and rows[-1].reason == "常态水位"
    await _cleanup()


@pytest.mark.asyncio
async def test_report_carries_triage_and_muted_note():
    """报告 API：findings[].triage 带处置与建议档位；同主机有忽略历史 → muted_note。"""
    import app.routers.incidents as ri

    async def _noop(*a, **k):
        return None
    ri.audit = _noop

    iid, fid = await _mk_incident_with_finding("strong")

    async with SessionLocal() as db:
        await ri.triage_finding(iid, fid, {"action": "dismissed", "reason": "确认常态"}, _Req(), db, _U())
        out = await ri.incident_findings(iid, db, None)
    t0 = out["data"]["triage"]
    assert t0 is not None, "报告应带处置记录"
    assert t0["action"] == "dismissed"
    assert t0["suggested"] == "建议必跟", "strong 应映射为「建议必跟」"
    assert "忽略" in out["data"]["triage_muted_note"], out["data"]["triage_muted_note"]
    await _cleanup()


@pytest.mark.asyncio
async def test_no_triage_returns_clean():
    """未处置的报告：triage=None、无降噪提示——不影响既有前端。"""
    import app.routers.incidents as ri

    iid, fid = await _mk_incident_with_finding()
    async with SessionLocal() as db:
        out = await ri.incident_findings(iid, db, None)
    assert out["data"]["triage"] is None
    assert out["data"]["triage_muted_note"] == ""
    await _cleanup()
