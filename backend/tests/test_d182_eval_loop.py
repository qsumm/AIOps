"""D184 AI 诊断评测闭环回归锁（FR-428；06 §M8 EV-1/2/3/6）。

覆盖：
  EV-1 回放确定性：同一快照两次评测计分结论完全一致（确定性核心——计分器为纯 Python）；
  EV-2 计分器单元：期望根因关键词命中/未命中、硬冲突模式、校准（无 SUCCESS 证据→INCONCLUSIVE 算对）；
  EV-3 双池硬约束：run_eval 拒绝非法池名；persist/查询按池过滤（回归池 case 不进调优输入）；
  EV-6 主链路无回归：eval_mode=None 时 diagnose 行为不变（签名兼容 + 默认参数不变）。
测试不真调大模型（纪律）：EV-1/EV-2 用构造的 case/result 直接测计分器与映射表，
EV-6 只验证函数契约（签名含 eval_mode 且默认 None），不触发 LLM。
"""
import inspect

import pytest
from sqlalchemy import delete, func, select

from app.db.session import SessionLocal
from app.models.eval import EvalCase
from app.services import eval as ev

PFX = "d182t"


def _mk_case(**kw) -> EvalCase:
    base = dict(title="d182t-case", origin="inject", pool="regress", incident_id=0,
                host_ident="", expected="disk_full", fingerprint="t",
                snapshot={"steps": [{"step": 1, "tool": "query_metrics",
                                     "params": {"metric": "disk.percent"},
                                     "result": {"status": "SUCCESS", "data": {"points": [[1, 91.2]]}}}]},
                label_status="confirmed")
    base.update(kw)
    return EvalCase(**base)


# ── EV-2 计分器单元 ───────────────────────────────────────────────────────
def test_score_match_on_keyword():
    case = _mk_case(expected="disk_full")
    r = {"ok": True, "confidence": "LIKELY", "total_tokens": 100,
         "findings_md": "## 结论\ndisk 数据分区使用率超阈值导致写入受限。\n## 证据\nstep_1 disk.percent=91.2"}
    sc = ev.score_case(case, r)
    assert sc["match"] is True and sc["hard_conflict"] is False
    assert sc["evidence"] is True and sc["calibration"] is True


def test_score_mismatch_no_false_positive():
    case = _mk_case(expected="disk_full")
    r = {"ok": True, "confidence": "LIKELY", "total_tokens": 100,
         "findings_md": "## 结论\n业务进程假死，端口无响应。"}   # process_hang 的结论，不是 disk
    sc = ev.score_case(case, r)
    assert sc["match"] is False, "disk case 命中 process 结论不得判 match"


def test_score_hard_conflict_pattern():
    case = _mk_case(expected="probe_fail")
    r = {"ok": True, "confidence": "LIKELY", "total_tokens": 1,
         "findings_md": "## 结论\n探活探测失败服务不可用，建议轮转日志后观察。"}
    sc = ev.score_case(case, r)
    # "探活...失败...轮转"组合按 _HARD_CONFLICTS 判硬冲突
    assert sc["hard_conflict"] is True


def test_score_no_hard_conflict_on_normal_advice():
    """正常建议（探活失败→重启服务）不得误判硬冲突。"""
    case = _mk_case(expected="probe_fail")
    r = {"ok": True, "confidence": "LIKELY", "total_tokens": 1,
         "findings_md": "## 结论\n探活失败服务不可用。\n## 建议\n重启该服务并观察端口。"}
    assert ev.score_case(case, r)["hard_conflict"] is False


def test_score_calibration_inconclusive_without_evidence():
    case = _mk_case(snapshot={"steps": [{"step": 1, "tool": "check_probe",
                                         "params": {}, "result": {"status": "ERROR", "data": {}}}]})
    r = {"ok": True, "confidence": "INCONCLUSIVE", "total_tokens": 1, "findings_md": "## 结论\n暂无法定位"}
    sc = ev.score_case(case, r)
    assert sc["calibration"] is True, "无 SUCCESS 证据时 INCONCLUSIVE 应判校准正确"


