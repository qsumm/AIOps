"""D160-5：云主机屏蔽 CPU 型号时，不要把占位符当"型号"展示。

**现场**（用户截图）：主机详情/诊断报告里出现 `CPU 型号 model: 0`——看着像故障。
真相：该云主机的 `/proc/cpuinfo` **没有真正的 model name**，只有一行 `model : 0`
（云厂商把 CPU 型号遮了）。Agent 的解析把"键: 值"原样拼成型号上报，平台照收，
于是 `model: 0` 被当成事实展示；而核数（8/4）、内存（30.5G）其实都是对的。

本套件锁死两端：
  ① Agent 侧：命中被遮蔽的值（0/unknown/…）直接跳过；存的是**值**而不是"键: 值"；
  ② 平台侧兜底：老 Agent 没升级、仍上报 `model: 0` 时，平台也不入库（显示"未暴露"）。
"""
from __future__ import annotations

import builtins
import importlib.util
import os
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT_PY = ROOT / "agent" / "agent_main.py"


def _load_agent():
    path = os.path.abspath(str(AGENT_PY))
    spec = importlib.util.spec_from_file_location("agent_main_d160", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("agent_main_d160", m)
    spec.loader.exec_module(m)
    return m


def _fake_cpuinfo(text: str):
    """让 agent 读到假的 /proc/cpuinfo（其它文件照常）。"""
    real_open = builtins.open

    def _open(file, *a, **kw):
        if str(file) == "/proc/cpuinfo":
            import io
            return io.StringIO(text)
        return real_open(file, *a, **kw)

    return _open


def test_agent_skips_masked_cpu_model(monkeypatch):
    """① `model : 0` 这种被遮蔽的值 → 型号留空（不是 "model: 0"）。"""
    m = _load_agent()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", _fake_cpuinfo(
        "processor\t: 0\nvendor_id\t: GenuineIntel\nmodel\t\t: 0\nflags\t\t: fpu\n"))
    info = m.collect_hostinfo()
    assert info.get("cpu_model", "") == "", \
        f"被遮蔽的型号不得入库，实际 {info.get('cpu_model')!r}"


def test_agent_keeps_real_cpu_model_without_key_prefix(monkeypatch):
    """① 真有 model name → 存**值**（不再带 "model name: " 前缀）。"""
    m = _load_agent()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", _fake_cpuinfo(
        "processor\t: 0\nmodel name\t: Intel(R) Xeon(R) Platinum 8375C CPU @ 2.90GHz\n"))
    info = m.collect_hostinfo()
    assert info.get("cpu_model") == "Intel(R) Xeon(R) Platinum 8375C CPU @ 2.90GHz", \
        f"应存纯型号值，实际 {info.get('cpu_model')!r}"


def test_agent_prefers_model_name_over_numeric_model_code(monkeypatch):
    """① x86 同时有 `model : 85`（数字编码）与 `model name : AMD EPYC …` → 取后者。"""
    m = _load_agent()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", _fake_cpuinfo(
        "processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 25\n"
        "model\t\t: 85\nmodel name\t: AMD EPYC 7K62 48-Core Processor\n"))
    info = m.collect_hostinfo()
    assert info.get("cpu_model") == "AMD EPYC 7K62 48-Core Processor", \
        f"应取 model name，实际 {info.get('cpu_model')!r}"


def test_agent_ignores_numeric_model_code_alone(monkeypatch):
    """① 只有 `model : 85`（x86 数字编码）时 → 型号留空（数字不是型号）。"""
    m = _load_agent()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", _fake_cpuinfo(
        "processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 25\nmodel\t\t: 85\n"))
    info = m.collect_hostinfo()
    assert info.get("cpu_model", "") == "", \
        f"纯数字的 model 编码不是型号，实际 {info.get('cpu_model')!r}"


def test_platform_ingest_drops_masked_cpu_model():
    """② 平台兜底：老 Agent 仍上报 `model: 0` 时，平台也不当型号存。"""
    from app.models.host import Host
    from app.services import ingest

    h = Host(ident="d160-cpu-model", name="x")
    ingest._apply_hostinfo(h, {"cpu_model": "model: 0", "cpu_cores": 8,
                                "cpu_cores_physical": 4, "mem_total": 32719826944},
                           datetime.utcnow())
    assert h.cpu_model == "", f"平台不得把占位符当型号，实际 {h.cpu_model!r}"
    assert h.cpu_cores == 8 and h.cpu_cores_physical == 4
    assert h.mem_total == 32719826944

    h3 = Host(ident="d160-cpu-model3", name="x")
    ingest._apply_hostinfo(h3, {"cpu_model": "85", "cpu_cores": 2}, datetime.utcnow())
    assert h3.cpu_model == "", f"纯数字的 model 编码不是型号，实际 {h3.cpu_model!r}"

    h2 = Host(ident="d160-cpu-model2", name="x")
    ingest._apply_hostinfo(h2, {"cpu_model": "Intel(R) Xeon(R) Gold 6130", "cpu_cores": 16},
                            datetime.utcnow())
    assert h2.cpu_model == "Intel(R) Xeon(R) Gold 6130", "真实型号必须照存"
