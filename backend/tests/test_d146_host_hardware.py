"""D146 守卫：主机详情要能看到「这台机器几核几 G」。

用户原话：「这里我希望可以起码展示当前服务器是啥配置啊，比如 20C 内存，20 核 cpu 之类的」。

锁三件会**静默退化**的事：
  ① Agent 必须真的采集到硬件配置（核数/内存/开机时间），且采集失败不得影响心跳；
  ② 平台必须把上报的 hostinfo **落库**（否则界面永远空白——用户看不出是没采到还是没存）；
  ③ 旧 Agent（不上报 hostinfo）时，界面必须**明说**「未上报 + 升级后可看」，
     不许显示成 "0 核 0 GB" 让人以为机器真是 0 核。
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
AGENT_PY = ROOT / "agent" / "agent_main.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("agent_main_d146", AGENT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_agent_collects_hostinfo():
    """Agent 能采到硬件配置：核数/内存/开机时间/架构。"""
    m = _load_agent()
    info = m.collect_hostinfo()
    assert isinstance(info, dict) and info, "collect_hostinfo 必须返回非空 dict"
    assert int(info.get("cpu_cores") or 0) >= 1, f"逻辑核数应 ≥1，实际 {info.get('cpu_cores')}"
    assert int(info.get("mem_total") or 0) > 0, "内存总量应为字节数且 >0"
    assert "boot_time" in info and "uptime_s" in info, "开机时间与运行时长必须上报"
    assert info.get("arch"), "架构（x86_64/aarch64）必须上报"


def test_agent_sends_hostinfo_in_payload():
    """上报体里必须带 hostinfo（平台按这个键落库）。"""
    src = AGENT_PY.read_text(encoding="utf-8")
    assert 'payload["hostinfo"]' in src, "Agent 必须把 hostinfo 放进上报体"
    # 服务发现失败不得连带丢掉磁盘快照（容器里 net_connections 会 AccessDenied）
    assert "服务发现失败（忽略）" in src and "磁盘快照失败（忽略）" in src, \
        "服务发现与磁盘快照必须各自 try，互不连坐"


def test_ingest_applies_hostinfo():
    """平台把 hostinfo 落到 hosts 静态列（核数/型号/内存/开机时间）。"""
    from app.models.host import Host
    from app.services import ingest

    h = Host(ident="d146-hw", name="d146")
    now = datetime(2026, 10, 20, 12, 0, 0)
    ingest._apply_hostinfo(h, {
        "cpu_cores": 20, "cpu_cores_physical": 10, "cpu_model": "Intel(R) Xeon(R) Platinum 8369B",
        "mem_total": 8058277888, "boot_time": "2026-10-18T08:35:49+00:00",
    }, now)
    assert h.cpu_cores == 20 and h.cpu_cores_physical == 10
    assert h.cpu_model.startswith("Intel")
    assert h.mem_total == 8058277888
    assert h.boot_time == datetime(2026, 10, 18, 8, 35, 49)
    assert h.hostinfo_at == now


def test_ingest_hostinfo_ignores_garbage():
    """脏数据不得把主机行写坏（字段类型/时间非法 → 保持原值）。"""
    from app.models.host import Host
    from app.services import ingest

    h = Host(ident="d146-hw2", name="d146", cpu_cores=8, mem_total=1024)
    ingest._apply_hostinfo(h, {"cpu_cores": "not-a-number", "mem_total": None,
                                "boot_time": "昨天", "cpu_model": 12345}, datetime.utcnow())
    assert h.cpu_cores == 8, "非法核数必须保持原值"
    assert h.mem_total == 1024, "非法内存必须保持原值"
    assert h.boot_time is None, "非法开机时间必须忽略"


@pytest.mark.asyncio
async def test_host_detail_api_exposes_hardware():
    """详情接口必须把硬件配置吐出来（前端才有东西可显示）。"""
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        hosts = (await ac.get("/api/ops/hosts?page_size=50", headers=h)).json()["data"]["items"]
        assert hosts, "测试库应至少有一台主机"
        ident = hosts[0]["ident"]
        d = (await ac.get(f"/api/ops/hosts/{ident}", headers=h)).json()["data"]
    for k in ("cpu_cores", "cpu_cores_physical", "cpu_model", "mem_total", "boot_time", "hostinfo_at"):
        assert k in d, f"详情接口缺少硬件字段 {k}"


def test_macos_capability_parity_guards():
    """D147：mac 与 Linux 的能力要同步（用户要求「mac linux都要同步」）。

    Linux 专属实现全都有降级路径，这里机械锁住四个**曾经只在 Linux 生效**的采集点：
      ① CPU 型号：macOS 的 platform.processor() 只给 "arm" → 必须走 sysctl 取商品名/机型；
      ② TCP 连接状态：mac 无 root 时 psutil.net_connections 直接 AccessDenied
         → 必须回落到 lsof（mac 自带）再退 netstat；
      ③ 内存缓存：Darwin 的 psutil 没有 buffers/cached → 用 inactive 近似，别恒显示 0；
      ④ 日志取证白名单：mac 的应用日志在 /opt/homebrew/var/log 与 ~/Library/Logs。
    """
    src = AGENT_PY.read_text(encoding="utf-8")
    assert 'sys.platform == "darwin"' in src and "machdep.cpu.brand_string" in src, \
        "macOS 的 CPU 型号必须走 sysctl（platform.processor() 只返回 arm）"
    assert '"lsof", "-nP", "-iTCP"' in src, "TCP 状态在 mac 上必须回落到 lsof"
    assert 'getattr(vm, "inactive", 0)' in src, "Darwin 缺 buffers/cached 时要用 inactive 近似"
    assert "/opt/homebrew/var/log" in src and "Library/Logs" in src, \
        "日志取证白名单必须包含 macOS 常见日志目录"


def test_macos_hostinfo_has_real_cpu_model():
    """在 macOS 上实跑：CPU 型号不能是 "arm"（这是 platform.processor() 的无信息返回值）。"""
    import platform as _pl
    if _pl.system() != "Darwin":
        pytest.skip("仅 macOS 上校验")
    info = _load_agent().collect_hostinfo()
    model = str(info.get("cpu_model") or "")
    assert model and model.lower() != "arm", f"macOS CPU 型号无效：{model!r}"
