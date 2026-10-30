"""P1：RCA 诊断信号补充（服务级 + 主机发现 + 近期变更 + 证据回放）。

锁五件事：
  ① service_health：把 svc_monitor 已采集的服务指标/状态/日志路径给到 AI；
  ② discovered_services：把 Agent 最近一次端口/Top 进程快照给到 AI；
  ③ 远程 get_process_list：回落到服务发现，不再直接返回“V1 不上报进程”；
  ④ recent_changes：变更单/处置审计聚合，并诚实标注 deployment_data=unavailable；
  ⑤ 证据回放：引用链接有 data-step、点击高亮、可查看完整工具输出。
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime

import pytest
from sqlalchemy import delete, text

from app.db.session import SessionLocal
from app.models.host import Host
from app.models.svc import Service
from app.services import rca_tools
from app.store import ring


@pytest.mark.asyncio
async def test_service_health_returns_metrics_and_log_path():
    ident = hashlib.sha256(b"p1-service-health").hexdigest()[:32]
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
        await db.execute(text("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        host = Host(ident=ident, name="P1-service", status="up", agent_token="t")
        db.add(host)
        await db.commit()
        await db.refresh(host)
        db.add(Service(host_id=host.id, name="mariadb", type="mysql", status="degraded",
                       note="Threads_running 偏高", metrics_json={"Threads_running": 48, "max_connections": 100},
                       spec_json={"unit": "mariadb", "log_path": "/var/log/mysql/error.log"},
                       last_check_at=datetime.utcnow(), enabled=True))
        await db.commit()
    try:
        r = await rca_tools.service_health(ident, service="mariadb")
        assert r["status"] == "SUCCESS"
        item = r["data"][0]
        assert item["status"] == "degraded"
        assert item["metrics"]["Threads_running"] == 48
        assert item["unit"] == "mariadb"
        assert item["log_path"] == "/var/log/mysql/error.log"
    finally:
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
            await db.execute(text("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()


@pytest.mark.asyncio
async def test_discovered_services_and_remote_process_list_from_ring():
    ident = hashlib.sha256(b"p1-discovered").hexdigest()[:32]
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        db.add(Host(ident=ident, name="P1-discovered", status="up", agent_token="t"))
        await db.commit()
    ring.set_services(ident, {
        "ports": [{"port": 3306, "addr": "0.0.0.0:3306", "proc": "mariadbd"}],
        "processes": [{"name": "mariadbd", "cpu": 88.5}],
    })
    try:
        r = await rca_tools.discovered_services(ident)
        assert r["status"] == "SUCCESS"
        assert r["data"]["ports"][0]["port"] == 3306
        assert r["data"]["processes"][0]["name"] == "mariadbd"
        assert r["data"]["at"]

        r = await rca_tools.get_process_list(ident, top_n=5)
        assert r["status"] == "SUCCESS"
        assert r["data"][0]["name"] == "mariadbd"
        assert "服务发现" in (r.get("note") or "")
    finally:
        ring.set_services(ident, {"ports": [], "processes": []})
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()


@pytest.mark.asyncio
async def test_recent_changes_aggregates_runbook_and_action_audit():
    ident = hashlib.sha256(b"p1-recent-changes").hexdigest()[:32]
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        host = Host(ident=ident, name="P1-changes", status="up", agent_token="t")
        db.add(host)
        await db.commit()
        await db.refresh(host)
        from app.models.ops import ActionAudit, Runbook

        rb = Runbook(name="重启 nginx", host_ident=ident, status="done", risk_level="mid",
                     execution_mode="manual", incident_id=1, steps=[], pre_checks=[],
                     rollback_plan="systemctl start nginx", verification="is-active")
        db.add(rb)
        await db.commit()
        await db.refresh(rb)
        db.add(ActionAudit(runbook_id=rb.id, host_ident=ident, action="restart_service",
                           params={"service": "nginx"}, result="ok", operator="user", status="success"))
        await db.commit()
    try:
        r = await rca_tools.recent_changes(ident, hours=24)
        assert r["status"] == "SUCCESS"
        assert r["data"]["summary"]["deployment_data"] == "unavailable"
        sources = {i["source"] for i in r["data"]["items"]}
        assert "runbook" in sources and "action_audit" in sources
        assert "发布" not in "".join(i["summary"] for i in r["data"]["items"])
    finally:
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM action_audit WHERE host_ident=:i"), {"i": ident})
            await db.execute(text("DELETE FROM runbooks WHERE host_ident=:i"), {"i": ident})
            await db.execute(text("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()


def test_incident_evidence_replay_is_wired():
    """P1-10：引用不是死链接——有 data-step、点击高亮、完整输出抽屉。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    js = (root / "static" / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    css = (root / "static" / "ops.css").read_text(encoding="utf-8")
    assert 'data-step' in js and 'cite-target' in js
    assert 'showStep' in js and '工具调用' in js
    assert 'tr.cite-target' in css
    assert '`#inc-steps [data-step="${n}"]`' in js


def test_agent_version_stale_and_hash_endpoint():
    """P1-12：版本漂移判定 + 安装件 SHA256 校验。"""
    import asyncio
    import hashlib
    from pathlib import Path
    from app.routers.agent_install import SRC_DIR, agent_code_sha256, agent_main_sha256
    from app.services import ingest

    assert ingest.is_agent_version_stale("0.3.0") is True
    assert ingest.is_agent_version_stale("0.3.1") is True
    # D121 起 stale 判定 = **文件指纹**优先；无指纹上报（旧 Agent）一律需要升级。
    serving = agent_code_sha256()
    # 指纹一致 → 不 stale（指纹是唯一判据，版本号仅展示）；指纹不同 → stale
    assert ingest.is_agent_version_stale("0.0.1", serving) is False
    assert ingest.is_agent_version_stale("0.0.1", "0" * 64) is True
    assert ingest.is_agent_version_stale(ingest.AGENT_LATEST_VERSION, serving) is False
    assert ingest.is_agent_version_stale(ingest.AGENT_LATEST_VERSION, "") is True  # 旧 Agent 无指纹 → 必须升一次
    assert ingest.is_agent_version_stale("") is True   # 未知版本也要提示升级
    digest = asyncio.run(agent_main_sha256()).body.decode().strip()
    assert digest == hashlib.sha256((SRC_DIR / "agent_main.py").read_bytes()).hexdigest()
    install = (Path(__file__).resolve().parents[2] / "agent" / "install.sh").read_text(encoding="utf-8")
    assert "agent_main.py.sha256" in install
    # D144：校验改为「计算下载件指纹并与平台哈希逐字比对」（sha_of），
    # 顺带用同一指纹做版本一致判定（一致 → 无需升级）。仍必须：有哈希来源、
    # 有指纹计算、校验失败时硬退出。
    assert "sha_of" in install
    assert "校验失败" in install
    assert "无需升级" in install


def test_hosts_ui_wires_upgrade_command():
    """P1-12：主机列表有“升级”入口，详情显示版本状态。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    hosts = (root / "static" / "js" / "pages" / "hosts.js").read_text(encoding="utf-8")
    detail = (root / "static" / "js" / "host_detail.js").read_text(encoding="utf-8")
    assert "upgradeGuide" in hosts and "version_stale" in hosts
    assert "可升级到" in hosts and "可升级到" in detail


def test_new_tools_are_registered_for_llm():
    """P1-5/6/7：新工具必须同时出现在 tool_specs 与 DISPATCH，否则模型看不到/调用不到。"""
    from app.services import rca_tools

    names = {t["function"]["name"] for t in rca_tools.tool_specs()}
    for n in ("service_health", "discovered_services", "recent_changes"):
        assert n in names, f"tool_specs 缺 {n}"
        assert n in rca_tools.DISPATCH, f"DISPATCH 缺 {n}"
