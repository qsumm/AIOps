"""D121 Agent 自升级回归锁：版本比对防降级、SHA256 校验拒坏文件、原子替换、
重启命令触发（systemd/launchd 分支）、无服务托管时明确失败回退命令。"""
import hashlib
import sys
import os
import pytest


def _load_agent():
    import importlib.util
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "agent", "agent_main.py"))
    spec = importlib.util.spec_from_file_location("agent_main_d121", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("agent_main_d121", m)
    spec.loader.exec_module(m)
    return m


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Resp:
    def __init__(self, data: bytes):
        self._d = data

    def read(self) -> bytes:
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_upgrade_rejects_bad_sha_and_keeps_file(tmp_path, monkeypatch):
    m = _load_agent()
    good = b"# agent main new version\n"
    bad = b"# corrupted\n"
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(bad), raising=False)
    # 模拟 agent 文件位于 tmp
    fake = tmp_path / "agent_main.py"
    fake.write_bytes(b"# old\n")
    monkeypatch.setattr(m.Path, "resolve", lambda self: tmp_path / "agent_main.py", raising=False)
    r = m.self_upgrade({"version": "9.9.9", "sha256": _sha(good)}, server="http://x")
    assert r["ok"] is False and "SHA256" in r["error"]
    assert fake.read_bytes() == b"# old\n"                       # 原文件未被破坏
    assert not (tmp_path / "agent_main.py.new").exists()          # 坏临时文件已清理


def test_upgrade_success_replaces_and_restarts(tmp_path, monkeypatch):
    m = _load_agent()
    new_code = b"# agent main vNEXT\n"
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(new_code), raising=False)
    fake = tmp_path / "agent_main.py"
    fake.write_bytes(b"# old\n")
    calls = []
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda cmd, **k: calls.append(cmd) or _Stub(), raising=False)
    monkeypatch.setattr(m.Path, "resolve", lambda self: tmp_path / "agent_main.py", raising=False)
    monkeypatch.setattr(m.Path, "exists", lambda self: self == m.Path("/run/systemd/system"), raising=False)
    r = m.self_upgrade({"version": "9.9.9", "sha256": _sha(new_code)}, server="http://x")
    assert r["ok"] is True
    assert fake.read_bytes() == new_code                          # 原子替换生效
    assert calls and calls[0][:3] == ["systemctl", "restart", "opsagent"]
    assert not (tmp_path / "agent_main.py.new").exists()


def test_upgrade_skips_only_when_code_hash_matches(tmp_path, monkeypatch):
    """D125：跳过条件 = **文件指纹一致**（与平台侧 stale 判定同口径）；
    同版本号但内容已变 → 必须真升级（否则新能力永远推不下去）。"""
    m = _load_agent()
    called = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: called.append(1), raising=False)
    # 指纹一致 → 跳过（即便版本号相同）
    monkeypatch.setattr(m, "_code_sha256", lambda: "a" * 64, raising=False)
    r = m.self_upgrade({"version": m.AGENT_VERSION, "sha256": "a" * 64}, server="http://x")
    assert r["ok"] is True and "跳过" in r.get("note", "")
    assert not called, "指纹一致时不得发起下载"
    # 指纹不同（同版本号）→ 不得跳过，要真升级
    monkeypatch.setattr(m, "_code_sha256", lambda: "b" * 64, raising=False)
    r2 = m.self_upgrade({"version": m.AGENT_VERSION, "sha256": "a" * 64}, server="http://x")
    assert "跳过" not in r2.get("note", ""), "同版本号但指纹不同必须真升级"


def test_upgrade_refuses_downgrade(tmp_path, monkeypatch):
    """平台版本低于本机 → 拒绝降级。"""
    m = _load_agent()
    monkeypatch.setattr(m, "_code_sha256", lambda: "b" * 64, raising=False)
    r = m.self_upgrade({"version": "0.0.1", "sha256": "a" * 64}, server="http://x")
    assert r["ok"] is False and "降级" in r["error"]


def test_upgrade_fails_without_service_manager(tmp_path, monkeypatch):
    m = _load_agent()
    new_code = b"# vNEXT\n"
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(new_code), raising=False)
    (tmp_path / "agent_main.py").write_bytes(b"# old\n")
    monkeypatch.setattr(m.Path, "resolve", lambda self: tmp_path / "agent_main.py", raising=False)
    monkeypatch.setattr(m.Path, "exists", lambda self: False, raising=False)   # 无 systemd/launchd
    r = m.self_upgrade({"version": "9.9.9", "sha256": _sha(new_code)}, server="http://x")
    assert r["ok"] is False and "systemd/launchd" in r["error"]   # 明确回退升级命令


class _Stub:
    def poll(self):
        return None


