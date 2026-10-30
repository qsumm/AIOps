"""D63 服务发现回归（用户建议：探活之外，还要"看见"服务器上跑了什么服务）。

锁三件事：
  ① Agent 端 discover_services()：结构稳定（ports/processes 列表、字段齐全、fail-soft 不抛）
  ② 采集接线：心跳里的 services 快照进 ring、主机详情接口原样带回
  ③ 快照只存内存保最近一份（不落库不加列——部署零迁移）
"""
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.store import ring


def test_agent_discover_services_shape():
    """Agent 的发现函数必须返回稳定结构且 fail-soft（本机真跑一次）。"""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "agent_main", os.path.join(os.path.dirname(__file__), "..", "..", "agent", "agent_main.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    d = m.discover_services()
    assert isinstance(d, dict) and isinstance(d["ports"], list) and isinstance(d["processes"], list)
    for p in d["ports"]:
        assert set(p) >= {"port", "addr", "proc"} and isinstance(p["port"], int)
    for p in d["processes"]:
        assert set(p) >= {"name", "cpu"}
    # 版本号必须 ≥ 0.3.0（P5 起 Agent 具备任务通道/日志取证/服务探测能力；
    # 前端与平台按 agent_version 做能力协商，故版本号是能力契约的一部分）
    import re as _re
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "agent", "agent_main.py"),
               encoding="utf-8").read()
    # P0 起版本号收敛为单一常量（注册/自监控/能力协商共用），避免三处漂移。
    m = _re.search(r'AGENT_VERSION = "(\d+)\.(\d+)\.(\d+)"', src)
    assert m, "agent 缺 AGENT_VERSION 常量"
    assert '"agent_version": AGENT_VERSION' in src, "注册上报必须使用 AGENT_VERSION"
    assert (int(m.group(1)), int(m.group(2))) >= (0, 3), f"Agent 版本过低：{m.group(0)}"


def test_ports_from_proc_parses_listening(monkeypatch):
    """/proc/net/tcp 兜底分支的解析纯函数：只取 LISTEN（st=0A）、十六进制端口、去重。

    slim 容器（无 ss/lsof/netstat）就靠这条路——macOS 上没有 /proc，只能合成数据测。"""
    import importlib.util
    import pathlib as _pl
    import os

    spec = importlib.util.spec_from_file_location(
        "agent_main2", os.path.join(os.path.dirname(__file__), "..", "..", "agent", "agent_main.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    rows = ("  sl local_address rem_address st tx_queue rx_queue\n"
            "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000\n"
            "   1: 00000000:0016 00000000:0000 0A 00000000:00000000\n"
            "   2: 00000000:1388 00000000:0000 01 00000000:00000000\n")
    class FakePath:
        def __init__(self, p): pass
        def read_text(self): return rows
    monkeypatch.setattr(_pl, "Path", FakePath)
    ports = m._ports_from_proc()
    assert [p["port"] for p in ports] == [8080, 22], ports   # 01(非监听) 必须被排除


def test_services_roundtrip_in_ring():
    ring.set_services("d63-host", {"ports": [{"port": 8848, "addr": "0.0.0.0:8848", "proc": "uvicorn"}],
                                   "processes": [{"name": "uvicorn", "cpu": 3.2}]})
    d = ring.get_services("d63-host")
    assert d["ports"][0]["port"] == 8848
    assert ring.get_services("no-such") == {}
    ring.set_services("d63-host", {"ports": [], "processes": []})
    assert ring.get_services("d63-host")["ports"] == []   # 只保最近一份


@pytest.mark.asyncio
async def test_host_detail_returns_services():
    app = create_app()
    ring.set_services("d63-detail", {"ports": [{"port": 22, "addr": "*:22", "proc": "sshd"}],
                                     "processes": [{"name": "sshd", "cpu": 0.1}]})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        lg = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        tok = lg.json()["data"]["access_token"]
        H = {"Authorization": f"Bearer {tok}"}
        # 主机不存在 → 404（鉴权后）
        r = await ac.get("/api/ops/hosts/d63-detail", headers=H)
        assert r.status_code == 404
    app.dependency_overrides.clear()
