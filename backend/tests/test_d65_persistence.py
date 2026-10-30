"""D65 两条 S 级缺陷的回归锁。

  ① **seekdb 卷必须挂到镜像真实数据目录** `/var/lib/seekdb`。
     实测事故：compose 原挂 `/var/lib/oceanbase`（从未存过数据，卷 4.0K vs 真实
     数据 193MB）——任何容器重建（升级/改配置）都静默清库。D64 给全服务加日志
     限额改变了 seekdb 配置 → compose 重建 seekdb → 演示库清零。
     这条静态锁保证卷路径不再被改回错的目录。

  ② **机器指纹优先稳定标识**（macOS IOPlatformUUID / Linux machine-id），不含
     动态主机名与网卡 MAC。实测事故：用户 Mac 的 hostname 是 DHCP 分配的
     192.168.0.185，IP 一变指纹就变 → 同一台机器被平台认成"新主机"
     （重复行、探活/历史/改名断连）。
"""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COMPOSE = os.path.join(ROOT, "deploy", "docker-compose.yaml")
AGENT = os.path.join(ROOT, "agent", "agent_main.py")


def test_seekdb_volume_mounts_real_data_dir():
    """/var/lib/seekdb 是 seekdb 镜像的真实数据目录（启动日志 --data-dir 证实）。

    锁两件事：卷挂在真实数据目录；错误目录 /var/lib/oceanbase 不再作为挂载点出现。
    """
    src = open(COMPOSE, encoding="utf-8").read()
    assert "seekdb-data:/var/lib/seekdb" in src, "seekdb 卷必须挂 /var/lib/seekdb（真实数据目录）"
    # 只允许出现在注释里（说明历史），不能出现在挂载指令中
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        assert "/var/lib/oceanbase" not in code, "不得再把卷挂到 /var/lib/oceanbase（空目录，重建即丢库）"


def _load_agent():
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_main_d65", AGENT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_fingerprint_uses_platform_uuid_not_hostname(monkeypatch):
    """稳定标识可用时：指纹与**主机名/内网 IP 变化无关**（用户 Mac 事故的根治验证）。"""
    m = _load_agent()
    monkeypatch.delenv("AIOPS_FINGERPRINT", raising=False)

    # 模拟 macOS：ioreg 返回稳定 UUID；platform.node() 在两次调用间"换 IP"
    class FakeRun:
        def __init__(self, stdout): self.stdout = stdout

    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "ioreg":
            return FakeRun('    "IOPlatformUUID" = "AAAA-BBBB-CCCC"\n')
        if cmd and cmd[0] == "cat":
            return FakeRun("machine-id-fixed\n")
        if cmd and cmd[0] == "ifconfig":
            return FakeRun("ether aa:bb:cc:dd:ee:ff\n")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(m.sys, "platform", m.sys.platform)   # no-op：保持真值

    import platform as _p
    monkeypatch.setattr(_p, "node", lambda: "192.168.0.185")
    fp1 = m.machine_fingerprint()
    monkeypatch.setattr(_p, "node", lambda: "10.0.0.99")     # DHCP 换租约：IP 变了
    fp2 = m.machine_fingerprint()
    assert fp1 == fp2, f"主机名/IP 变化不应改变指纹（{fp1} != {fp2}）"
    assert len(fp1) == 32


def test_fingerprint_env_override_still_wins(monkeypatch):
    """容器场景：AIOPS_FINGERPRINT 仍最高优先（D7 语义不变）。"""
    import hashlib

    m = _load_agent()
    monkeypatch.setenv("AIOPS_FINGERPRINT", "demo-mock-web-01")
    assert m.machine_fingerprint() == hashlib.sha256(b"demo-mock-web-01").hexdigest()


def test_agent_version_bumped_for_upgrade_path():
    """安装分发依赖版本号提示升级：指纹公式变更属破坏性升级 → 版本必须 ≥0.2.0。"""
    src = open(AGENT, encoding="utf-8").read()
    # P0 起版本号收敛为单一常量（注册/自监控/能力协商共用）
    m = re.search(r'AGENT_VERSION = "(\d+)\.(\d+)\.(\d+)"', src)
    assert m, "agent 缺 AGENT_VERSION 常量"
    assert '"agent_version": AGENT_VERSION' in src, "注册上报必须使用 AGENT_VERSION"
    major, minor, _ = (int(x) for x in m.groups())
    assert (major, minor) >= (0, 2), f"当前 {major}.{minor}，指纹/服务发现为 0.2.0 起的能力"
