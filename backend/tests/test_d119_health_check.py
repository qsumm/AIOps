"""D119 健康诊断引擎回归锁：10 检查项判定（正常/告警/严重/数据缺失四态）、
报告落库与历史、无 LLM 兜底（engine 本身零 LLM 依赖）、单项异常不中断。"""
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.host import Host
from app.models.health import HealthReport
from app.services import health_check as hc
from app.services import ingest

FP = "test-fp-d119"


async def _mk_host(db):
    tok = ingest.mint_install_token()
    data = await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d119", "install_token": tok})
    h = (await db.execute(select(Host).where(Host.ident == data["ident"]))).scalar_one()
    return data["ident"], h.id, h


async def _rm_host(db, ident):
    h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if h is not None:
        await db.delete(h)
        await db.commit()


@pytest.mark.asyncio
async def test_diagnose_produces_complete_report_without_llm():
    """无 LLM 兜底硬验收：engine 零 LLM 调用；老 agent（无新指标）→ 检查项 skip 而非报错。"""
    async with SessionLocal() as db:
        ident, host_id, _ = await _mk_host(db)
        try:
            data = await hc.diagnose_host(db, host_id, ident, minutes=60)
            assert data["report_id"] > 0
            s = data["summary"]
            assert set(s) >= {"overall", "pass", "warn", "fail", "skip"}
            assert len(data["items"]) == len(hc.CHECKS) == 10
            # 新指标未上报 → 这些项应 skip（不 error，不中断）
            by_id = {i["id"]: i for i in data["items"]}
            assert by_id["cpu_high"]["status"] == "skip"
            assert by_id["probe"]["status"] == "skip"      # 未配探活
            assert by_id["service_down"]["status"] == "skip"
            assert s["overall"] in ("healthy", "attention", "unhealthy")
            # 报告已落库可回看
            r = (await db.execute(select(HealthReport).where(HealthReport.id == data["report_id"]))).scalar_one()
            assert r.status == "done" and r.summary["overall"] == s["overall"]
            assert len(r.items) == 10
        finally:
            await _rm_host(db, ident)


@pytest.mark.asyncio
async def test_stats_and_thresholds():
    s = hc._stats([(1, 10.0), (2, 50.0), (3, 90.0)])
    assert s == {"latest": 90.0, "avg": 50.0, "p95": 90.0, "peak": 90.0}
    assert hc._stats([]) == {"latest": None, "avg": None, "p95": None, "peak": None}
    assert hc._thres({"p95": 90, "latest": 60}) == "fail"
    assert hc._thres({"p95": 75, "latest": 60}) == "warn"
    assert hc._thres({"p95": 50, "latest": 75}) == "warn"   # latest 过阈也算
    assert hc._thres({"p95": 50, "latest": 55}) == "pass"
    assert hc._thres({"p95": None, "latest": None}) == "skip"


@pytest.mark.asyncio
async def test_cpu_check_levels():
    """构造 ring 序列：>85 → fail，70~85 → warn，正常 → pass。"""
    from app.store import ring as R
    from datetime import datetime, timedelta
    async with SessionLocal() as db:
        ident, host_id, _ = await _mk_host(db)
        try:
            now = datetime.utcnow()
            for lvl, expect in ((92.0, "fail"), (78.0, "warn"), (30.0, "pass")):
                for i in range(4):
                    R.append_point(ident, "cpu.percent", now - timedelta(minutes=5 - i), lvl)
                st, _, con = await hc.check_cpu_high(db, ident, [])
                assert st == expect, f"{lvl}% → {st} (期望 {expect})"
                R._rings.pop((ident, "cpu.percent"), None)
        finally:
            await _rm_host(db, ident)


@pytest.mark.asyncio
async def test_item_error_does_not_break_report():
    """单项抛异常 → error 态，报告完整（10 项不缺）。"""
    async with SessionLocal() as db:
        ident, host_id, _ = await _mk_host(db)
        try:
            async def _boom(db, ident, ev):
                raise RuntimeError("boom")
            # CHECKS 元组在 import 时已绑定函数引用，须原位替换才能命中分发路径
            orig_entry = hc.CHECKS[0]
            hc.CHECKS[0] = (orig_entry[0], orig_entry[1], orig_entry[2], _boom)
            try:
                data = await hc.diagnose_host(db, host_id, ident, minutes=60)
            finally:
                hc.CHECKS[0] = orig_entry
            by_id = {i["id"]: i for i in data["items"]}
            assert by_id["cpu_high"]["status"] == "error"
            assert len(data["items"]) == 10
        finally:
            await _rm_host(db, ident)