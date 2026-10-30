"""P2：日志路径自动发现（进程 → 打开的日志文件）回归锁。

设计口径：**发现 ≠ 采集**。
  · Agent 按 unit/port/process 定位 PID，从 /proc/<pid>/fd（或 lsof）、systemd
    StandardOutput/Error 收集"该进程打开的文件"，按日志相似度打分；
  · 候选必须**逐个通过路径白名单**才允许被拉取——白名单外的只记录拒绝原因；
  · 平台侧 fetch_remote_log 不带 path/unit 时走发现（Agent ≥0.3.2），发现到的路径
    回写为服务声明的"建议 log_path"（不覆盖已声明值，UI 一键采纳转正）。
"""
from __future__ import annotations

import re
import asyncio
import hashlib
import importlib.util
import json
import pathlib
import sys
import time
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENT_PY = ROOT / "agent" / "agent_main.py"


def _agent():
    spec = importlib.util.spec_from_file_location("agent_main_p2", AGENT_PY)
    m = importlib.util.module_from_spec(spec)
    sys.modules["agent_main_p2"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def utc_tz(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_discovery_scores_candidates_and_respects_whitelist(tmp_path, monkeypatch):
    """候选按"像日志"打分排序；白名单外的候选只能记录拒绝原因，绝不能被 chosen。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _agent()
    good = tmp_path / "redis.log"
    good.write_text("2026-10-18 10:00:00 [ERROR] boom\n", encoding="utf-8")
    secret = "/etc/shadow"

    monkeypatch.setattr(m, "_pids_for_target", lambda unit="", port=0, process="": [4242])
    monkeypatch.setattr(m, "_list_open_files",
                        lambda pid: [str(good), secret, "/tmp/gone.log (deleted)"])
    monkeypatch.setattr(m, "_systemd_log_files", lambda unit: [])

    r = m.discover_log_candidates({"discover": {"service": "redis", "port": 6379}})
    assert r["chosen"] == str(good), r
    paths = {c["path"]: c for c in r["candidates"]}
    assert paths[str(good)]["allowed"] is True and paths[str(good)]["score"] > 0
    assert paths[secret]["allowed"] is False and paths[secret]["score"] <= 0
    assert "白名单" in paths[secret]["deny_reason"] or "允许" in paths[secret]["deny_reason"]
    # 已删除句柄要被明显降权
    assert paths["/tmp/gone.log (deleted)"]["score"] < paths[str(good)]["score"]


def test_fetch_log_discovery_fetches_chosen_file(tmp_path, monkeypatch, utc_tz):
    """fetch_log 不带 path/unit：走发现 → chosen 过白名单 → 正常走筛选/折叠流水线。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _agent()
    log = tmp_path / "app.log"
    log.write_text(
        "2026-10-18 10:00:01 [INFO] ok\n"
        "2026-10-18 10:00:02 [ERROR] connection refused\n"
        "2026-10-18 10:00:03 [ERROR] connection refused\n",
        encoding="utf-8")

    monkeypatch.setattr(m, "discover_log_candidates", lambda payload: {
        "service": "app", "unit": "", "process": "order", "port": 8080,
        "pids": [7], "file_count": 3, "candidates": [{"path": str(log), "score": 9, "allowed": True}],
        "chosen": str(log)})

    r = m.fetch_log({"discover": {"service": "app", "process": "order", "port": 8080},
                     "since": "2026-10-18T10:00:00Z", "service_type": "app"})
    assert r["ok"] is True and r["segments"], r
    assert r["discovered"]["chosen"] == str(log)
    assert any("connection refused" in ln for s in r["segments"] for ln in s["lines"])
    assert r["deduped"] >= 1


def test_fetch_log_discovery_failures_are_actionable(tmp_path, monkeypatch):
    """发现失败必须"可行动"：说明找到什么/为什么不能用/下一步怎么办。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _agent()

    # ① 什么都没找到
    monkeypatch.setattr(m, "discover_log_candidates", lambda p: {
        "service": "x", "candidates": [], "chosen": "", "pids": []})
    r = m.fetch_log({"discover": {"service": "x"}, "since": "2026-10-18T10:00:00Z"})
    assert r["ok"] is False and "自动发现" in r["error"]

    # ② 找到了但全被白名单拒绝 → denied 标记 + 指路 AIOPS_LOG_DIRS
    monkeypatch.setattr(m, "discover_log_candidates", lambda p: {
        "service": "x", "candidates": [{"path": "/etc/shadow", "score": 3, "allowed": False,
                                        "deny_reason": "不在白名单"}], "chosen": "", "pids": [1]})
    r = m.fetch_log({"discover": {"service": "x"}, "since": "2026-10-18T10:00:00Z"})
    assert r["ok"] is False and r.get("denied") is True
    assert "AIOPS_LOG_DIRS" in r["error"]

    # ③ 没有 discover 信息 → 维持原契约错误
    r = m.fetch_log({})
    assert r["ok"] is False and "path 或 unit" in r["error"]


def test_discovery_helpers_fail_soft():
    """发现函数 fail-soft：进程不存在/端口没人听 → 空候选，绝不抛异常。"""
    m = _agent()
    r = m.discover_log_candidates({"discover": {"service": "nope",
                                                "process": "definitely-not-running-xyz"}})
    assert r["chosen"] == "" and r["candidates"] == []
    assert m._list_open_files(999999) == []     # 不存在的 PID


# ════════════════ 平台侧：无 path/unit 自动发现 + 建议回写 ════════════════
@pytest.mark.asyncio
async def test_fetch_remote_log_auto_discovers_and_writes_suggestion(monkeypatch):
    from httpx import ASGITransport  # noqa: F401  （确保 app 依赖已加载）
    from sqlalchemy import delete, select, text as sqltext

    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.models.svc import AgentTask, Service
    from app.services import rca_tools

    ident = hashlib.sha256(b"p2-log-discovery").hexdigest()[:32]
    captured: dict = {}

    async def fake_enqueue(db, host, kind, payload, origin="", created_by=""):
        captured["payload"] = payload
        t = AgentTask(host_id=host.id, kind=kind, payload_json=payload, status="done",
                      result_json={"ok": True, "bytes": 120, "scanned": 30, "matched": 2,
                                   "window": {"source": "timestamp", "since": "", "until": ""},
                                   "segments": [{"signature": "error", "count": 2, "first_ts": "",
                                                 "last_ts": "", "lines": ["x ERROR y"], "repeats": 0}],
                                   "discovered": {"service": "redis", "pids": [9], "file_count": 2,
                                                  "candidates": [{"path": "/var/log/redis/redis.log",
                                                                  "score": 9, "allowed": True}],
                                                  "chosen": "/var/log/redis/redis.log"}},
                      done_at=datetime.utcnow())
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t

    monkeypatch.setattr("app.routers.tasks.enqueue", fake_enqueue)
    rca_tools.reset_log_budget("p2-disc-test")

    async with SessionLocal() as db:
        await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
        await db.execute(sqltext("DELETE FROM agent_tasks WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
        await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        host = Host(ident=ident, name="P2-disc", status="up", agent_token="t", agent_version="0.3.2", last_beat=datetime.utcnow())
        db.add(host)
        await db.commit()
        await db.refresh(host)
        db.add(Service(host_id=host.id, name="redis", type="redis", status="unknown",
                       spec_json={"port": 6379, "unit": "redis"}, enabled=True))
        await db.commit()
    try:
        # ① 不带 path/unit → 自动发现
        r = await rca_tools.fetch_remote_log(ident, since="2026-10-18T09:00:00Z",
                                             budget_key="p2-disc-test", service_type="redis")
        assert r["status"] == "SUCCESS", r
        assert "自动发现" in r["data"]["path"] and "redis.log" in r["data"]["path"]
        assert r["data"]["discovered"]["chosen"] == "/var/log/redis/redis.log"
        # 下发给 Agent 的载荷带 discover 定位信息（不带 path/unit）
        assert captured["payload"]["discover"]["service"] == "redis"
        assert captured["payload"]["discover"]["port"] == 6379
        assert not captured["payload"]["path"] and not captured["payload"]["unit"]
        # ② 建议路径已回写到服务声明
        async with SessionLocal() as db:
            svc = (await db.execute(select(Service).where(Service.name == "redis"))).scalars().first()
            assert svc.spec_json.get("suggested_log_path") == "/var/log/redis/redis.log"
    finally:
        async with SessionLocal() as db:
            await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
            await db.execute(sqltext("DELETE FROM agent_tasks WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
            await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()


@pytest.mark.asyncio
async def test_fetch_remote_log_discovery_version_gate(monkeypatch):
    """Agent < 0.3.2 时不走发现，明确提示升级（而不是让 Agent 报格式错）。"""
    from sqlalchemy import text as sqltext

    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.services import rca_tools

    ident = hashlib.sha256(b"p2-disc-oldagent").hexdigest()[:32]
    rca_tools.reset_log_budget("p2-old-agent")
    async with SessionLocal() as db:
        await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        db.add(Host(ident=ident, name="P2-old", status="up", agent_token="t", agent_version="0.3.1", last_beat=datetime.utcnow()))
        await db.commit()
    try:
        r = await rca_tools.fetch_remote_log(ident, budget_key="p2-old-agent")
        assert r["status"] == "NO_DATA" and "0.3.2" in (r.get("note") or "")
    finally:
        async with SessionLocal() as db:
            await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()


def test_log_discovery_wired_everywhere():
    """守卫：提示词/工具描述/UI 三处都要挂上自动发现（修了没锁等于没修）。"""
    rca = (ROOT / "backend" / "app" / "services" / "rca_tools.py").read_text(encoding="utf-8")
    prompts = (ROOT / "backend" / "app" / "services" / "prompts.py").read_text(encoding="utf-8")
    rca_py = (ROOT / "backend" / "app" / "services" / "rca.py").read_text(encoding="utf-8")
    svc_js = (ROOT / "static" / "js" / "pages" / "services.js").read_text(encoding="utf-8")
    agent = AGENT_PY.read_text(encoding="utf-8")

    assert "自动发现" in rca and "suggested_log_path" in rca
    assert "自动发现" in prompts and "自动发现" in rca_py
    assert "建议日志路径" in svc_js and "suggested_log_path" in svc_js
    assert "discover_log_candidates" in agent and re.search(r'AGENT_VERSION\s*=\s*"[^"]+"', agent)
    # 工具描述必须说明 path/unit 可留空
    assert "可留空" in rca


def test_noise_candidates_do_not_block_journald_fallback(tmp_path, monkeypatch, utc_tz):
    """/dev/null 等 0 分噪音不得挡住 journald 回落；只有日志特征文件被白名单拒才阻断。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _agent()

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = "2026-10-18T10:00:01 redis[1]: OOM command not allowed when used memory > 'maxmemory'\n"
            stderr = ""
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "discover_log_candidates", lambda p: {
        "service": "redis", "unit": "redis", "process": "redis", "port": 6379,
        "pids": [5], "file_count": 1,
        "candidates": [{"path": "/dev/null", "score": 0, "allowed": False, "deny_reason": "白名单"}],
        "chosen": ""})

    r = m.fetch_log({"discover": {"service": "redis", "port": 6379, "unit": "redis"},
                     "since": "2026-10-18T10:00:00Z", "until": "2026-10-18T10:01:00Z",
                     "service_type": "redis"})
    assert r["ok"] is True and r["segments"], r
    assert r["discovered"]["fallback"] == "journald"
    assert r["window"]["source"] == "journalctl"
    assert any("OOM command not allowed" in ln for s in r["segments"] for ln in s["lines"])


@pytest.mark.asyncio
async def test_service_log_fetch_endpoint_discovers_when_undeclared(tmp_path, monkeypatch):
    """UI「取日志」：未声明 log_path 的服务不再 400，而是走自动发现下发。"""
    import hashlib

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select, text as sqltext

    from app.db.session import SessionLocal
    from app.main import create_app
    from app.models.host import Host
    from app.models.svc import Service

    declared = tmp_path / "declared.log"
    declared.write_text("2026-10-18 10:00:00 [ERROR] x\n", encoding="utf-8")
    ident = hashlib.sha256(b"p2-svc-endpoint-disc").hexdigest()[:32]
    captured: dict = {}

    async def fake_enqueue(db, host, kind, payload, origin="", created_by=""):
        captured["payload"] = payload
        from app.models.svc import AgentTask

        t = AgentTask(host_id=host.id, kind=kind, payload_json=payload, status="pending")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t

    monkeypatch.setattr("app.routers.tasks.enqueue", fake_enqueue)
    app = create_app()
    async with SessionLocal() as db:
        await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
        await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        host = Host(ident=ident, name="P2-svc-disc", status="up", agent_token="t", agent_version="0.3.2", last_beat=datetime.utcnow())
        db.add(host)
        await db.commit()
        await db.refresh(host)
        db.add(Service(host_id=host.id, name="前端验证-temp", type="app", status="unknown",
                       spec_json={"port": 80, "process": "nginx"}, enabled=True))
        await db.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            lg = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            H = {"Authorization": f"Bearer {lg.json()['data']['access_token']}"}
            r = await ac.post("/api/ops/services?ident=none", headers=H)  # 占位，防误删
            sid = (await db.execute(select(Service).where(Service.name == "前端验证-temp"))).scalars().first().id
            resp = await ac.post(f"/api/ops/services/{sid}/log-fetch", headers=H,
                                 json={"window_min": 30})
            assert resp.status_code == 200, resp.text[:200]
            data = resp.json()["data"]
            assert data["discover"] is True
            assert captured["payload"]["discover"]["process"] == "nginx"
            assert not captured["payload"]["path"] and not captured["payload"]["unit"]
            # 声明后回到原路径（不再带 discover）
            await ac.patch(f"/api/ops/services/{sid}", headers=H, json={"log_path": str(declared)})
            resp2 = await ac.post(f"/api/ops/services/{sid}/log-fetch", headers=H,
                                  json={"window_min": 30})
            assert resp2.status_code == 200
            assert resp2.json()["data"]["discover"] is False
    finally:
        async with SessionLocal() as db:
            await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
            await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_task_result_writes_suggested_log_path():
    """回写统一在任务结果回传处：UI「取日志」（origin=service:N）发现成功 → 服务声明得到建议路径。"""
    import hashlib

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select, text as sqltext

    from app.db.session import SessionLocal
    from app.main import create_app
    from app.models.host import Host
    from app.models.svc import AgentTask, Service

    ident = hashlib.sha256(b"p2-task-result-sugg").hexdigest()[:32]
    app = create_app()
    async with SessionLocal() as db:
        await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
        await db.execute(sqltext("DELETE FROM agent_tasks WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
        await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        host = Host(ident=ident, name="P2-sugg", status="up", agent_token="tok-p2")
        db.add(host)
        await db.commit()
        await db.refresh(host)
        svc = Service(host_id=host.id, name="无声明服务", type="app", status="unknown",
                      spec_json={"port": 80, "process": "nginx"}, enabled=True)
        db.add(svc)
        await db.commit()
        await db.refresh(svc)
        task = AgentTask(host_id=host.id, kind="log_fetch", status="picked",
                         payload_json={"discover": {"service": svc.name, "port": 80, "process": "nginx"}},
                         origin=f"service:{svc.id}")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        tid, sid = task.id, svc.id
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post(f"/api/ops/agents/tasks/{tid}/result",
                              json={"result": {"ok": True, "bytes": 10,
                                               "discovered": {"service": "无声明服务", "pids": [3],
                                                              "candidates": [{"path": "/var/log/nginx/error.log",
                                                                              "score": 9, "allowed": True}],
                                                              "chosen": "/var/log/nginx/error.log"}}},
                              headers={"X-Ident": ident, "X-Token": "tok-p2"})
            assert r.status_code == 200, r.text[:200]
        async with SessionLocal() as db:
            s = await db.get(Service, sid)
            assert s.spec_json.get("suggested_log_path") == "/var/log/nginx/error.log"
    finally:
        async with SessionLocal() as db:
            await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
            await db.execute(sqltext("DELETE FROM agent_tasks WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
            await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()
        app.dependency_overrides.clear()


# ════════════════ 离线短路（用户实测报障：服务器下线后取证白等 40s+）════════════════
@pytest.mark.asyncio
async def test_log_fetch_rejects_immediately_when_host_offline():
    """UI「取日志」：主机离线 → 409 + 明确说"任务未下发"，绝不进入 60s 等待。"""
    import hashlib

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text as sqltext

    from app.db.session import SessionLocal
    from app.main import create_app
    from app.models.host import Host
    from app.models.svc import Service

    ident = hashlib.sha256(b"p2-offline-host").hexdigest()[:32]
    app = create_app()
    async with SessionLocal() as db:
        await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
        await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        db.add(Host(ident=ident, name="P2-offline", status="down", agent_token="t",
                    last_beat=datetime.utcnow()))
        await db.commit()
        await db.refresh(Host.__table__) if False else None
        host_id = (await db.execute(sqltext("SELECT id FROM hosts WHERE ident=:i"), {"i": ident})).scalar()
        db.add(Service(host_id=host_id, name="离线服务", type="app", status="unknown",
                       spec_json={"log_path": "/var/log/app.log"}, enabled=True))
        await db.commit()
        sid = (await db.execute(sqltext("SELECT id FROM services WHERE name='离线服务'"))).scalar()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            lg = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            H = {"Authorization": f"Bearer {lg.json()['data']['access_token']}"}
            r = await ac.post(f"/api/ops/services/{sid}/log-fetch", headers=H, json={"window_min": 5})
            assert r.status_code == 409, r.text[:200]
            assert "离线" in r.json()["msg"] and "未下发" in r.json()["msg"]
    finally:
        async with SessionLocal() as db:
            await db.execute(sqltext("DELETE FROM services WHERE host_id IN (SELECT id FROM hosts WHERE ident=:i)"), {"i": ident})
            await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_fetch_remote_log_short_circuits_when_host_offline():
    """AI 取证工具：主机离线 → 立即 NO_DATA（含心跳时长），不烧 45s 等待与预算。"""
    import hashlib

    from sqlalchemy import text as sqltext

    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.services import rca_tools

    ident = hashlib.sha256(b"p2-offline-ai").hexdigest()[:32]
    rca_tools.reset_log_budget("p2-offline")
    async with SessionLocal() as db:
        await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        db.add(Host(ident=ident, name="P2-offline-ai", status="down", agent_token="t",
                    agent_version="0.3.2", last_beat=datetime.utcnow()))
        await db.commit()
    try:
        r = await rca_tools.fetch_remote_log(ident, budget_key="p2-offline")
        assert r["status"] == "NO_DATA"
        assert "离线" in (r.get("note") or "") and "未下发" in (r.get("note") or "")
    finally:
        async with SessionLocal() as db:
            await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()


@pytest.mark.asyncio
async def test_fetch_remote_log_bails_out_when_host_dies_midwait(monkeypatch):
    """等待期间主机掉线 → 立即早退（不等满 45s）。fake 下发时把主机置为离线。"""
    import hashlib

    from sqlalchemy import text as sqltext

    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.services import rca_tools

    ident = hashlib.sha256(b"p2-dies-midwait").hexdigest()[:32]
    rca_tools.reset_log_budget("p2-midwait")

    async def fake_enqueue(db, host, kind, payload, origin="", created_by=""):
        # 模拟"下发后主机立刻失联"：心跳拨回 10 分钟前 + 状态 down
        await db.execute(sqltext(
            "UPDATE hosts SET status='down', last_beat=:lb WHERE id=:i"),
            {"lb": datetime.utcnow() - timedelta(minutes=10), "i": host.id})
        await db.commit()
        from app.models.svc import AgentTask

        t = AgentTask(host_id=host.id, kind=kind, payload_json=payload, status="pending")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t

    monkeypatch.setattr("app.routers.tasks.enqueue", fake_enqueue)
    async with SessionLocal() as db:
        await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
        db.add(Host(ident=ident, name="P2-midwait", status="up", agent_token="t",
                    agent_version="0.3.2", last_beat=datetime.utcnow()))
        await db.commit()
    try:
        t0 = time.time()
        r = await rca_tools.fetch_remote_log(ident, path="/var/log/app.log", budget_key="p2-midwait")
        elapsed = time.time() - t0
        assert r["status"] == "NO_DATA" and "离线" in (r.get("note") or ""), r
        assert elapsed < 10, f"应在数秒内早退，实际等了 {elapsed:.1f}s"
    finally:
        async with SessionLocal() as db:
            await db.execute(sqltext("DELETE FROM hosts WHERE ident=:i"), {"i": ident})
            await db.commit()
