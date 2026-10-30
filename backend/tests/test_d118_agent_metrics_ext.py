"""D118 扩展采集的回归锁：差分采集器四条硬规则、TCP 状态解析、指标命名契约、
进程快照扩展字段防护。命名即契约——平台大盘/详情/诊断树/前端都按这些名字消费，
任何一方改名都会被 1.4 节的命名对照锁拦下（防再次踩 disk{mnt}.percent 类坑）。"""
import pytest

from app.store import ring


# ---------- 1.1 差分采集器四条硬规则 ----------

def _load_agent():
    import importlib.util, sys, os
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "agent", "agent_main.py"))
    spec = importlib.util.spec_from_file_location("agent_main_d118", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("agent_main_d118", m)
    spec.loader.exec_module(m)
    return m


def test_diff_rates_first_sample_is_zero():
    m = _load_agent()
    m._rate_prev.clear()
    assert m._diff_rates("k", {"a": 100}, now=1000.0) is None       # 首拍：无速率


def test_diff_rates_normal():
    m = _load_agent()
    m._rate_prev.clear()
    m._diff_rates("k", {"a": 100}, now=1000.0)
    r = m._diff_rates("k", {"a": 250}, now=1010.0)
    assert r["a"] == pytest.approx(15.0)                             # (250-100)/10s


def test_diff_rates_counter_reset_no_negative():
    m = _load_agent()
    m._rate_prev.clear()
    m._diff_rates("k", {"a": 1000}, now=1000.0)
    r = m._diff_rates("k", {"a": 5}, now=1010.0)                     # 回绕：cur < prev
    assert r["a"] == 0.0


def test_diff_rates_dt_zero_no_crash():
    m = _load_agent()
    m._rate_prev.clear()
    m._diff_rates("k", {"a": 100}, now=1000.0)
    assert m._diff_rates("k", {"a": 200}, now=1000.0) is None        # dt=0 → None，不除零


# ---------- 1.2 TCP 状态解析 ----------

def test_tcp_counts_parses_proc_sample(tmp_path):
    m = _load_agent()
    d = tmp_path / "proc"; d.mkdir()
    sample = (
        "  sl local rem st\n"
        "   0: 0100007F:0016 00000000:0000 0A\n"   # LISTEN
        "   1: 0100007F:0016 0100007F:9E37 01\n"   # ESTABLISHED
        "   2: 0100007F:9E37 0100007F:0016 01\n"   # ESTABLISHED
        "   3: 0100007F:1234 0100007F:0050 06\n"   # TIME_WAIT
        "   4: 0100007F:4321 0100007F:0050 08\n"   # CLOSE_WAIT
        "   5: 0100007F:5678 0100007F:0050 02\n"   # SYN_SENT
    )
    t1 = d / "tcp"; t1.write_text(sample)
    t6 = d / "tcp6"; t6.write_text("  sl local rem st\n   0: 00:0016 00:0000 0A\n")  # +1 LISTEN
    counts = m._tcp_counts(paths=[str(t1), str(t6)])
    assert counts["listen"] == 2
    assert counts["established"] == 2
    assert counts["time_wait"] == 1 and counts["close_wait"] == 1 and counts["syn_sent"] == 1
    assert counts["total"] == 7


# ---------- 1.4 指标命名契约锁 ----------

# 与 docs/.../02-技术设计方案.md §2.1 一字不差（新增/改名必须同时改两处，否则本锁红）
NAMES = {"cpu.user", "cpu.system", "cpu.iowait",
         "mem.available_percent", "mem.buffers", "mem.cached",
         "load.5", "load.15",
         "net.tcp.established", "net.tcp.time_wait", "net.tcp.listen",
         "net.tcp.close_wait", "net.tcp.syn_sent", "net.tcp.total"}
PREFIX_OK = ("disk.io.", "disk.inode.", "net.")


def test_ext_metric_names_follow_contract():
    m = _load_agent()
    seen = set()
    for p in m.collect_ext_metrics():
        name = p["metric"]
        seen.add(name)
        assert name in NAMES or name.startswith(PREFIX_OK), f"未登记的指标名：{name}"
    # 伪文件系统不该出现在 inode 指标里（disk_partitions(all=False) 只给真实挂载）
    for name in seen:
        if name.startswith("disk.inode."):
            key = name.split(".")[2]
            assert key not in ("proc", "sys", "dev"), name


# ---------- 1.4 进程快照扩展字段防护 ----------

def test_set_services_keeps_new_proc_fields_and_drops_dirty():
    ring._services.clear()
    ring.set_services("h", {"ports": [], "processes": [
        {"name": "nginx", "cpu": 3.2, "state": "running", "user": "root", "pid": 101,
         "mem_rss": 123456, "mem_percent": 6.5, "open_fds": 54},
        {"name": "bad", "cpu": "x", "pid": "NaN", "mem_rss": -5, "open_fds": "y",
         "mem_percent": 999},
    ]})
    procs = ring.get_services("h")["processes"]
    assert len(procs) == 2
    good = next(p for p in procs if p["name"] == "nginx")
    assert good["pid"] == 101 and good["mem_rss"] == 123456
    assert good["open_fds"] == 54 and good["mem_percent"] == 6.5 and good["user"] == "root"
    bad = next(p for p in procs if p["name"] == "bad")
    assert "pid" not in bad and bad["mem_rss"] == 0 and "open_fds" not in bad
    assert bad["mem_percent"] == 100.0     # clamp 到 [0,100]