@pytest.mark.asyncio
async def test_upgrade_endpoint_dispatches_task_and_demo_denied():
    """平台端点：admin 下发 pending 任务（payload 带 sha256）；demo/write 矩阵 403/401。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.main import app
    from app.models.host import Host
    from app.models.svc import AgentTask
    from app.services import ingest

    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(db, {"fingerprint": "test-fp-d121e", "hostname": "d121e",
                                                "install_token": tok, "ip": "9.9.9.9"})
        ident = data["ident"]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            r = await ac.post(f"/api/ops/hosts/{ident}/upgrade", json={}, headers=hdr)
            assert r.status_code == 200, r.text
            payload = r.json()["data"]
            assert payload.get("task_id"), payload
            # 任务已落库，payload 带 sha256 + version
            async with SessionLocal() as db:
                t = (await db.execute(select(AgentTask).where(
                    AgentTask.kind == "self_upgrade"))).scalars().all()
                assert any(t_.payload_json.get("sha256") and t_.payload_json.get("version")
                           for t_ in t), "任务 payload 必须带 sha256 与 version"
            # demo 登录 → DENY
            dl = await ac.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
            if dl.status_code == 200:
                dh = {"Authorization": f"Bearer {dl.json()['data']['access_token']}"}
                r2 = await ac.post(f"/api/ops/hosts/{ident}/upgrade", json={}, headers=dh)
                assert r2.status_code == 403
    finally:
        async with SessionLocal() as db:
            h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
            if h is not None:
                tasks = (await db.execute(select(AgentTask).where(AgentTask.host_id == h.id))).scalars().all()
                for t in tasks:
                    await db.delete(t)
                await db.delete(h)
                await db.commit()


def test_stale_by_code_hash():
    """版本漂移判定（D121）：上报 hash == 平台 serving hash → 不 stale；不同 → stale；无 hash 回落版本比较。"""
    from app.services import ingest

    from app.routers.agent_install import agent_code_sha256
    serving = agent_code_sha256()
    assert ingest.is_agent_version_stale(ingest.AGENT_LATEST_VERSION, serving) is False
    assert ingest.is_agent_version_stale(ingest.AGENT_LATEST_VERSION, "0" * 64) is True
    assert ingest.is_agent_version_stale("0.0.1", "") is True          # 旧 agent（无指纹）→ 必升
    assert ingest.is_agent_version_stale(ingest.AGENT_LATEST_VERSION, "") is True  # 版本号相同也升（获得自升级能力）


def test_unknown_kind_with_upgrade_payload_falls_back():
    """D121 兜底：未知 kind 但载荷带 sha256+version → 走自升级（协议演进前向兼容）。"""
    m = _load_agent()
    new_code = b"# vFUTURE\n"
    calls = []
    monkey_data = new_code

    class R:
        def read(self):
            return monkey_data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: R()
    fake = m.Path("/tmp/d121-fallback/agent_main.py")
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"# old\n")
    orig_resolve = m.Path.resolve
    m.Path.resolve = lambda self: fake
    orig_exists = m.Path.exists
    m.Path.exists = lambda self: self == m.Path("/run/systemd/system")

    class P:
        """Popen 替身。

        注意两点（都是踩过的坑）：
          ① 必须**返回对象**——原实现 `lambda cmd, **k: calls.append(cmd)` 返回的是
             `list.append` 的返回值 **None**，而 `subprocess.run` 内部是
             `with Popen(...) as process:` → None 不支持上下文管理器 → TypeError；
          ② 必须**在 finally 里还原**——`m.subprocess` 就是全局 subprocess 模块，
             裸赋值永不复原，会把整个会话里后续所有用 subprocess 的测试打坏
             （D130 查案实录：这条曾让 D3 前端语法门禁在完整回归里静默失效）。
        """
        def __init__(self, cmd, *a, **k):
            calls.append(cmd)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def poll(self):
            return None

        def wait(self, *a, **k):
            return 0

    orig_popen = m.subprocess.Popen
    m.subprocess.Popen = P
    try:
        r = m.execute_task({"kind": "brand_new_kind",
                            "payload": {"version": "9.9.9", "sha256": _sha(new_code)}}, "http://x")
    finally:
        urllib.request.urlopen = orig
        m.Path.resolve = orig_resolve
        m.Path.exists = orig_exists
        m.subprocess.Popen = orig_popen   # ← 漏还原会污染全局 subprocess（见下方守卫）
    assert r["ok"] is True, r
    assert fake.read_bytes() == new_code
    assert calls and calls[0][0] == "systemctl"


def test_unknown_kind_without_upgrade_payload_rejected():
    """无升级载荷的未知 kind 仍拒绝（不把任意任务误当升级）。"""
    m = _load_agent()
    r = m.execute_task({"kind": "mystery", "payload": {"x": 1}}, "http://x")
    assert r["ok"] is False and "未知任务类型" in r["error"]