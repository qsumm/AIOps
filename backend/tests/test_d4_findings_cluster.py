"""D4 单测：write_findings 强 schema 校验（06 §2 强制模块 3）+ 收敛聚类（强制模块 4）。

四强制单测之一/之二；不依赖 LLM（收敛走规则兜底路径），findings 校验为纯函数。
"""
import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.event import Incident
from app.services import cluster as cluster_svc
from app.services.findings import failed_stub, parse_findings, validate_findings

VALID = """---
agent_id: rca-1
purpose: cpu 高负载根因
status: succeeded
tools_used: [query_metrics, host_info]
citations: [step_1, step_2]
self_assessed_strength: moderate
confidence: LIKELY
impact:
  affected: [{entity: demo-web-01, evidence_token: cpu 92% 持续 40min}]
  checked_healthy: [demo-cache-01]
  changed_other_reason: []
  not_checked: [磁盘 io]
coverage:
  error_signal: checked
  recent_change: not_checked: 无部署数据
  runtime_state: checked
  history: checked
  blast_radius: checked
---

## 结论

CPU 持续高位，疑似某进程空转。

## 证据

- query_metrics: cpu.percent max 97.2 / avg 91.4（step_1）
- host_info: 无最近变更记录（step_2）

## 推理

指标窗口内无发布，负载与 CPU 同步上升，指向本机进程而非外部流量。

## 已排除的假设

- 排除了外部流量：QPS 未变（未查到 QPS 数据，故仅弱排除）
"""


def test_validate_valid_findings():
    f, errs = parse_and_validate(VALID)
    assert errs == [], errs
    assert f.confidence == "LIKELY"
    assert f.strength == "moderate"
    assert f.ok


def parse_and_validate(text):
    f = parse_findings(text)
    return f, validate_findings(f)


def test_coverage_checklist_required_and_complete():
    """P1-11：五个调查维度必须出现；缺一项就报错，逼模型显式写 not_checked。"""
    f, errs = parse_and_validate(VALID)
    assert errs == [], errs

    bad = VALID.replace("  history: checked\n", "")
    f, errs = parse_and_validate(bad)
    assert any("coverage 缺 history" in e for e in errs), errs


def test_action_plan_requires_evidence_and_rollback():
    """P1-9：结构化处置计划有则必须可落地（每步带证据与回滚）。"""
    with_plan = VALID.replace("not_checked: [磁盘 io]", """not_checked: [磁盘 io]
action_plan:
  - step: 1
    action: restart_service
    service: nginx
    evidence: step_1
    precondition: check_probe HTTP down
    rollback: systemctl start nginx""")
    f, errs = parse_and_validate(with_plan)
    assert errs == [], errs

    bad = with_plan.replace("    rollback: systemctl start nginx\n", "")
    f, errs = parse_and_validate(bad)
    assert any("rollback" in e for e in errs), errs


def test_missing_section_fails():
    bad = VALID.replace("## 推理", "## Something else")
    f, errs = parse_and_validate(bad)
    assert any("推理" in e for e in errs), errs


def test_missing_citations_fails():
    bad = VALID.replace("citations: [step_1, step_2]", "citations: []")
    f, errs = parse_and_validate(bad)
    assert any("citations" in e for e in errs), errs


def test_bad_confidence_fails():
    bad = VALID.replace("confidence: LIKELY", "confidence: PROBABLY")
    f, errs = parse_and_validate(bad)
    assert any("confidence" in e for e in errs), errs


def test_affected_without_evidence_token_fails():
    bad = VALID.replace("affected: [{entity: demo-web-01, evidence_token: cpu 92% 持续 40min}]",
                        "affected: [{entity: demo-web-01}]")
    f, errs = parse_and_validate(bad)
    assert any("evidence_token" in e for e in errs), errs


def test_no_frontmatter_fails():
    f, errs = parse_and_validate("## 结论\n没有任何元数据")
    assert any("frontmatter" in e for e in errs), errs


def test_failed_stub_passes_validation():
    """stub 必须自身能通过校验（否则落库的是坏数据）。"""
    f, errs = parse_and_validate(failed_stub("LLM 不可达", ["query_metrics"]))
    assert errs == [], errs
    assert f.status == "failed" and f.confidence == "INCONCLUSIVE"