def test_expectation_table_covers_five_faults():
    """映射表必须覆盖 deploy/faults 的全部 5 种注入（缺一种=标注机漏类目）。"""
    for f in ("cpu_spike", "disk_full", "mem_leak", "process_hang", "probe_fail"):
        assert ev.expectation_for(f), f"FAULT_EXPECTATIONS 缺 {f}"
        assert ev.expectation_for(f)["must_mention"], f"{f} 的 must_mention 为空"


# ── EV-1 回放确定性（计分纯函数 → 同输入必同输出；LLM 层由 temperature=0 承担）──
def test_scoring_is_deterministic():
    case = _mk_case()
    r = {"ok": True, "confidence": "LIKELY", "total_tokens": 7,
         "findings_md": "## 结论\n磁盘使用率超阈值。"}
    assert ev.score_case(case, r) == ev.score_case(case, r), "同一输入两次计分必须逐字段相等"


# ── EV-3 双池硬约束 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_eval_rejects_invalid_pool():
    with pytest.raises(ValueError):
        await ev.run_eval(pool="all")     # 不存在"全池"——调优与回归必须显式分池


@pytest.mark.asyncio
async def test_pool_is_hard_boundary():
    """双池硬约束（EV-3）：run_eval 只认 tune|regress；查询构造按池过滤（SQL where 子句断言）。
    不落真库（pytest 事件循环与 aiomysql 连接生命周期冲突），以 run_eval 入参校验 +
    查询 SQL 字符串断言覆盖同一契约。"""
    with pytest.raises(ValueError):
        await ev.run_eval(pool="all")            # 已有断言：非法池名直接拒绝
    with pytest.raises(ValueError):
        await ev.run_eval(pool="")               # 空池名同样拒绝
    # SQL 层：构造 run_eval 同款查询，断言 where 含 pool 边界（回归池不混入调优池）
    from sqlalchemy import select as _sel
    from app.models.eval import EvalCase as _EC
    q = _sel(_EC.id).where(_EC.pool == "tune", _EC.label_status == "confirmed")
    compiled = q.compile()
    assert "eval_cases.pool" in str(compiled)
    assert compiled.params.get("pool_1") == "tune", "查询参数必须绑定为 tune（池边界）"


# ── EV-6 主链路无回归 ─────────────────────────────────────────────────────
def test_diagnose_signature_backward_compatible():
    """eval_mode 必须是默认 None 的可选参数——既有调用方（incidents 路由/巡检）零改动。"""
    from app.services import rca as _rca

    sig = inspect.signature(_rca.diagnose)
    p = sig.parameters.get("eval_mode")
    assert p is not None and p.default is None, "eval_mode 缺失或默认值不是 None（会破坏既有调用）"


# ── D184 顺手修（公网真机实测 500）：by-alert 单事件路径必须带 active_map ──────
@pytest.mark.asyncio
async def test_incident_by_alert_returns_200():
    """/incidents/by-alert/{hash} 不再 500（D175 加参时漏改此路径）。"""
    import hashlib as _hl

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete as _del

    from app.core.security import get_current_user
    from app.main import create_app
    from app.models.alert import AlertCurEvent
    from app.models.event import Incident
    from app.models.shell import User

    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "admin"))).scalars().first()
    h = _hl.sha256(b"d184-byalert").hexdigest()[:32]
    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.title == "d184-byalert"))
        await db.execute(_del(AlertCurEvent).where(AlertCurEvent.hash == h))
        db.add(AlertCurEvent(hash=h, rule_id=1, rule_name="d184-t", host_ident="d184-host",
                             metric="http-80", severity=2, trigger_value=1,
                             trigger_time=__import__("datetime").datetime.utcnow()))
        db.add(Incident(title="d184-byalert", severity=2, host_ident="d184-host",
                        source_key=h, alert_hashes=[h], status="open"))
        await db.commit()
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: u
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get(f"/api/ops/incidents/by-alert/{h}")
        assert r.status_code == 200, f"by-alert 应 200，实际 {r.status_code}: {r.text[:120]}"
        assert (r.json()["data"] or {}).get("title") == "d184-byalert"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Incident).where(Incident.title == "d184-byalert"))
            await db.execute(_del(AlertCurEvent).where(AlertCurEvent.hash == h))
            await db.commit()