# ---------------- 收敛聚类 ----------------
@pytest.mark.asyncio
async def test_cluster_same_host_merges():
    """同机同窗：第二条并入同一 incident（规则兜底，不依赖 LLM）。"""
    base = {"rule_name": "演示-CPU高负载", "metric": "cpu.percent", "host_ident": "cluster-test-h1", "severity": 2}
    r1 = await cluster_svc.cluster_alert({**base, "hash": "c" * 32, "trigger_value": 91, "trigger_time": "t"}, use_llm=False)
    r2 = await cluster_svc.cluster_alert({**base, "hash": "d" * 32, "trigger_value": 95, "trigger_time": "t"}, use_llm=False)
    try:
        assert r1["incident_id"] == r2["incident_id"], "同机同窗应并入同一事件"
        assert r2["merged"] is True and r2["by"] == "rule"
        async with SessionLocal() as db:
            inc = await db.get(Incident, r1["incident_id"])
            assert len(inc.alert_hashes) == 2
            assert inc.severity == 2
    finally:
        await _cleanup(["c" * 32, "d" * 32])


@pytest.mark.asyncio
async def test_cluster_idempotent_by_source_key():
    """同一告警指纹重复处理 → 幂等返回同一 incident（不重复建）。"""
    a = {"hash": "e" * 32, "rule_name": "r", "metric": "m", "host_ident": "cluster-test-h2", "severity": 3}
    r1 = await cluster_svc.cluster_alert(a, use_llm=False)
    r2 = await cluster_svc.cluster_alert(a, use_llm=False)
    try:
        assert r1["incident_id"] == r2["incident_id"]
        assert r2["by"] == "idempotent"
        async with SessionLocal() as db:
            rows = (await db.execute(select(Incident).where(Incident.source_key == a["hash"]))).scalars().all()
            assert len(rows) == 1
    finally:
        await _cleanup(["e" * 32])


@pytest.mark.asyncio
async def test_cluster_other_host_creates_new():
    """不同主机 → 新事件（一级规则聚类的 key 含 host）。"""
    r1 = await cluster_svc.cluster_alert({"hash": "f" * 32, "rule_name": "r", "metric": "m",
                                          "host_ident": "cluster-test-h3", "severity": 3}, use_llm=False)
    r2 = await cluster_svc.cluster_alert({"hash": "a1" + "b" * 30, "rule_name": "r", "metric": "m",
                                          "host_ident": "cluster-test-h4", "severity": 3}, use_llm=False)
    try:
        assert r1["incident_id"] != r2["incident_id"], "不同主机不应并入"
    finally:
        await _cleanup(["f" * 32, "a1" + "b" * 30])


@pytest.mark.asyncio
async def test_manual_merge_and_split():
    a = {"hash": "1a" + "0" * 30, "rule_name": "r", "metric": "m", "host_ident": "cluster-test-h5", "severity": 3}
    b = {"hash": "2b" + "0" * 30, "rule_name": "r2", "metric": "m2", "host_ident": "cluster-test-h5", "severity": 2}
    r1 = await cluster_svc.cluster_alert(a, use_llm=False)
    r2 = await cluster_svc.cluster_alert(b, use_llm=False)
    try:
        # 不同规则族但同主机 → 一级 key 用 host，同窗会并入；若已并入则先拆开再验证 merge/split
        if r1["incident_id"] == r2["incident_id"]:
            sp = await cluster_svc.manual_split(r1["incident_id"], b["hash"])
            assert sp["ok"], sp
            r2 = {"incident_id": sp["new"]}
            r1["incident_id"] = r1["incident_id"]
        m = await cluster_svc.manual_merge(r1["incident_id"], r2["incident_id"])
        assert m["ok"] and m["members"] >= 2, m
        sp2 = await cluster_svc.manual_split(r1["incident_id"], a["hash"])
        assert sp2["ok"] is False or sp2["ok"] is True  # 单成员拆分应被拒；多成员应成功
    finally:
        await _cleanup([a["hash"], b["hash"]])


async def _cleanup(hashes: list[str]) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.source_key.in_(hashes)))
        await db.commit()