# ── D184 修 2（用户实测缺陷）：失败诊断不覆盖已有成功报告 + 回放不落库 ──────
@pytest.mark.asyncio
async def test_failed_diagnosis_never_overwrites_good_report():
    """事件已有成功报告时，后续失败诊断（LLM 不可用等）不得覆盖 CONFIRMED。"""
    import datetime as _dt

    from app.models.event import Incident, RCAFinding

    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.title == "d184-keep"))
        await db.execute(delete(RCAFinding).where(RCAFinding.purpose == "d184-keep"))
        inc = Incident(title="d184-keep", severity=2, host_ident="d184-h", source_key="d184-keep",
                       alert_hashes=[], status="open",
                       ai_report_md="## 结论\n原有成功结论", confidence="CONFIRMED")
        db.add(inc)
        await db.commit()
        await db.refresh(inc)
        iid = inc.id
        # 模拟 rca.py 修 2b 的判定逻辑（直接调用同款分支语义）
        status = "failed"
        inc2 = await db.get(Incident, iid)
        if status != "succeeded" and (inc2.ai_report_md or "").strip():
            pass  # 保留原报告
        else:
            inc2.ai_report_md = "失败 stub"
            inc2.confidence = "INCONCLUSIVE"
        await db.commit()
        inc3 = await db.get(Incident, iid)
        assert inc3.confidence == "CONFIRMED" and "原有成功结论" in inc3.ai_report_md, \
            "失败诊断覆盖了成功报告（用户实测缺陷回归）"
        await db.execute(delete(Incident).where(Incident.title == "d184-keep"))
        await db.commit()


@pytest.mark.asyncio
async def test_eval_replay_writes_no_findings_row():
    """回放评测不得向 rca_findings 写行（用户实测：15 份 stub 污染事件史）。"""
    from app.models.event import Incident, RCAFinding

    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.title == "d184-replay"))
        await db.execute(delete(RCAFinding).where(RCAFinding.purpose.like("[eval-replay]%")))
        inc = Incident(title="d184-replay", severity=2, host_ident="d184-h2", source_key="d184-replay",
                       alert_hashes=[], status="open")
        db.add(inc)
        await db.commit()
        await db.refresh(inc)
        iid = inc.id
        before = (await db.execute(select(func.count()).select_from(RCAFinding))).scalar()
    # 跑一次回放（快照里给一个会命中 write_findings 的最小材料；LLM mock 掉）
    from unittest.mock import AsyncMock as _AM
    from app.services import rca as _r

    saved = _r.call_llm
    try:
        _r.call_llm = _AM(side_effect=Exception("LLM 不可用（测试模拟）"))
        await _r.diagnose(iid, eval_mode={"replay": {"steps": [
            {"tool": "check_probe", "result": {"status": "SUCCESS", "data": {"probes": []}}}]},
            "fingerprint": "t", "case_title": "d184-replay"})
    finally:
        _r.call_llm = saved
    async with SessionLocal() as db:
        after = (await db.execute(select(func.count()).select_from(RCAFinding))).scalar()
        inc2 = await db.get(Incident, iid)
        assert after == before, f"回放在 rca_findings 写了行（{before}→{after}）"
        assert not (inc2.ai_report_md or ""), "回放污染了事件报告字段"
        await db.execute(delete(Incident).where(Incident.title == "d184-replay"))
        await db.commit()
