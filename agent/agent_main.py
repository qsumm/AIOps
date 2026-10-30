#!/usr/bin/env python3
# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""内置 Agent——被监控机侧采集与上报（FR-B1；03 §1.4）。

- psutil 采集 CPU/内存/磁盘/网络/负载/监控进程（15s 周期可配）
- 探活：http / tcp / process / https-cert（证书读 notAfter 剩余天数）
- 断网本地缓存（SQLite，上限 interval_limit 条≈1h），恢复后批量续传
用法：python agent_main.py --server http://<平台>:8300 --install-token <tok> [--interval 15]
"""

# 兼容 Python 3.9（macOS 系统 python3 就是 3.9）：本文件大量使用 `X | None` 注解，
# 3.10 以下会在**定义时**求值并抛 TypeError（真机实测：Mac 上 Agent 装完立刻崩）。
# 开启注解延迟求值后，3.9 也能正常跑（安装脚本仍优先挑 3.10+ 解释器）。
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import ssl
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psutil

CACHE_DB = Path(__file__).parent / "agent_cache.db"


def _detect_ip() -> str:
    """探测本机 IPv4（优先对外出口 IP）。login 12 不用接口：首选中去往公网的默认路由地址，
    取不到再回退 psutil（无则空串）。用于主机详情 IP 展示——此前空串导致所有主机 IP 为 '-'。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith(("127.", "0.")):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    try:
        import psutil  # noqa: WPS433 —— 运行时再导，避免无 psutil 环境崩
        for nic, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith(("127.", "0.")):
                    return a.address
    except Exception:
        pass
    return ""


def machine_fingerprint() -> str:
    """机器指纹：**优先稳定标识**（D65 修复）。

    背景（用户 Mac 实测）：原公式 = sha256(主机名 + 架构 + 平台 UUID + 首块网卡 MAC)。
    但 Mac 的 hostname 常是动态 IP（本例 192.168.0.185，DHCP 换租约就变），en0 MAC
    在 macOS 开"私有 Wi-Fi 地址"时也会随机化——任一变化 → 指纹变化 → 平台把同一台
    机器认成"新主机"（重复行、探活/历史断连、改名丢失）。Windows/Linux 云主机换网卡同理。

    新口径：有平台级稳定标识（macOS IOPlatformUUID / Linux machine-id）就**只用它**；
    没有才回落到旧公式（保持容器/特殊环境行为不变）。
    AIOPS_FINGERPRINT 环境变量仍最高优先（容器场景显式固定身份，D7）。
    """
    override = os.getenv("AIOPS_FINGERPRINT", "").strip()
    if override:
        return hashlib.sha256(override.encode()).hexdigest()
    stable = ""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    stable = line.split("=")[-1].strip().strip('"')
                    break
        else:
            out = subprocess.run(["cat", "/etc/machine-id"], capture_output=True, text=True, timeout=5)
            stable = out.stdout.strip()
    except Exception:
        pass
    if stable:
        return hashlib.sha256(stable.encode()).hexdigest()[:32]
    # 回落：无稳定标识的环境（少见）用旧公式；主机名/MAC 变化会换指纹，属已知代价。
    parts = [platform.node(), platform.machine()]
    try:
        out = subprocess.run(["ifconfig", "en0"] if sys.platform == "darwin" else ["cat", "/sys/class/net/eth0/address"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if ("ether" in line) or (":" in line and len(line.strip().split(":")) == 6):
                parts.append(line.split()[-1])
                break
    except Exception:
        pass
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


# ---------- 本地缓存（断网续传，FR-B1 验收）----------
def cache_init() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS cache (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, ts REAL)")
    return conn


def cache_push(conn: sqlite3.Connection, payload: dict, limit: int) -> None:
    with conn:
        conn.execute("INSERT INTO cache(payload, ts) VALUES (?, ?)", (json.dumps(payload), time.time()))
        # 上限淘汰：超出 limit 条删最老（≈1h/interval）
        conn.execute("DELETE FROM cache WHERE id <= (SELECT MAX(id) - ? FROM cache)", (limit,))


def cache_pop_batch(conn: sqlite3.Connection, size: int = 50) -> list[dict]:
    rows = conn.execute("SELECT id, payload FROM cache ORDER BY id LIMIT ?", (size,)).fetchall()
    if not rows:
        return []
    with conn:
        conn.execute("DELETE FROM cache WHERE id <= ?", (rows[-1][0],))
    return [json.loads(r[1]) for r in rows]


# ---------- 采集 ----------
def snapshot() -> dict:
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disks = {}
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks[part.mountpoint] = round(u.percent, 1)
        except PermissionError:
            continue
    net = psutil.net_io_counters()
    load1, _, _ = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0, 0, 0)
    # 指标名必须与平台侧**逐字一致**（规则/巡检/大盘/压测都按这些名字取值）：
    #   cpu.percent / mem.percent / disk.percent / load.1 / net.bytes_sent|recv
    # 曾经用过的 `load.load1` 与 `disk{mnt}.percent` 平台读不到 → 磁盘类规则、
    # 巡检的"磁盘水位/系统负载"两项、大盘负载图在 Agent 主机上全为空白（实测发现）。
    # 现在：disk.percent = 根分区（根不在列表里时取最紧的分区，告警更保守）；
    # 另按挂载点给出明细 disk.<挂载点>.percent，供"只盯某个盘"的规则使用。
    root_disk = disks.get("/")
    if root_disk is None and disks:
        root_disk = max(disks.values())
    metrics = [
        {"metric": "cpu.percent", "value": cpu},
        {"metric": "mem.percent", "value": round(mem.percent, 1)},
        {"metric": "load.1", "value": round(load1, 2)},
        {"metric": "net.bytes_sent", "value": net.bytes_sent},
        {"metric": "net.bytes_recv", "value": net.bytes_recv},
    ]
    if root_disk is not None:
        metrics.append({"metric": "disk.percent", "value": root_disk})
    for mnt, v in disks.items():
        key = mnt.strip("/").replace("/", "_") or "root"
        metrics.append({"metric": f"disk.{key}.percent", "value": v})
    metrics.extend(collect_ext_metrics())
    return {"metrics": metrics}


def collect_hostinfo() -> dict:
    """机器硬件配置（CPU 核数/型号、内存总量、开机时间、系统架构）。

    为什么加（D146）：主机详情此前只有 IP / OS / Agent 版本 / 标识——用户看不出
    「这台机器到底几核几 G」，而排障第一步往往就是问配置（OOM？CPU 打满？规格够不够？）。
    这些是**静态信息**（开机时间外基本不变），每次心跳带一份很便宜（~200 字节）。

    字段口径：
      cpu_cores         逻辑核数（psutil.cpu_count(logical=True)）
      cpu_cores_physical 物理核数（拿不到时为 0）
      cpu_model         CPU 型号（Linux 读 /proc/cpuinfo；其它平台用 platform.processor()）
      mem_total         内存字节数（psutil.virtual_memory().total）
      boot_time         开机时间（UTC ISO8601）
      uptime_s          开机秒数
      arch / kernel     架构与内核版本
    """
    info: dict = {}
    try:
        info["cpu_cores"] = int(psutil.cpu_count(logical=True) or 0)
    except Exception:  # noqa: BLE001
        info["cpu_cores"] = 0
    try:
        info["cpu_cores_physical"] = int(psutil.cpu_count(logical=False) or 0)
    except Exception:  # noqa: BLE001
        info["cpu_cores_physical"] = 0
    try:
        import platform as _pl
        model = ""
        if sys.platform.startswith("linux"):
            # x86 有 "model name"；ARM 只有 "CPU part"/"Model"/"Hardware"（云主机常见）
            want = ("model name", "model", "hardware", "cpu part")
            # D160-5：云主机的 /proc/cpuinfo 常**屏蔽**型号，只留一行 `model : 0`——
            # 原实现把"键: 值"原样当型号存下来，界面显示成 "model: 0"，看着像故障。
            # 这里：① 命中被遮蔽的值（0/unknown/…）直接跳过；② 只存**值**，不拼键名。
            masked = {"0", "unknown", "none", "n/a", "to be filled by o.e.m.",
                      "to be filled by oem", "unspecified", "default"}
            # D160-5：x86 的 /proc/cpuinfo **同时**有两行——
            #   model      : 85          ← x86 的 family/model **数字编码**，不是型号
            #   model name : AMD EPYC …  ← 这才是型号
            # 云主机还会把型号整个遮掉（只剩 `model : 0`）。所以规则是：
            #   ① 纯数字 / 掩码值 一律不当型号；② 按键优先级挑（model name > hardware > cpu part > model）。
            try:
                cands: dict[str, str] = {}
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        low = line.lower()
                        for key in want:
                            if low.startswith(key + "\t") or low.startswith(key + ":"):
                                val = (line.split(":", 1)[1].strip() if ":" in line
                                       else line.split(None, 1)[-1].strip())
                                if val and val.lower() not in masked and not val.isdigit():
                                    cands.setdefault(key, val)
                                break
                for key in want:
                    if key in cands:
                        model = cands[key]
                        break
            except OSError:
                model = ""
        if not model and sys.platform == "darwin":
            # macOS：platform.processor() 在 Apple Silicon 上只返回 "arm"（无信息量）。
            # 先取商品名（Intel 有 "Intel(R) Core(TM) ..."），Apple Silicon 常见为空
            # → 再取 hw.model（如 Mac15,6），至少能看出机型代际。
            for key in ("machdep.cpu.brand_string", "hw.model"):
                try:
                    out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=3)
                    val = (out.stdout or "").strip()
                except Exception:
                    val = ""
                if val:
                    model = val
                    break
        if not model and not sys.platform.startswith("linux"):
            # Linux 上没有型号就**留空**（平台会显示"未暴露"）——架构串不是型号；
            # 非 Linux 才退到 platform.processor() 兜底。
            model = (_pl.processor() or "").strip()
        info["cpu_model"] = model[:160]
        info["arch"] = (_pl.machine() or "")[:32]
        info["kernel"] = (_pl.release() or "")[:64]
    except Exception:  # noqa: BLE001
        pass
    try:
        info["mem_total"] = int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001
        info["mem_total"] = 0
    try:
        bt = psutil.boot_time()
        info["boot_time"] = datetime.fromtimestamp(bt, tz=timezone.utc).isoformat()
        info["uptime_s"] = int(max(0, time.time() - bt))
    except Exception:  # noqa: BLE001
        pass
    return info


def collect_disks() -> list[dict]:
    """每个挂载点的完整磁盘快照（df -h 视角，全量含 tmpfs/overlay）。

    与标量 `disk.X.percent` 分工：这里给的是结构化明细
    （device/fstype/size/used/free/percent/mount），平台存最近一份快照，
    供大盘 / 主机详情渲染"每个分区"。采集全量、展示层再过滤伪文件系统，
    避免展示口径一变就要重装 agent。对挂载点测不了用量（如 /proc）直接跳过。"""
    out = []
    for part in psutil.disk_partitions(all=True):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        out.append({
            "device": part.device or "",
            "fstype": part.fstype or "",
            "size": u.total,
            "used": u.used,
            "free": u.free,
            "percent": round(u.percent, 1),
            "mount": part.mountpoint or "",
        })
    return out


# ---------- 扩展采集（D118，对标阿里云操作系统/网络监控；命名与 02-技术设计方案 §2.1 一字不差） ----------
# 速率类指标全部走「差分」：内存里存上一次计数器快照与时间戳，本轮 (cur-prev)/dt。
# 首拍（prev=None）、计数器回绕（cur<prev，设备重建/agent 重启）、dt<=0 一律计 0——
# 绝不让采集异常污染指标序列（测试设计 04 §1.1 的四条硬规则）。
_rate_prev: dict[str, tuple[float, dict[str, float]]] = {}


def _diff_rates(key: str, counters: dict, now: float):
    """计数器差分 → 速率。prev 缺失/时间未前进/计数回绕时安全返回 None 或 0。"""
    prev = _rate_prev.get(key)
    _rate_prev[key] = (now, dict(counters))
    if prev is None or now <= prev[0]:
        return None
    dt = now - prev[0]
    out = {}
    for k, v in counters.items():
        d = float(v) - float(prev[1].get(k, 0.0))
        out[k] = d / dt if d > 0 else 0.0
    return out


def _diskstats() -> dict:
    """Linux 分区级磁盘计数（/proc/diskstats）：completed IOPS 与 sectors×512 字节。
    macOS 无此文件 → 返回空（由 psutil perdisk 兜底为全盘聚合）。"""
    out = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                out[parts[2]] = {"read_iops": float(parts[3]), "write_iops": float(parts[7]),
                                 "read_sectors": float(parts[5]), "write_sectors": float(parts[9])}
    except OSError:
        pass
    return out


_TCP_ST = {"01": "established", "06": "time_wait", "0A": "listen", "08": "close_wait", "02": "syn_sent"}


def _tcp_counts(paths=None) -> dict:
    """TCP 连接数按状态：Linux 解析 /proc/net/tcp+tcp6（零开销）；macOS psutil 兜底。
    paths 仅供测试注入样本文件；生产恒为 None。"""
    sources = paths or ("/proc/net/tcp", "/proc/net/tcp6")
    counts = {"established": 0, "time_wait": 0, "listen": 0, "close_wait": 0, "syn_sent": 0}
    parsed = False
    for path in sources:
        try:
            with open(path) as f:
                lines = f.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            seg = line.split()
            if len(seg) < 4:
                continue
            st = _TCP_ST.get(seg[3].upper())
            if st:
                counts[st] += 1
        parsed = True
    if not parsed:
        psutil_ok = False
        try:
            for c in psutil.net_connections(kind="tcp"):
                s = str(getattr(c, "status", "") or "").lower()
                if s in counts:
                    counts[s] += 1
            psutil_ok = True
        except Exception:
            psutil_ok = False
        if not psutil_ok or not sum(counts.values()):
            # macOS 上没有 root 权限时 psutil.net_connections 直接 AccessDenied。
            # 顺序：lsof（mac 自带、无需特权，行尾带 `(ESTABLISHED)` 状态）
            #       → netstat（老系统兜底）。
            # 实测坑：macOS 27 的 `netstat -an -p tcp` 在非 tty 子进程里返回 0 行
            # （交互 shell 有输出），所以 lsof 才是可靠的那条。
            import re as _re2
            try:
                out = subprocess.run(["lsof", "-nP", "-iTCP"],
                                     capture_output=True, text=True, timeout=12).stdout
                for line in out.splitlines()[1:]:
                    m = _re2.search(r"\((ESTABLISHED|TIME_WAIT|LISTEN|CLOSE_WAIT|SYN_SENT)\)\s*$", line)
                    if m:
                        key = m.group(1).lower()
                        if key in counts:
                            counts[key] += 1
            except Exception:
                pass
            if not sum(counts.values()):
                try:
                    out = subprocess.run(["netstat", "-an"],
                                         capture_output=True, text=True, timeout=8).stdout
                    st_map = {"established": "established", "time_wait": "time_wait",
                              "listen": "listen", "close_wait": "close_wait", "syn_sent": "syn_sent"}
                    for line in out.splitlines():
                        parts = line.split()
                        if len(parts) < 6 or not parts[0].startswith("tcp"):
                            continue
                        s = st_map.get(parts[-1].lower())
                        if s:
                            counts[s] += 1
                except Exception:
                    pass
    counts["total"] = sum(counts.values())
    return counts


def collect_ext_metrics() -> list:
    """D118 扩展指标集。**命名即契约**：cpu.* / mem.* / load.* / disk.io.* / disk.inode.* / net.* / net.tcp.*，
    平台侧大盘/详情/诊断树与命名回归锁都按这些名字消费。"""
    import os as _os
    metrics: list = []
    now = time.time()

    # CPU 分解：cpu_times_percent(interval=None) 基于上次调用差分（首拍全 0，属预期）
    try:
        ct = psutil.cpu_times_percent(interval=None)
        for k in ("user", "system", "iowait"):
            v = float(getattr(ct, k, 0.0) or 0.0)
            metrics.append({"metric": f"cpu.{k}", "value": round(max(0.0, v), 1)})
    except Exception:
        pass

    # 内存分解（buffers/cached 以 MB 报）
    try:
        vm = psutil.virtual_memory()
        total = vm.total or 1
        metrics.append({"metric": "mem.available_percent",
                        "value": round(float(vm.available) / total * 100, 1)})
        # macOS（Darwin）的 psutil 没有 buffers/cached：用 inactive 近似「可回收缓存」，
        # 否则 Mac 主机的「缓冲/缓存」永远显示 0（不是没数据，是字段不存在）。
        _cached = getattr(vm, "cached", None)
        if _cached is None:
            _cached = getattr(vm, "inactive", 0) or 0
        metrics.append({"metric": "mem.buffers",
                        "value": round(float(getattr(vm, "buffers", 0) or 0) / 1048576, 1)})
        metrics.append({"metric": "mem.cached",
                        "value": round(float((_cached or 0) + (getattr(vm, "shared", 0) or 0)) / 1048576, 1)})
    except Exception:
        pass

    # 负载 5/15（load.1 在基础指标里已有）
    try:
        _, l5, l15 = _os.getloadavg()
        metrics.append({"metric": "load.5", "value": round(float(l5), 2)})
        metrics.append({"metric": "load.15", "value": round(float(l15), 2)})
    except Exception:
        pass

    # 磁盘 IO：Linux 分区级（/proc/diskstats，sectors×512=bytes）；macOS psutil perdisk 全盘聚合
    try:
        devs = _diskstats()
        if devs:
            flat = {f"{d}.{k}": v for d, cnts in devs.items() for k, v in cnts.items()}
            rates = _diff_rates("disk", flat, now)
            if rates:
                for d in devs:
                    metrics.append({"metric": f"disk.io.{d}.read_bps",
                                    "value": round(rates.get(f"{d}.read_sectors", 0.0) * 512, 1)})
                    metrics.append({"metric": f"disk.io.{d}.write_bps",
                                    "value": round(rates.get(f"{d}.write_sectors", 0.0) * 512, 1)})
                    metrics.append({"metric": f"disk.io.{d}.read_iops",
                                    "value": round(rates.get(f"{d}.read_iops", 0.0), 1)})
                    metrics.append({"metric": f"disk.io.{d}.write_iops",
                                    "value": round(rates.get(f"{d}.write_iops", 0.0), 1)})
        else:
            pc = psutil.disk_io_counters(perdisk=True)
            if pc:
                flat = {}
                for d, c in pc.items():
                    for k, v in (("read_bytes", c.read_bytes), ("write_bytes", c.write_bytes),
                                 ("read_count", c.read_count), ("write_count", c.write_count)):
                        flat[f"{d}.{k}"] = float(v)
                rates = _diff_rates("disk", flat, now)
                if rates:
                    agg = {k: sum(rates.get(f"{x}.{k}", 0.0) for x in pc)
                           for k in ("read_bytes", "write_bytes", "read_count", "write_count")}
                    for name, key in (("read_bps", "read_bytes"), ("write_bps", "write_bytes"),
                                      ("read_iops", "read_count"), ("write_iops", "write_count")):
                        metrics.append({"metric": f"disk.io.{name}", "value": round(agg[key], 1)})
    except Exception:
        pass

    # 网络：按网卡差分（bytes/packets 为 Count/s；err/drop 计数差分同阿里云口径）
    try:
        nc = psutil.net_io_counters(pernic=True)
        if nc:
            flat = {}
            for nic, c in nc.items():
                for k, v in (("in_bps", c.bytes_recv), ("out_bps", c.bytes_sent),
                             ("in_pps", c.packets_recv), ("out_pps", c.packets_sent),
                             ("errin", c.errin), ("errout", c.errout),
                             ("dropin", c.dropin), ("dropout", c.dropout)):
                    flat[f"{nic}.{k}"] = float(v)
            rates = _diff_rates("net", flat, now)
            if rates:
                for nic in nc:
                    for k in ("in_bps", "out_bps", "in_pps", "out_pps", "errin", "errout", "dropin", "dropout"):
                        v = rates.get(f"{nic}.{k}")
                        if v is not None:
                            metrics.append({"metric": f"net.{nic}.{k}", "value": round(v, 1)})
    except Exception:
        pass

    # TCP 连接数（按状态）
    try:
        for st, v in _tcp_counts().items():
            metrics.append({"metric": f"net.tcp.{st}", "value": float(v)})
    except Exception:
        pass

    # inode 使用率（Linux statvfs；macOS 语义不同 → 失败静默跳过）
    try:
        for part in psutil.disk_partitions(all=False):
            try:
                st = _os.statvfs(part.mountpoint)
                files = float(getattr(st, "f_files", 0) or 0)
                if files > 0:
                    key = part.mountpoint.strip("/").replace("/", "_") or "root"
                    metrics.append({"metric": f"disk.inode.{key}.percent",
                                    "value": round((files - float(st.f_ffree or 0)) / files * 100, 1)})
            except OSError:
                continue
    except Exception:
        pass

    return metrics


def _ports_from_proc() -> list[dict]:
    """/proc/net/tcp(/tcp6) → LISTEN 端口列表。纯函数便于单测（macOS 无 /proc，容器实跑才覆盖）。"""
    import pathlib as _pl

    out: list[dict] = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in _pl.Path(f).read_text().splitlines()[1:]:
                parts = line.split()
                if len(parts) < 4 or parts[3] != "0A":   # 0A = LISTEN
                    continue
                port = int(parts[1].rsplit(":", 1)[1], 16)
                if all(p["port"] != port for p in out):
                    out.append({"port": port, "addr": "*:*", "proc": ""})
        except Exception:
            pass
    return out


def discover_services(max_ports: int = 24, max_procs: int = 10) -> dict:
    """服务发现：扫出本机**正在监听的端口**与 **CPU 占用最高的进程**（D63）。

    用户口径：「探活之外，还要看得见服务器上跑了什么服务」。探活（probes）是
    主动盯指定目标，发现（discovery）是把现状摆出来——两者互补。
    fail-soft：ss/lsof/ps 任一步失败只少给一段，绝不能影响心跳与指标上报。
    """
    import re as _re
    import shutil
    import subprocess

    ports: list[dict] = []
    procs: list[dict] = []
    try:
        if shutil.which("ss"):
            out = subprocess.run(["ss", "-tlnpt"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 4 or ":" not in parts[3]:
                    continue
                addr = parts[3]
                port = addr.rsplit(":", 1)[-1]
                name = ""
                m = _re.search(r'\(\s*"([^"]+)"', line)
                if m:
                    name = m.group(1)
                if port.isdigit() and all(p["port"] != int(port) for p in ports):
                    ports.append({"port": int(port), "addr": addr, "proc": name})
        elif shutil.which("lsof"):
            out = subprocess.run(["lsof", "+c0", "-nP", "-iTCP", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=8).stdout
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 9 or ":" not in parts[8]:
                    continue
                addr = parts[8]
                port = addr.rsplit(":", 1)[-1]
                if port.isdigit() and all(p["port"] != int(port) for p in ports):
                    ports.append({"port": int(port), "addr": addr, "proc": parts[0]})
        else:
            # 兜底①：直接解析 /proc/net/tcp（Linux 内核永远提供，slim 容器连 netstat 都没有——
            # D63-2 实测 aiops-agent 镜像三无：无 ss / 无 lsof / 无 netstat）。代价是拿不到进程名。
            ports.extend(_ports_from_proc())
            if not ports:
                # 兜底②：netstat -tlnp（macOS/老系统）
                try:
                    out = subprocess.run(["netstat", "-tlnp"], capture_output=True, text=True, timeout=8).stdout
                    for line in out.splitlines():
                        parts = line.split()
                        if len(parts) < 4 or not parts[0].startswith("tcp") or ":" not in parts[3]:
                            continue
                        addr = parts[3]
                        port = addr.rsplit(":", 1)[-1]
                        proc = parts[-1].rsplit("/", 1)[-1] if "/" in parts[-1] else ""
                        if port.isdigit() and all(p["port"] != int(port) for p in ports):
                            ports.append({"port": int(port), "addr": addr, "proc": proc})
                except Exception:
                    pass
    except Exception:
        pass
    # D111：进程状态（STAT）是 L2「僵死」故障的关键证据——没有它 AI 只能给 INCONCLUSIVE。
    # psutil 优先（status() → running/sleeping/stopped/zombie），ps 兜底解析 STAT 列。
    try:
        import psutil as _ps
        rows = []
        for p_ in _ps.process_iter(["pid", "name", "cpu_percent", "status",
                                    "memory_info", "memory_percent", "username", "num_fds"]):
            try:
                mi = p_.info.get("memory_info")
                rows.append({"name": p_.info.get("name") or "",
                             "cpu": float(p_.info.get("cpu_percent") or 0.0),
                             "state": str(p_.info.get("status") or ""),
                             "pid": p_.info.get("pid"),
                             "user": str(p_.info.get("username") or "")[:32],
                             "mem_rss": int(getattr(mi, "rss", 0) or 0),
                             "mem_percent": round(float(p_.info.get("memory_percent") or 0.0), 1),
                             "open_fds": int(p_.info.get("num_fds") or 0)})
            except Exception:
                continue
        # D118（FR-A6，对标阿里云进程监控表）：CPU TopN ∪ 内存 TopN 去重——
        # 只有 CPU TopN 会漏掉"内存泄漏但 CPU 空闲"的进程，两个维度都必须可见。
        by_cpu = sorted(rows, key=lambda r: -r["cpu"])
        by_mem = sorted(rows, key=lambda r: -r["mem_percent"])
        seen = set()
        for r in by_cpu + by_mem:
            base = (r["name"] or "").rsplit("/", 1)[-1]
            if not base or base in seen:
                continue
            seen.add(base)
            procs.append({"name": base, "cpu": round(r["cpu"], 1), "state": r["state"],
                          "pid": r["pid"], "user": r["user"], "mem_rss": r["mem_rss"],
                          "mem_percent": r["mem_percent"], "open_fds": r["open_fds"]})
            if len(procs) >= max_procs:
                break
    except Exception:
        try:
            out = subprocess.run(["ps", "-axo", "%cpu,stat,comm"], capture_output=True, text=True, timeout=5).stdout
            rows = []
            for line in out.splitlines()[1:]:
                seg = line.strip().split(None, 2)
                if len(seg) == 3:
                    try:
                        rows.append((seg[2].strip(), float(seg[0]), seg[1]))
                    except ValueError:
                        pass
            rows.sort(key=lambda x: -x[1])
            seen = set()
            for name, cpu, stat in rows:
                base = name.rsplit("/", 1)[-1]
                if not base or base in seen:
                    continue
                seen.add(base)
                procs.append({"name": base, "cpu": round(cpu, 1), "state": stat})
                if len(procs) >= max_procs:
                    break
        except Exception:
            pass
    return {"ports": ports[:max_ports], "processes": procs}


def probe_http(url: str, timeout: float = 5.0) -> dict:
    t0 = time.time()
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        return {"status": "up" if r.status_code < 500 else "down", "latency_ms": int((time.time() - t0) * 1000)}
    except Exception:
        return {"status": "down", "latency_ms": -1}


def probe_tcp(hostport: str, timeout: float = 3.0) -> dict:
    host, _, port = hostport.partition(":")
    t0 = time.time()
    try:
        with socket.create_connection((host, int(port or 80)), timeout=timeout):
            return {"status": "up", "latency_ms": int((time.time() - t0) * 1000)}
    except Exception:
        return {"status": "down", "latency_ms": -1}


def probe_process(pattern: str, max_hits: int = 0) -> dict:
    """进程探活（FR-B1）：按名称/命令行子串匹配进程。

    D7 实测发现：run_probes 此前只有 http/tcp/https-cert 三个分支，**process 从未实现**，
    导致进程探活永远上报 unknown（探活规则里的 process 项因此永不触发；巡检的"异常进程"项也拿不到数据）。
    匹配规则：pattern 命中 process name 或 cmdline（不区分大小写）；排除自身与父进程，避免自匹配。
    latency_ms 复用为**命中进程数**（与 https-cert 复用为"剩余天数"同一约定，含义见 04 §2 probes 注释）。
    """
    pat = (pattern or "").strip().lower()
    if not pat:
        return {"status": "unknown", "latency_ms": -1}
    me, parent = os.getpid(), os.getppid()
    hits = 0
    try:
        import psutil

        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] in (me, parent):
                    continue
                name = (proc.info.get("name") or "").lower()
                cmd = " ".join(proc.info.get("cmdline") or []).lower()
                if pat in name or pat in cmd:
                    hits += 1
                    if max_hits and hits >= max_hits:
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        return {"status": "unknown", "latency_ms": -1}
    return {"status": "up" if hits else "down", "latency_ms": hits}


def _der_len(buf: bytes, i: int) -> tuple[int, int]:
    """DER 长度字段 → (长度, 值起始下标)。支持短式与 0x81/0x82 长式。"""
    b = buf[i]
    if b < 0x80:
        return b, i + 1
    n = b & 0x7F
    return int.from_bytes(buf[i + 1:i + 1 + n], "big"), i + 1 + n


def _der_children(buf: bytes, start: int, end: int) -> list[tuple[int, bytes]]:
    """拆出 TLV 段内的子元素：[(tag, 完整 TLV 字节)]。"""
    out, i = [], start
    while i < end:
        tag = buf[i]
        ln, j = _der_len(buf, i + 1)
        out.append((tag, buf[i:j + ln]))
        i = j + ln
    return out


def _tlv_value(tlv: bytes) -> bytes:
    """完整 TLV → 值字节。"""
    _, j = _der_len(tlv, 1)
    return tlv[j:]


def _parse_asn1_time(v: bytes) -> datetime:
    """ASN.1 Time → datetime（UTCTime 'YYMMDDHHMMSSZ' / GeneralizedTime 'YYYYMMDDHHMMSSZ'）。"""
    s = v.decode("ascii").strip()
    if s.endswith("Z"):
        s = s[:-1]
    if len(s) == 12:                       # UTCTime：YY>=50 归 19xx，否则 20xx
        yy = int(s[:2])
        year = 1900 + yy if yy >= 50 else 2000 + yy
        s = f"{year}{s[2:]}"
    if len(s) < 14:
        raise ValueError(f"无法解析的 ASN.1 时间: {v!r}")
    return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]), int(s[10:12]), int(s[12:14]),
                    tzinfo=timezone.utc)


def _cert_not_after_der(der: bytes) -> datetime:
    """从 DER 证书里取 notAfter（最小 ASN.1 走位，**不依赖 cryptography/openssl**）。

    Certificate   ::= SEQUENCE { tbsCertificate SEQUENCE {...}, signatureAlgorithm, signatureValue }
    TBSCertificate::= SEQUENCE { [0] version OPTIONAL, serialNumber, signature, issuer, validity, ... }
    Validity      ::= SEQUENCE { notBefore Time, notAfter Time }

    为什么要自己走位：ssl 在 `verify_mode=CERT_NONE` 下 `getpeercert()` 返回空 dict，
    只有 `binary_form=True` 能拿到 DER；而引入 cryptography 会违反"依赖锁定"纪律。

    注意：**必须先剥掉 TLV 头再从 0 开始遍历子元素**——长式长度（`30 82 03 0b`）时值不在下标 1
    （D9 实测：把起始下标写死成 1 会一路错位到 IndexError）。
    """
    cert = _der_children(der, 0, len(der))[0][1]          # 外层 SEQUENCE → tbsCertificate 区
    top = _der_children(_tlv_value(cert), 0, len(_tlv_value(cert)))
    tbs = _tlv_value(top[0][1])                           # tbsCertificate
    kids = _der_children(tbs, 0, len(tbs))
    idx = 1 if kids and kids[0][0] == 0xA0 else 0          # [0] EXPLICIT version 可选
    validity = _tlv_value(kids[idx + 3][1])                # serial → signature → issuer → validity
    children = _der_children(validity, 0, len(validity))   # notBefore, notAfter
    not_after = children[1][1]                             # 取第二个 child 的完整 TLV（bytes）
    return _parse_asn1_time(_tlv_value(not_after))


def probe_https_cert(hostport: str) -> dict:
    """证书剩余天数（FR-D5 巡检项数据源；评审 #7）。

    ⚠️ **必须用不校验证书链/主机名的上下文取对端证书**：这是"到期监控"，不是"TLS 健康检查"。
    D9 实测踩到：用 `create_default_context()`（默认校验证书链与主机名）时，**自签证书、
    内网私有 CA 证书、以及用 IP 探 CN=xxx 的证书**都会抛 SSLCertVerificationError
    → 一律返回 down/-1，于是这类环境**永远读不到"还剩几天"**（阈值与前端展示同时失真）；
    已过期证书也会变成 -1 而不是真实的负数天数。而这些恰恰是本产品的主战场（内网/单机自签）。
    证书链/主机名的校验结论不丢：一并作为 verify_error 返回，交给上层判断。

    D163-5（交付前测试发现）：接入向导提示用户填 `https://your.domain`，但这里用
    `partition(":")` 解析 → "https" 被当主机名、"//your.domain" 被当端口 → 连接必败
    → **按官方提示填的证书探活永远 down/-1**。修：先剥 scheme（https://、http://），
    端口缺省 443；`host:port` 老写法照常。
    """
    # D163-5：剥 scheme —— "https://your.domain:8443" / "https://your.domain" / "your.domain:8443" 都收
    t = (hostport or "").strip()
    for scheme in ("https://", "http://"):
        if t.lower().startswith(scheme):
            t = t[len(scheme):]
            break
    host, _, port = t.partition(":")
    if not port.strip().isdigit():
        port = "443"      # 证书探活默认 https 端口（host:port 老写法仍尊重数字端口）
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    verify_error = ""
    try:
        # 单独用校验上下文探一次，只为把"链/主机名不合法"这个事实取出来（失败不影响取值）
        try:
            with socket.create_connection((host, int(port or 443)), timeout=5) as s2:
                with ssl.create_default_context().wrap_socket(s2, server_hostname=host):
                    pass
        except Exception as e:  # noqa: BLE001
            verify_error = f"{type(e).__name__}: {e}"
        with socket.create_connection((host, int(port or 443)), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        if not der:
            return {"status": "down", "latency_ms": -1, "verify_error": "未取到对端证书"}
        exp = _cert_not_after_der(der)
        days = (exp - datetime.now(timezone.utc)).days
        return {"status": "up" if days > 0 else "down", "latency_ms": days, "verify_error": verify_error}
    except Exception as e:  # noqa: BLE001
        return {"status": "down", "latency_ms": -1, "verify_error": f"{type(e).__name__}: {e}"}


def run_probes(specs: list[dict]) -> list[dict]:
    out = []
    for p in specs:
        r = {"name": p["name"], "type": p["type"], "target": p.get("target", "")}
        if p["type"] == "http":
            r.update(probe_http(p["target"]))
        elif p["type"] == "tcp":
            r.update(probe_tcp(p["target"]))
        elif p["type"] == "https-cert":
            r.update(probe_https_cert(p["target"]))
        elif p["type"] == "process":
            r.update(probe_process(p.get("target", "")))
        out.append(r)
    return out




# ═══════════════════════════════════════════════════════════════════
# P5（v0.4）：服务级监控采集 —— 在目标机本地跑只读探测，只回传原始输出
#
# 分层约定（与平台侧 services/svc_monitor.py 配对）：
#   · 采集在 Agent（见下）：探测命令必须在目标机上执行，且**凭据不出目标机**
#     （用目标机自己的 my.cnf / 本地免密）；
#   · 解析与阈值判定在平台侧：于是改阈值/加指标/修解析都是平台一次发布的事，
#     不需要动目标机上的 Agent。
# 探测项来自：① 内置五类模板（按监听端口/进程自动发现，零配置）
#             ② 安装时 `--services '[{"type":"app","name":"订单服务","port":8081,"process":"order"}]'`
#                （自研应用没法靠端口猜出来，需显式声明，与既有 --probes 同款设计）
# ═══════════════════════════════════════════════════════════════════
SVC_PROBES: dict[str, list[str]] = {
    "mysql": ["sh", "-c",
              "mysql --defaults-file=/etc/my.cnf -N -e "
              "\"SHOW GLOBAL STATUS WHERE Variable_name IN "
              "('Threads_connected','Threads_running','Slow_queries','Aborted_connects',"
              "'Innodb_row_lock_waits','Uptime'); SHOW GLOBAL VARIABLES WHERE Variable_name="
              "'max_connections'\""],
    "redis": ["redis-cli", "-h", "127.0.0.1", "INFO"],
    "nginx": ["sh", "-c", "curl -fsS --max-time 3 http://127.0.0.1/stub_status || true"],
    "mq": ["sh", "-c", "curl -fsS --max-time 3 -u guest:guest "
                       "http://127.0.0.1:15672/api/queues || true"],
}
SVC_DEFAULT = {   # 与平台侧模板的默认值保持一致（端口/单元/日志路径）
    "mysql": {"port": 3306, "unit": "mysql", "log": "/var/log/mysql/error.log"},
    "redis": {"port": 6379, "unit": "redis", "log": "/var/log/redis/redis-server.log"},
    "nginx": {"port": 80, "unit": "nginx", "log": "/var/log/nginx/error.log"},
    "mq": {"port": 5672, "unit": "rabbitmq-server", "log": "/var/log/rabbitmq/rabbit.log"},
    "app": {"port": 0, "unit": "", "log": ""},
}
_RAW_CAP = 8192          # 原始输出截断（够解析关键指标，又不至于把心跳撑大）


def _port_listening(port: int) -> bool:
    if not port:
        return False
    s_ = socket.socket()
    try:
        s_.settimeout(1.5)
        s_.connect(("127.0.0.1", int(port)))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            s_.close()
        except Exception:  # noqa: BLE001
            pass


def _svc_cmd_with_port(stype: str, cmd: list[str], port: int, default_port: int) -> list[str]:
    """把内置探测命令里的默认端口换成声明的端口（未声明/等于默认则原样返回）。"""
    if not port or port == default_port:
        return cmd
    if stype == "mysql":
        return ["sh", "-c", "mysql --defaults-file=/etc/my.cnf -h 127.0.0.1 -P %d -N -e "
                            "\"SHOW GLOBAL STATUS WHERE Variable_name IN "
                            "('Threads_connected','Threads_running','Slow_queries','Aborted_connects',"
                            "'Innodb_row_lock_waits','Uptime'); SHOW GLOBAL VARIABLES WHERE Variable_name="
                            "'max_connections'\"" % port]
    if stype == "redis":
        return ["redis-cli", "-h", "127.0.0.1", "-p", str(port), "INFO"]
    if stype == "nginx":
        return ["sh", "-c", "curl -fsS --max-time 3 http://127.0.0.1:%d/stub_status || true" % port]
    if stype == "mq":
        # 管理 API 端口（15672）与 AMQP 端口（5672）不是一个：声明里给的是 AMQP 端口时仍用默认管理口
        mgmt = port if port != 5672 else default_port
        return ["sh", "-c", "curl -fsS --max-time 3 -u guest:guest "
                            "http://127.0.0.1:%d/api/queues || true" % mgmt]
    return cmd


def probe_services(declared: list[dict]) -> list[dict]:
    """采集本机服务状态。declared 为安装时声明的应用类服务（可空）。

    返回每项：{type, name, spec, raw, ok, error}
      · 内置四类（mysql/redis/nginx/mq）：按"默认端口在监听"或"unit 进程存在"自动发现后探测；
      · 应用类（app）：只用 declared 声明（端口 + 进程名 + 可选健康端点）。
    """
    out: list[dict] = []
    procs = ""
    try:
        procs = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True,
                               timeout=5).stdout
    except Exception:  # noqa: BLE001
        procs = ""

    # 平台声明的**非默认端口**也要能探（如 MySQL 装在 3307）——把声明按类型收进来，
    # 探测命令按声明的端口参数化；没有声明时按默认端口/单元自动发现（原行为不变）。
    declared_by_type: dict[str, dict] = {}
    for item in declared or []:
        t = str(item.get("type") or "app")
        if t in SVC_PROBES:
            declared_by_type.setdefault(t, item)

    for stype, cmd in SVC_PROBES.items():
        d = SVC_DEFAULT[stype]
        dec = declared_by_type.get(stype) or {}
        port = int(dec.get("port") or d["port"] or 0)
        unit = str(dec.get("unit") or d["unit"] or "")
        found = _port_listening(port) or (unit and unit in procs)
        if not found:
            continue                      # 没有这个服务就不报（避免制造空的 unknown 噪音）
        name = str(dec.get("name") or "%s(%d)" % (
            {"mysql": "MySQL", "redis": "Redis", "nginx": "Nginx", "mq": "消息队列"}[stype], port))
        spec = {"type": stype, "name": name, "port": port, "unit": unit,
                "log_path": str(dec.get("log_path") or d["log"])}
        cmd = _svc_cmd_with_port(stype, cmd, port, d["port"])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            raw = (r.stdout or "")[:_RAW_CAP]
            # 有输出即算"探测成功"（是否正常由平台侧按阈值判定）；空输出且报错才算失败
            ok = bool(raw.strip())
            out.append({"type": stype, "name": name, "spec": spec, "raw": raw, "ok": ok,
                        "error": "" if ok else ((r.stderr or "无输出")[:200])})
        except Exception as e:  # noqa: BLE001
            out.append({"type": stype, "name": name, "spec": spec, "raw": "", "ok": False,
                        "error": "%s: %s" % (type(e).__name__, str(e)[:160])})

    # 应用类：按声明探测（进程 / 端口 / 健康端点）
    for item in declared or []:
        spec = dict(item)
        name = str(spec.get("name") or spec.get("process") or "应用")
        spec.setdefault("type", "app")
        proc = str(spec.get("process") or "")
        port = int(spec.get("port") or 0)
        url = str(spec.get("health_url") or "")
        if not (proc or port or url):
            out.append({"type": "app", "name": name, "spec": spec, "raw": "", "ok": False,
                        "error": "未声明探测方式（进程名 / 端口 / 健康端点至少给一个）"})
            continue
        errs = []
        # app 类回传**结构化**检查结果：进程/端口/健康端点各一项，平台侧据此判定
        # （不能让平台去解析一段文本——第一版这么写，导致有进程活着也被判成 unknown，实测踩到）
        metrics = {}
        if proc:
            metrics["process_ok"] = 1 if _process_alive(proc) else 0
        if port:
            metrics["port_ok"] = 1 if _port_listening(port) else 0
        if url:
            metrics["health_ok"] = 1 if probe_http(url).get("status") == "up" else 0
        checks = [v for v in metrics.values()]
        ok = any(checks)
        out.append({"type": "app", "name": name, "spec": spec, "raw": "", "ok": ok,
                    "metrics": metrics, "error": "；".join(errs)})
    return out


def _process_alive(name: str) -> bool:
    """进程是否存活（按命令行关键字）。

    坑（验收实测）：`pgrep -f order_service` 会匹配到**执行 pgrep 的那条 sh 命令自己**
    （它的命令行里就含这个关键字）→ 进程已经死了也报"存活"，服务状态少报一级
    （本该 down，被降成 degraded）。修法用经典的"首字符方括号"技巧：
    模式写成 `[o]rder_service`，自己的命令行里是 `[o]rder_service` 这个**字面量**，
    匹配不上该正则；真实进程的命令行是 `order_service`，照旧命中。
    """
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.\-/]", "", name)
    if not safe:
        return False
    pat = "[%s]%s" % (safe[0], safe[1:])     # 防自杀匹配
    try:
        r = subprocess.run(["sh", "-c", "pgrep -f '%s' | head -1" % pat],
                           capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


# ═══════════════════════════════════════════════════════════════════
# P5（v0.4）：平台→Agent 受控任务通道 —— 日志取证（log_fetch）与受控动作（svc_action）
#
# 为什么在 Agent 里做筛选而不是把原始日志整段传回平台：
#   · 相关性优先而非行数优先——先按时间窗定位，再本地筛（级别/错误签名/重复折叠/首错优先），
#     只回传相关片段；重复折叠一项通常就能省掉 80~90% 体积；
#   · 覆盖报告（扫描/命中/丢弃/折叠/截断/实际窗口）随结果回传，平台侧 AI 据此决定
#     "要不要再取一轮"（分不清"窗口选错"与"内容不够"就无法做有依据的迭代）。
#
# 安全边界：任务只有两种 kind；log_fetch 只读文件且路径白名单（realpath 后判前缀）；
# svc_action 只认枚举动作 ID + 服务名字符集校验——不存在"把用户输入拼进 shell"的路径。
# ═══════════════════════════════════════════════════════════════════

# ── 安全与资源边界 ────────────────────────────────────────────────
TASK_MAX_BYTES = 256 * 1024          # 单次回传字节上限（超出按相关度截断）
TASK_MAX_SCAN = 200_000       # 单次扫描行数上限（防超大文件拖死 Agent）
TASK_MAX_CONTEXT = 10                # 上下文行数上限
TASK_DEFAULT_CONTEXT = 3
TASK_CMD_TIMEOUT_S = 20

# 日志路径白名单：默认 /var/log，另可经环境变量追加（安装时声明）
_EXTRA_LOG_DIRS = [d for d in os.getenv("AIOPS_LOG_DIRS", "").split(":") if d.strip()]
# macOS 的应用日志常年在这些位置（Homebrew:/opt/homebrew/var/log、/usr/local/var/log、
# 用户级 ~/Library/Logs）——不补进来，Mac 主机的「日志取证」只能取到 /var/log 下的系统日志。
LOG_ALLOWED_DIRS = ["/var/log", "/var/logs", "/tmp",
                    "/opt/homebrew/var/log", "/usr/local/var/log",
                    os.path.expanduser("~/Library/Logs")] + _EXTRA_LOG_DIRS

# 错误签名库（级别过滤之外的第二道精准捞取；按服务类型分组，通用项对所有服务生效）
LOG_SIGNATURES = {
    "common": [
        "error", "exception", "fatal", "panic", "critical", "traceback",
        "connection refused", "no space left", "out of memory", "oom",
        "timeout", "timed out", "permission denied", "denied", "failed", "failure",
        "cannot", "unable to", "abort", "crash", "refused",
    ],
    "mysql": ["too many connections", "lock wait timeout", "deadlock", "got error",
              "table is full", "server shutdown", "aborted connection", "access denied"],
    "redis": ["oom command not allowed", "max number of clients", "background saving error",
              "misconf", "loading error"],
    "nginx": ["upstream timed out", "no live upstreams", "connect() failed", "ssl_do_handshake",
              "worker process exited", "open() failed", "upstream prematurely closed"],
    "mq": ["connection blocked", "queue full", "channel error", "consumer timeout"],
    "app": ["stack trace", "segfault", "core dumped", "unhandled"],
}

# 时间戳识别（按出现顺序匹配；解析用）
LOG_TS_PATTERNS = [
    # ISO8601: 2026-10-16T10:05:31 / 2026-10-16 10:05:31(.123)（整段一个捕获组）
    (re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s*(Z|[+-]\d{2}:?\d{2})?"),
     "%Y-%m-%dT%H:%M:%S"),
    # nginx: [16/Oct/2026:10:05:31 +0800]
    (re.compile(r"^\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?\]"),
     "%d/%b/%Y:%H:%M:%S"),
    # syslog / 简写: Oct 16 10:05:31（无年份，按当前年补）
    (re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"),
     "%b %d %H:%M:%S"),
    # journalctl 短格式: 10:05:31
    (re.compile(r"^(\d{2}:\d{2}:\d{2})"), "%H:%M:%S"),
]


def task_parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                continue
    return None


def task_line_ts(line: str, now: datetime) -> datetime | None:
    """从行首解析时间戳；解析不出返回 None（该行按"无时间戳"处理）。"""
    for pat, fmt in LOG_TS_PATTERNS:
        m = pat.match(line)
        if not m:
            continue
        raw = m.group(1).replace(" ", "T")     # 统一为 T 分隔（fmt 用 T 形式）
        try:
            dt = datetime.strptime(raw, fmt)
        except Exception:  # noqa: BLE001
            continue
        if "%Y" not in fmt:                      # 补年份
            dt = dt.replace(year=now.year)
        if m.lastindex and m.lastindex >= 2 and m.group(2) and m.group(2) not in (" ", ""):
            off = m.group(2)
            if len(off) == 5:                    # +0800 → +08:00
                off = off[:3] + ":" + off[3:]
            try:
                dt = datetime.fromisoformat(dt.strftime("%Y-%m-%dT%H:%M:%S") + off)
                return dt.astimezone(timezone.utc)
            except Exception:  # noqa: BLE001
                pass
        # 行里没写时区：按**本机本地时区**解释（日志文件就是按主机本地时间写的；
        # journalctl 也按系统本地时间解释 --since）。这一条必须按本地，
        # 否则在非 UTC 主机上窗口会整体错一个时差——实测（UTC+8 的机器）：
        # 平台给 UTC 窗口 03:44~03:56，文件里是本地 11:44，按 UTC 解释就"整窗都在范围外"，
        # 取证一条都取不到，还会给出"请重设窗口"这种指错方向的建议。
        dt = dt.replace(tzinfo=_local_tz())
        return dt.astimezone(timezone.utc)
    return None


def _local_tz():
    """本机时区（带缓存）。容器里通常是 UTC，此时行为与旧版一致。"""
    global _LOCAL_TZ
    if _LOCAL_TZ is None:
        _LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
    return _LOCAL_TZ


_LOCAL_TZ = None


def task_path_allowed(path: str) -> tuple[bool, str]:
    """路径白名单校验：两侧都做 realpath 归一后判前缀。

    为什么两侧都要归一（实测踩到）：macOS 上 `/tmp` 是指向 `/private/tmp` 的软链，
    只归一目标路径会让"/tmp 在白名单里"变成永不命中；同时归一也保证
    `../` 穿越与"软链指向白名单外"这两类绕过都判得住（对抗测试项）。
    """
    try:
        real = os.path.realpath(path)
        roots = [os.path.realpath(d) for d in LOG_ALLOWED_DIRS]
    except Exception as e:  # noqa: BLE001
        return False, f"路径无法解析：{e}"
    if not any(real == d or real.startswith(d.rstrip("/") + "/") for d in roots):
        return False, (f"路径不在允许范围内（仅限 {'、'.join(LOG_ALLOWED_DIRS)}）——"
                       "如需采集其它目录，请在安装时用 AIOPS_LOG_DIRS 声明")
    return True, real


# ── P2：日志路径自动发现（进程 → 打开的日志文件）─────────────────────
# 背景：服务没声明 log_path 时，取证只能走 journald 或"猜路径"（烧预算还不中）。
# 而"这个进程实际打开了哪些文件"在目标机上是现成事实：/proc/<pid>/fd（Linux）
# 或 lsof（macOS），加上 systemd 单元的 StandardOutput/Error 重定向。
# 安全边界：发现 ≠ 采集。候选必须**逐个通过路径白名单**（task_path_allowed）
# 才允许被拉取；白名单外的候选只记录拒绝原因。发现本身纯只读、毫秒级。

def _pids_for_target(unit: str = "", port: int = 0, process: str = "") -> list[int]:
    """定位候选 PID。三条只读途径：systemd MainPID → 监听端口 → 进程名。去重限 8。"""
    pids: list[int] = []
    me = os.getpid()

    def _add(raw) -> None:
        try:
            p = int(raw)
        except (TypeError, ValueError):
            return
        if p > 1 and p != me and p not in pids:
            pids.append(p)

    if unit and shutil.which("systemctl"):
        try:
            out = subprocess.run(["systemctl", "show", unit, "-p", "MainPID", "--value"],
                                 capture_output=True, text=True, timeout=5)
            _add(out.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
    if port:
        if shutil.which("ss"):
            try:
                out = subprocess.run(["ss", "-tlnpt"], capture_output=True, text=True, timeout=5)
                for ln in out.stdout.splitlines()[1:]:
                    if re.search(rf"[:.]{port}\s", ln + " "):
                        m = re.search(r"pid=(\d+)", ln)
                        if m:
                            _add(m.group(1))
            except Exception:  # noqa: BLE001
                pass
        elif shutil.which("lsof"):
            try:
                out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                                     capture_output=True, text=True, timeout=8)
                for tok in out.stdout.split():
                    _add(tok)
            except Exception:  # noqa: BLE001
                pass
    if process and shutil.which("pgrep"):
        try:
            out = subprocess.run(["pgrep", "-f", process], capture_output=True, text=True, timeout=5)
            for tok in out.stdout.split():
                _add(tok)
        except Exception:  # noqa: BLE001
            pass
    return pids[:8]


def _list_open_files(pid: int) -> list[str]:
    """该 PID 打开的路径（Linux /proc/<pid>/fd readlink；macOS/无 /proc 回落 lsof）。"""
    paths: list[str] = []
    fd_dir = f"/proc/{pid}/fd"
    if os.path.isdir(fd_dir):
        try:
            for fd in os.listdir(fd_dir)[:512]:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target.startswith("/"):
                    paths.append(target)
        except Exception:  # noqa: BLE001
            pass
        return paths
    if shutil.which("lsof"):
        try:
            out = subprocess.run(["lsof", "-w", "-Fn", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=10)
            for ln in out.stdout.splitlines():
                if ln.startswith("n/"):
                    paths.append(ln[1:])
        except Exception:  # noqa: BLE001
            pass
    return paths


def _systemd_log_files(unit: str) -> list[str]:
    """systemd 单元 StandardOutput/Error 的**重定向文件**（journal/inherit 返回空）。"""
    files: list[str] = []
    if unit and shutil.which("systemctl"):
        try:
            out = subprocess.run(["systemctl", "show", unit, "-p", "StandardOutput",
                                  "-p", "StandardError"],
                                 capture_output=True, text=True, timeout=5)
            for kv in out.stdout.splitlines():
                v = kv.split("=", 1)[-1].strip()
                if v.startswith("/") and not v.endswith("/"):
                    files.append(v)
        except Exception:  # noqa: BLE001
            pass
    return files


_LOG_SUFFIX = re.compile(r"\.(log|out|err)(\.\d+)?$", re.I)


def _score_log_candidate(path: str, service: str = "") -> tuple[int, list[str]]:
    """按"像不像日志文件"打分。分数只决定**候选顺序**，不放松白名单。"""
    reasons: list[str] = []
    score = 0
    name = os.path.basename(path).lower()
    low = path.lower()
    if _LOG_SUFFIX.search(name):
        score += 3
        reasons.append("日志类后缀")
    if "/var/log" in low:
        score += 2
        reasons.append("系统日志目录")
    elif re.search(r"/logs?/", low):
        score += 2
        reasons.append("应用日志目录")
    key = (service or "").lower().split("-")[0]
    if key and key in name:
        score += 2
        reasons.append("文件名含服务名")
    if re.search(r"\berr", name):
        score += 3
        reasons.append("错误日志文件名")
    if "(deleted)" in path:
        score -= 4
        reasons.append("已删除（轮转旧句柄）")
    try:
        import stat as _stat
        st = os.stat(path.split(" (deleted)")[0])
        if _stat.S_ISREG(st.st_mode):
            score += 2
            reasons.append("常规文件")
            if not os.access(path.split(" (deleted)")[0], os.R_OK):
                score -= 3
                reasons.append("不可读")
        age = time.time() - st.st_mtime
        if age < 3600:
            score += 2
            reasons.append("1 小时内仍在写")
        elif age < 86400:
            score += 1
    except OSError:
        score -= 5
        reasons.append("文件不可达")
    return score, reasons


def discover_log_candidates(payload: dict) -> dict:
    """按服务定位信息（unit/port/process）找出"最像日志"的打开文件。

    返回 {service, unit, pids, via_fd, candidates:[{path,score,reasons,allowed,deny_reason}], chosen}。
    chosen 一定已通过路径白名单且得分 > 0；找不到就是空串（调用方给可行动的失败说明）。
    """
    d = payload.get("discover") or {}
    service = str(d.get("service") or "").strip()
    unit = str(d.get("unit") or "").strip()
    process = str(d.get("process") or "").strip()
    try:
        port = int(d.get("port") or 0)
    except (TypeError, ValueError):
        port = 0

    pids = _pids_for_target(unit, port, process)
    raw: list[str] = []
    for pid in pids:
        raw.extend(_list_open_files(pid))
    raw.extend(_systemd_log_files(unit))

    seen: dict[str, dict] = {}
    for p in raw:
        if not p or p in seen:
            continue
        score, reasons = _score_log_candidate(p, service)
        allowed, real_or_msg = task_path_allowed(p.split(" (deleted)")[0])
        seen[p] = {"path": p, "score": score, "reasons": reasons,
                   "allowed": bool(allowed),
                   "deny_reason": "" if allowed else str(real_or_msg)}
    candidates = sorted(seen.values(), key=lambda x: (-x["score"], x["path"]))
    usable = [c for c in candidates if c["allowed"] and c["score"] > 0]
    return {"service": service, "unit": unit, "process": process, "port": port,
            "pids": pids, "file_count": len(raw),
            "candidates": candidates[:12],
            "chosen": usable[0]["path"] if usable else ""}


def task_strip_ts(line: str) -> str:
    """剥掉行首时间戳得到"正文"。折叠必须按正文比——否则同一错误只因时间戳不同
    就永远折不掉（实测：40 条重复的 `Too many connections` 一条都没折）。"""
    raw = line.lstrip()
    for pat, _fmt in LOG_TS_PATTERNS:
        m = pat.match(raw)
        if m:
            return raw[m.end():].strip()
    return line.strip()


def _fmt_local(iso: str) -> str:
    """把 UTC ISO 串渲染成本机本地时间（给人看的，用于"文件实际覆盖范围"这类提示）。"""
    dt = task_parse_iso(iso)
    return dt.astimezone(_local_tz()).strftime("%Y-%m-%d %H:%M:%S") if dt else (iso or "")[:19]


def _fmt_win(dt) -> str:
    return dt.astimezone(_local_tz()).strftime("%m-%d %H:%M:%S") if dt else "-"


def task_read_window(path: str, since: datetime | None, until: datetime | None,
                 unit: str = "") -> tuple[list[tuple[str, datetime | None]], dict]:
    """按时间窗取行。**声明了日志文件就先读文件**，读不到再回落到 journald。

    为什么文件优先（服务端真机验收实测）：服务同时声明了 `unit` 与 `log_path` 时，
    真正记录业务错误的是**文件**；先走 journalctl 只会拿回几行 systemd 启停，
    看着"取到了"其实没有任何证据（实测命中 5 行、丢弃 5 行、回传 0 字节），
    而调用方只会以为"这个服务没有错误日志"——比取不到更糟。
    只有当**没有声明文件**、或文件不存在/不可读、或文件在窗口内一行都没命中时，才回落到 journald。
    """
    now = datetime.now(timezone.utc)
    meta = {"source": "", "scanned": 0, "matched": 0}
    lines: list[tuple[str, datetime | None]] = []

    file_usable = bool(path) and os.path.isfile(path) and os.access(path, os.R_OK)
    use_journal = bool(unit) and not file_usable
    if unit and file_usable:
        meta["unit_note"] = (f"该服务也声明了 systemd 单元 {unit}，但已声明的日志文件优先；"
                             "文件里没东西时可改用 unit 走 journald")

    # 档 1：systemd 单元（仅当没有可用的日志文件）
    if use_journal:
        cmd = ["journalctl", "-u", unit, "--no-pager", "-o", "short-iso"]
        # journalctl 按**系统本地时间**解释 --since/--until，故这里把 UTC 窗口换成当地时间
        if since:
            cmd += ["--since", since.astimezone(_local_tz()).strftime("%Y-%m-%d %H:%M:%S")]
        if until:
            cmd += ["--until", until.astimezone(_local_tz()).strftime("%Y-%m-%d %H:%M:%S")]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=TASK_CMD_TIMEOUT_S)
            if out.returncode == 0:
                meta["source"] = "journalctl"
                for ln in out.stdout.splitlines():
                    meta["scanned"] += 1
                    ts = task_line_ts(ln.lstrip(), now)
                    if task_in_window(ts, since, until):
                        lines.append((ln, ts))
                meta["matched"] = len(lines)
                return lines, meta
        except Exception:  # noqa: BLE001 —— 回落到文件档
            pass

    if not os.path.isfile(path):
        return [], {**meta, "source": "missing", "error": f"文件不存在：{path}"}
    if not os.access(path, os.R_OK):
        return [], {**meta, "source": "denied", "error": f"文件不可读（权限不足）：{path}"}

    out_of_window, t_min, t_max = 0, None, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                meta["scanned"] += 1
                if meta["scanned"] > TASK_MAX_SCAN:
                    meta["scan_truncated"] = True
                    break
                ts = task_line_ts(ln, now)
                if ts:
                    t_min = ts if t_min is None or ts < t_min else t_min
                    t_max = ts if t_max is None or ts > t_max else t_max
                if task_in_window(ts, since, until):
                    lines.append((ln.rstrip("\n"), ts))
                elif ts:
                    out_of_window += 1
        # 文件实际覆盖的时间范围：把"窗口选错了"说清楚，比"没取到"有用得多
        meta["file_range"] = [t_min.isoformat() if t_min else "", t_max.isoformat() if t_max else ""]
        meta["out_of_window"] = out_of_window
    except Exception as e:  # noqa: BLE001
        return [], {**meta, "source": "error", "error": f"读取失败：{type(e).__name__}: {e}"}

    meta["source"] = "timestamp" if any(t for _, t in lines) else "keyword"
    meta["matched"] = len(lines)

    # 回落档：文件里窗口内一行都没有，但声明了单元 → journald 再试一次（可能日志在 journal 里）
    if not lines and unit and file_usable:
        j_lines, j_meta = task_read_window("", since, until, unit=unit)
        if j_lines:
            j_meta["unit_note"] = (f"已声明的日志文件 {path} 在窗口内没有内容，"
                                   f"已回落到 systemd 单元 {unit} 的 journald")
            return j_lines, j_meta
        meta["unit_note"] = (f"已声明的日志文件 {path} 在窗口内没有内容，"
                             f"systemd 单元 {unit} 的 journald 在窗口内也没有内容")
    return lines, meta


def task_in_window(ts: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    if ts is None:
        return True                       # 无时间戳的行不参与窗口过滤（交由签名筛）
    if since and ts < since:
        return False
    if until and ts > until:
        return False
    return True


# 栈帧与异常链的续行形态（多语言）：这些行**属于上一条错误行本身**，不是普通上下文，
# 因此不受 context 行数限制——半截堆栈会误导分析（实测：context=1 时最后一行栈帧被丢掉）。
LOG_CONT_RE = re.compile(
    r"^\s+(at\s|File\s\"|\.\.\.|Caused\s+by:|Suppressed:|Traceback|"
    r"[\w$.]+\([\w.$:]+\)|\d+\s+frames?\b)")


# 通用级别词（命中这些词说明"只知道是错误"，不知道是哪一类）——此时用**消息指纹**细化标签。
# 为什么需要（实测）：自研应用的服务类型是 app，用不上 mysql/nginx 那套具体签名库，
# 于是所有错误都被标成同一个簇「error」，"有几类错"这个最有价值的信息就没了。
LOG_GENERIC_SIGS = {"error", "exception", "failed", "failure", "fatal", "panic",
                    "critical", "timeout", "timed out", "abort", "crash", "refused", "denied"}


def task_msg_fingerprint(line: str) -> str:
    """消息指纹：剥时间戳与级别词后，把易变部分归一（数字/hex/路径/引号内容）。
    这样"参数不同的同类错误"能聚到一簇，而"不同类的错误"能分开。"""
    t = task_strip_ts(line)
    t = re.sub(r"^\s*\[[A-Z]+\]\s*", "", t)              # 去掉 [ERROR] 这类级别前缀
    t = re.sub(r"\b[A-Za-z]:[\\/][^\s]*", "<path>", t)      # 路径
    t = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", t, flags=re.I)  # 长 hex（trace id 等）
    t = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", t)             # 数字
    t = re.sub(r"\s+", " ", t).strip()
    return t[:72]


def task_label_for(line: str, sig: str) -> str:
    """最终簇标签：具体签名直接用；通用签名则用"消息指纹"细化。"""
    if sig and sig not in LOG_GENERIC_SIGS:
        return sig
    fp = task_msg_fingerprint(line)
    if not fp:
        return sig or "未分类"
    return fp if not sig else f"{sig}: {fp[:56]}"


def task_match_signature(line: str, svc_type: str) -> str:
    """匹配错误签名。**按"具体度"排序后再匹配**（长词优先、服务专属优先）。

    为什么必须排序（实测踩到）：原先按列表顺序匹配，通用词 `error` 排在最前，
    于是所有错误行都被标成同一个簇「error」——簇标签失去区分度，
    "有几类错"这个最有价值的信息就没了。现在 `too many connections` 这类
    具体签名会优先命中，泛化词只作为兜底。
    """
    low = line.lower()
    cands = list(LOG_SIGNATURES.get(svc_type, [])) + list(LOG_SIGNATURES["common"])
    # 服务专属签名整体优先，同类内长词优先（长词=更具体）
    cands.sort(key=lambda x: (x in LOG_SIGNATURES["common"], -len(x)))
    for sig in cands:
        if sig in low:
            return sig
    return ""


def fetch_log(payload: dict) -> dict:
    """执行 log_fetch：定位 → 筛选 → 折叠 → 封顶 → 覆盖报告。

    返回结构（平台原样入库，供 AI 读）：
      {window:{since,until,source}, scanned, matched, dropped_by_level, deduped, truncated,
       segments:[{signature, count, first_ts, last_ts, lines:[...]}]}
    """
    t0 = time.time()
    path = str(payload.get("path") or "").strip()
    unit = str(payload.get("unit") or "").strip()
    since = task_parse_iso(payload.get("since"))
    until = task_parse_iso(payload.get("until"))
    pattern = str(payload.get("pattern") or "").strip()
    level_only = payload.get("level", "auto")
    ctx_n = min(int(payload.get("context") or TASK_DEFAULT_CONTEXT), TASK_MAX_CONTEXT)
    svc_type = str(payload.get("service_type") or "app").strip().lower()

    # P2：没给 path/unit 时走"进程 → 日志文件"自动发现（候选必须过白名单才被采用）
    disc_block: dict | None = None
    if not path and not unit:
        d = payload.get("discover")
        if not isinstance(d, dict) or not any(d.get(k) for k in ("service", "process", "port", "unit")):
            return {"ok": False, "error": "log_fetch 需要 path 或 unit 之一"
                                          "（或提供 discover 定位信息以自动发现日志文件）"}
        disc_block = discover_log_candidates(payload)
        chosen = disc_block.get("chosen") or ""
        if not chosen:
            allowed = [c for c in disc_block["candidates"] if c.get("allowed")]
            # 只有"日志特征明显（score>0）却被白名单拒绝"的才是真拒绝信号；
            # /dev/null 之类 0 分噪音不挡 journald 回落。
            rejected = [c for c in disc_block["candidates"]
                        if not c.get("allowed") and c.get("score", 0) > 0]
            # 有声明 unit → 日志多半走 journald：回落 journalctl（覆盖报告如实标注来源）
            if not allowed and not rejected and str(d.get("unit") or "").strip():
                unit = str(d.get("unit")).strip()
                disc_block["fallback"] = "journald"
            else:
                if allowed:
                    return {"ok": False, "discovered": disc_block,
                            "error": "自动发现找到候选文件，但没有一个符合日志特征（.log/日志目录/仍在写）——"
                                     "请人工确认后在服务声明里填 log_path"}
                if rejected:
                    return {"ok": False, "denied": True, "discovered": disc_block,
                            "error": ("自动发现到 %d 个候选路径，但都不在日志白名单内（仅限 %s）——"
                                      "可在安装时用 AIOPS_LOG_DIRS 追加目录，或人工确认后在服务声明里填 log_path"
                                      % (len(rejected), ", ".join(LOG_ALLOWED_DIRS[:4])))}
                return {"ok": False, "discovered": disc_block,
                        "error": ("自动发现未找到该进程打开的日志文件（pids=%s）——可能日志走 journald，"
                                  "或进程名/端口未匹配到进程；请在服务声明里补 log_path/unit"
                                  % (disc_block.get("pids") or "无"))}
        if chosen:
            disc_block["fallback"] = "file"
            path = chosen

    if path:
        allowed, real_or_msg = task_path_allowed(path)
        if not allowed:
            # 安全事件：不透露目标是否存在，只说明不在白名单
            return {"ok": False, "error": real_or_msg, "denied": True}
        path = real_or_msg

    lines, meta = task_read_window(path, since, until, unit)
    if meta.get("error"):
        return {"ok": False, "error": meta["error"], "window": task_win(since, until, meta.get("source", ""))}

    # ── 筛选：级别/签名（+可选关键词）判定命中 ──
    kw = None
    if pattern:
        try:
            kw = re.compile(pattern, re.I)
        except re.error:
            kw = re.compile(re.escape(pattern), re.I)

    hit_sig: dict[int, str] = {}
    run_sig = ""                      # 当前"错误块"的签名（含其后紧跟的栈帧续行）
    for i, (ln, _ts) in enumerate(lines):
        sig = task_match_signature(ln, svc_type)
        if sig or (kw is not None and kw.search(ln)):
            run_sig = task_label_for(ln, sig or "pattern")
            hit_sig[i] = run_sig
        elif run_sig and LOG_CONT_RE.match(ln):
            hit_sig[i] = run_sig      # 栈帧/异常链续行 = 错误行的一部分
        else:
            run_sig = ""              # 空行或不相关行 → 当前错误块结束
    dropped_by_level = len(lines) - len(hit_sig)

    # ── 分段：同一签名的命中与其上下文归入同一段 ──
    # 不采用"命中行 + 一个统一上下文桶"：那会把不相干的上下文（健康检查 INFO）混进一个桶，
    # AI 读起来分不清谁是谁的上下文。
    seg_idx: dict[str, set[int]] = {}
    for i, sig in hit_sig.items():
        seg_idx.setdefault(sig, set())
        for j in range(max(0, i - ctx_n), min(len(lines), i + ctx_n + 1)):
            seg_idx[sig].add(j)

    # ── 折叠：同段内"正文"相同的行只留首次，并记录重复次数 ──
    segments = []
    for sig, idxs in seg_idx.items():
        kept: list[str] = []
        seen: dict[str, int] = {}
        count, first_ts, last_ts = 0, None, None
        for j in sorted(idxs):
            ln, ts = lines[j]
            if j in hit_sig:
                count += 1
                if ts:
                    first_ts = ts if first_ts is None or ts < first_ts else first_ts
                    last_ts = ts if last_ts is None or ts > last_ts else last_ts
            body = task_strip_ts(ln)
            if body in seen:
                seen[body] += 1
                continue
            seen[body] = 1
            kept.append(ln)
        segments.append({"signature": sig, "count": count,
                         # 线上载荷必须可 JSON 化：datetime 对象直接 json.dumps 会 TypeError
                         # （实测：任务明明执行成功，回传却因序列化失败而丢结果）
                         # 口径：给**本机本地时间**（用户要拿它和日志文件对着看），另附 UTC
                         "first_ts": _fmt_local(first_ts.isoformat()) if first_ts else "",
                         "last_ts": _fmt_local(last_ts.isoformat()) if last_ts else "",
                         "first_ts_utc": first_ts.astimezone(timezone.utc).isoformat() if first_ts else "",
                         "last_ts_utc": last_ts.astimezone(timezone.utc).isoformat() if last_ts else "",
                         "lines": kept,
                         "repeats": sum(v - 1 for v in seen.values())})

    # 首错优先：按该段首次出现时间排序（时间未知的排最后）
    segments.sort(key=lambda x: (not x["first_ts_utc"], x["first_ts_utc"] or "9999"))

    # ── 封顶：按相关度顺序输出，累计 ≤TASK_MAX_BYTES ──
    total, truncated, out_segments = 0, False, []
    for seg in segments:
        seg_bytes = sum(len(x.encode("utf-8", "ignore")) + 1 for x in seg["lines"])
        if total + seg_bytes > TASK_MAX_BYTES:
            room = TASK_MAX_BYTES - total
            if room <= 512:
                truncated = True
                break
            acc, used = [], 0
            for x in seg["lines"]:
                b = len(x.encode("utf-8", "ignore")) + 1
                if used + b > room:
                    break
                acc.append(x)
                used += b
            seg = {**seg, "lines": acc}
            seg_bytes = used
            truncated = True
        out_segments.append(seg)
        total += seg_bytes
        if truncated:
            break

    # 空结果必须给出"为什么空 + 下一步改什么"——否则 AI 无从判断该不该再取一轮
    # （这是"覆盖报告是迭代前提"的落点：没有它，空结果就是死路）
    hint = ""
    if not out_segments:
        fr = meta.get("file_range") or ["", ""]
        if meta.get("source") == "journalctl" or meta.get("journal_empty"):
            hint = (f"systemd 单元在窗口 [{_fmt_win(since)} ~ {_fmt_win(until)}]（本机时间）内没有可用的日志行——"
                    "该服务可能把日志写在文件里（可在服务声明的 log_path 里指定），或日志已被轮转")
        elif meta.get("out_of_window", 0) and fr[0]:
            hint = (f"时间窗 [{_fmt_win(since)} ~ {_fmt_win(until)}] 未覆盖该文件的日志时间范围"
                    f"（文件内实际为 {_fmt_local(fr[0])} ~ {_fmt_local(fr[1])}，"
                    f"窗口外 {meta['out_of_window']} 行；均为本机本地时间）——"
                    "该文件里的日志多半不在这个时间窗内（例如日志已轮转、或应用写的是别的时间约定），"
                    "可换文件，或按日志里真实发生故障的时刻重设窗口")
        elif not fr[0]:
            hint = ("该文件内没有可识别的时间戳，且按关键词也没抓到错误行——"
                    "请确认文件是否正确，或改用 pattern 指定关键词")
        else:
            hint = ("窗口内取到了日志行，但都不是错误级别的（已被级别/签名过滤掉）——"
                    "可换 pattern 指定关键词，或提高 context 行数看上下文")
    elif truncated:
        hint = "结果已按相关度截断——如需更多，请收窄时间窗或加 pattern 聚焦"

    return {
        "ok": True,
        "hint": hint,
        # 取证来源的补充说明（如"同时声明了 unit，但日志文件优先"）——让用户看得到口径
        "unit_note": meta.get("unit_note", ""),
        "window": task_win(since, until, meta.get("source", "")),
        "scanned": meta.get("scanned", 0),
        "matched": meta.get("matched", 0),
        "dropped_by_level": dropped_by_level,
        "deduped": sum(s.get("repeats", 0) for s in out_segments),
        "truncated": truncated,
        "bytes": total,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "segments": out_segments,
        "file_range": meta.get("file_range") or [],
        "out_of_window": meta.get("out_of_window", 0),
        **({"discovered": disc_block} if disc_block else {}),
    }


def task_win(since: datetime | None, until: datetime | None, source: str) -> dict:
    """回传实际窗口。**同时给两种口径**：

    · since/until —— 主机**本地时间**（用户拿去和日志文件里的时间对着看，一眼能对上）；
    · *_utc —— 原始 UTC（排障与对账用，平台库内一律 UTC）。
    另带 tz_offset 明示本机时差（容器里通常是 +00:00，中国区主机是 +08:00）。
    """
    local = _local_tz()
    return {"since": since.astimezone(local).strftime("%Y-%m-%d %H:%M:%S") if since else "",
            "until": until.astimezone(local).strftime("%Y-%m-%d %H:%M:%S") if until else "",
            "since_utc": since.astimezone(timezone.utc).isoformat() if since else "",
            "until_utc": until.astimezone(timezone.utc).isoformat() if until else "",
            "tz_offset": datetime.now(local).strftime("%z"),
            "source": source}


# ── svc_action：只认枚举动作 ID（无自由文本命令）────────────────────
# 动作矩阵：id → (可执行命令模板, 前置条件说明, 是否可回滚)
SVC_ACTIONS: dict[str, dict] = {
    "restart_service": {
        "cmd": ["systemctl", "restart", "{service}"],
        "pre": "systemd 单元存在且处于 failed/inactive",
        "mode": "systemctl",
    },
    "restart_container": {
        "cmd": ["docker", "restart", "{service}"],
        "pre": "容器存在",
        "mode": "docker",
    },
    "reconnect_pool": {
        # 无通用命令：语义是"让应用重连"，落地为重启（首版与 restart_service 同实现但语义分开，便于审计区分）
        "cmd": ["systemctl", "restart", "{service}"],
        "pre": "应用服务存在",
        "mode": "systemctl",
    },
    "rotate_logs": {
        # 「轮转日志」= 给服务发 HUP 让它重开日志文件（systemd 服务的标准做法：
        # 支持 HUP 的进程收到信号后关闭并重新打开日志句柄）。
        # 刻意**不用 logrotate -f**：那会改写/移动文件，风险量级与重启类差一个数量级，
        # 不该挂在自动恢复开关后面。
        "cmd": ["systemctl", "kill", "-s", "HUP", "{service}"],
        "pre": "服务由 systemd 管理且支持 HUP 重开日志",
        "mode": "systemctl",
    },
    # L3/L4 关键能力：能改配置文件。**安全边界**：
    # ① 路径白名单（{file} 只接受 alnum + ._- 的安全字符集，杜绝 ../../../etc/passwd 之类）
    # ② 删除前自动备份到 /var/backups/opsagent/<原名>.bak.<ts>
    # ③ 不动 /etc/passwd /etc/shadow /etc/sudoers 等高敏路径（路径白名单前置过滤）
    "unlink_nginx_conf": {
        "cmd": ["/bin/rm", "-f", "/etc/nginx/conf.d/{file}"],
        "pre": "路径在 /etc/nginx/conf.d/ 且文件名安全字符集",
        "post": "自动备份到 /var/backups/opsagent/conf.d.<file>.bak.<ts>",
        "mode": "rm",
    },
    "unlink_mariadb_cnf": {
        "cmd": ["/bin/rm", "-f", "/etc/my.cnf.d/{file}"],
        "pre": "路径在 /etc/my.cnf.d/ 且文件名安全字符集",
        "post": "自动备份到 /var/backups/opsagent/my.cnf.d.<file>.bak.<ts>",
        "mode": "rm",
    },
    # L3a 配置急救（有界、可恢复）：nginx -t 报错的 conf.d 文件 → 备份隔离 → 再重启。
    # 解决"配置坏了，普通 restart 永远失败"的死局；只会动 conf.d 下语法检查指名的文件。
    "nginx_fix_bad_conf": {
        "cmd": ["sh", "-c", 'set -e; out=$(nginx -t 2>&1 || true); '
                            'if echo "$out" | grep -qi "emerg\|unknown directive"; then '
                            'f=$(echo "$out" | grep -oE "/etc/nginx/conf\.d/[A-Za-z0-9._-]+\.conf" | head -1); '
                            'if [ -n "$f" ] && [ -f "$f" ]; then mkdir -p /var/backups/opsagent; '
                            'mv "$f" "/var/backups/opsagent/$(date +%s).$(basename "$f").bak"; fi; fi; '
                            'nginx -t; systemctl kill -s SIGKILL nginx 2>/dev/null || true; sleep 1; '
                            'systemctl reset-failed nginx 2>/dev/null || true; systemctl start nginx'],
        "pre": "nginx 服务异常；坏配置会先备份到 /var/backups/opsagent/ 再隔离",
        "post": "nginx -t 通过 + systemd active",
        "mode": "nginx",
    },
    # L3b 配置急救（mariadb）：从**最新**错误日志取 unknown variable → 反查 .cnf 隔离 → 重启；
    # 若仍失败，循环尝试下一个（最多 3 个）。mysqld --help 不校验未知变量（实测 rc=0），不可用。
    "mariadb_fix_bad_cnf": {
        "cmd": ["sh", "-c",
                'B=/var/backups/opsagent; mkdir -p "$B"; i=0; '
                'while [ $i -lt 3 ]; do '
                'log=$( { journalctl -u mariadb --no-pager -n 200 2>/dev/null; '
                'tail -200 /var/log/mariadb/mariadb.log 2>/dev/null; '
                'tail -200 /var/log/mysql/error.log 2>/dev/null; } | tail -400 ); '
                'key=$(echo "$log" | grep -oE "unknown variable .[A-Za-z0-9_.-]+" | tail -1 '
                '| tr -d "\x27" | sed "s/unknown variable //"); '
                'if [ -z "$key" ]; then break; fi; '
                'k2=${key%%=*}; '
                'f=$(grep -rl -- "$k2" /etc/my.cnf.d/ 2>/dev/null | head -1); '
                'if [ -z "$f" ]; then break; fi; '
                'mv "$f" "$B/$(date +%s).$(basename "$f").bak"; '
                'systemctl kill -s SIGKILL mariadb 2>/dev/null || true; sleep 1; '
                'systemctl reset-failed mariadb 2>/dev/null || true; '
                'systemctl start mariadb 2>/dev/null && break; i=$((i+1)); done; '
                'systemctl is-active mariadb'],
        "pre": "mariadb 起不来；隔离含最新 unknown variable 的配置文件后重启",
        "post": "systemd active + 3306 可连",
        "mode": "systemd",
    },
    # 安全 reload：先语法检查再 reload，失败不执行 reload（避免 nginx 启动失败但 reload 成功导致失忆）
    "nginx_reload_safe": {
        # 必须走 sh -c：subprocess 列表模式不会解释 &&（写成列表会把 "&&" 当参数传给 nginx）
        "cmd": ["sh", "-c", "nginx -t && nginx -s reload"],
        "pre": "nginx 配置目录存在",
        "mode": "nginx",
    },
}


def task_service_name_ok(name: str) -> bool:
    """服务名只允许保守字符集（防止把 shell 元字符塞进参数）。"""
    return bool(name) and bool(re.fullmatch(r"[A-Za-z0-9_.@:\\-]{1,64}", name))




# ── run_readonly：受控只读命令（D161，AI 助手取证用）────────────────
# 信任边界与 runbook 只读步骤**完全同源**：前缀白名单 + 写重定向/管道到 sh 拒绝 +
# 禁用模式 + 超时 + 输出封顶。平台侧已校验一遍，这里**再校验一遍**（纵深防御——
# 即使平台被绕过，Agent 也不会执行白名单外的命令）。
RO_PREFIXES = ("df", "free", "top", "ps", "uptime", "cat", "tail", "head", "ls", "grep", "ss", "netstat",
               "systemctl status", "systemctl is-active", "journalctl", "docker ps", "docker logs",
               "openssl s_client", "du", "vmstat", "iostat", "nproc", "uname", "lsof", "pgrep",
               "pstree", "sha256sum", "md5sum", "stat", "wc", "hostname", "id", "date",
           "curl -I", "curl -i")   # D163-7：与平台只读白名单对齐（HTTP 探活检查）
# ⚠️ 刻意不加 "find"：可带 -delete/-exec（写操作），D161-5 逮住过这条后门
# D163-6：RO_DENY 里的 "passwd" 移除——它是**子串**匹配，cat /etc/passwd 这类读用户列表
# 的最高频运维操作被误拦。改密命令（首词 passwd）在 run_readonly 里单独拦（下方）。
RO_DENY = ("rm ", "mkfs", "shutdown", "reboot", "halt", "poweroff", "userdel",
           "iptables", "kill ", "> ", ">>", ";", "&&", "||", "`", "$(",
           "|sh", "| sh", "|bash", "| bash")   # 管道进 shell = 任意代码执行（D161-6）
RO_OUTPUT_CAP = 65536

# D173/P2：技能授权命令的**只读形状**校验（与平台侧 runbook.skill_command_allowed 同规则）。
# 平台侧已判一遍，这里再判一遍（纵深防御）——即使平台被绕过，Agent 也不会执行写命令。
# 三道闸：无 shell 组合字符 → 首词在只读工具集 → 个别工具有子命令约束。
SKILL_CMD_METACHARS = re.compile(r"[;|&`$()\n\r<>]")
SKILL_CMD_SAFE_VERBS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "wc", "du", "df", "stat", "nproc",
    "free", "uptime", "top", "ps", "ss", "netstat", "lsof", "pgrep", "pstree", "vmstat", "iostat",
    "hostname", "id", "date", "ping", "traceroute", "dmesg", "lscpu", "lsblk", "journalctl",
    "openssl", "sha256sum", "md5sum", "find", "systemctl", "docker", "mysql", "mysqldump",
    "psql", "redis-cli", "curl",
}
SKILL_FIND_WRITE_FLAGS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprintf", "-fls")
SKILL_SYSTEMCTL_READONLY = ("status", "is-active", "is-failed", "list-units", "list-unit-files",
                            "show", "cat", "list-timers")
SKILL_DOCKER_READONLY = ("ps", "logs", "images", "inspect", "stats", "top", "version", "info", "port")
SKILL_SQL_READONLY = ("select", "show", "describe", "desc", "explain", "with")
SKILL_REDIS_READONLY = ("get", "mget", "info", "ttl", "type", "llen", "lrange", "lpos", "scard",
                        "smembers", "hget", "hgetall", "hkeys", "hvals", "zrange", "zcard", "exists",
                        "dbsize", "keys", "scan", "strlen", "getrange")


def skill_command_allowed(cmd: str) -> tuple[bool, str]:
    """技能授权命令的只读形状校验（Agent 侧；与平台同规则）。"""
    c = (cmd or "").strip()
    if not c:
        return False, "空命令"
    if SKILL_CMD_METACHARS.search(c):
        return False, "命令含 shell 组合字符（; | & ` $ ( ) < >）"
    parts = c.split()
    verb = parts[0]
    if verb not in SKILL_CMD_SAFE_VERBS:
        return False, f"命令首词「{verb}」不在只读工具集内"
    low = c.lower()
    if verb == "find" and any(f in low for f in SKILL_FIND_WRITE_FLAGS):
        return False, "find 不得带 -delete/-exec 等写操作参数"
    if verb == "systemctl" and not any(low.startswith(f"systemctl {s}") for s in SKILL_SYSTEMCTL_READONLY):
        return False, "systemctl 只允许查询类子命令"
    if verb == "docker" and not any(low.startswith(f"docker {s}") for s in SKILL_DOCKER_READONLY):
        return False, "docker 只允许查询类子命令"
    if verb in ("mysql", "mysqldump", "psql"):
        m = re.search(r"-e\s+(['\"])(.*?)\1", c, re.S) or re.search(r"-c\s+(['\"])(.*?)\1", c, re.S)
        if m:
            stmt = m.group(2).strip().lower()
            if not stmt.startswith(SKILL_SQL_READONLY):
                return False, "SQL 只允许只读语句"
    if verb == "redis-cli" and len(parts) >= 2 and parts[1].lower() not in SKILL_REDIS_READONLY:
        return False, "redis-cli 只允许读命令"
    if verb == "curl" and re.search(r"(?:^|\s)-(?:[XTFdDoO])|--(?:data|form|upload-file|request|output)\b", c, re.I):
        return False, "curl 只允许 GET 类请求"
    return True, "只读形状校验通过"


def run_readonly(payload: dict) -> dict:
    cmd = str(payload.get("cmd") or "").strip()
    if not cmd:
        return {"ok": False, "error": "空命令"}
    if len(cmd) > 500:
        return {"ok": False, "error": "命令过长（≤500 字符）"}
    low = cmd.lower()
    # D163-6：改密码命令（首词 passwd）单独拦——不能放进 RO_DENY（子串会误伤 /etc/passwd）
    if low.split() and low.split()[0] == "passwd":
        return {"ok": False, "error": "passwd（改密码）属于变更命令，不在只读范围", "denied": True}
    if any(m in low for m in RO_DENY):
        return {"ok": False, "error": "命令含被禁止的模式（写重定向/链式/危险命令）", "denied": True}
    # D173/P2：会话作用域的技能授权命令（平台侧已校验一遍，这里再校验一遍——纵深防御）。
    # 放行条件是**精确相等**（不是前缀），避免"授权 du -sh /var/log/* 就放行 du -sh /"这类逃逸。
    allowed_cmds = [str(c).strip() for c in (payload.get("allowed_commands") or []) if str(c).strip()]
    skill_ok = False
    if cmd in allowed_cmds:
        shape_ok, shape_reason = skill_command_allowed(cmd)
        if not shape_ok:
            return {"ok": False, "error": f"授权命令未通过只读形状校验：{shape_reason}", "denied": True}
        skill_ok = True
    if not skill_ok and not any(low.startswith(p) for p in RO_PREFIXES):
        return {"ok": False,
                "error": (f"命令不在只读白名单内（允许前缀：{'/'.join(RO_PREFIXES[:12])}…）"
                          + (f"；本次任务授权命令 {len(allowed_cmds)} 条（需完全一致）" if allowed_cmds else "")),
                "denied": True}
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=TASK_CMD_TIMEOUT_S)
        so = (out.stdout or "")[:RO_OUTPUT_CAP]
        se = (out.stderr or "")[:4096]
        return {"ok": True, "cmd": cmd, "rc": out.returncode,
                "stdout": so, "stderr": se, "truncated": len(out.stdout or "") > RO_OUTPUT_CAP}
    except subprocess.TimeoutExpired:
        return {"ok": False, "cmd": cmd, "error": f"超时（>{TASK_CMD_TIMEOUT_S}s）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "cmd": cmd, "error": f"{type(e).__name__}: {e}"}


def run_action(payload: dict) -> dict:
    """执行 svc_action。**只认动作 ID**；服务名做字符集校验（对抗项：`;rm -rf` 必须被拒）。"""
    action = str(payload.get("action") or "").strip()
    service = str(payload.get("service") or "").strip()
    spec = SVC_ACTIONS.get(action)
    if spec is None:
        return {"ok": False, "error": f"动作不在白名单内：{action!r}（仅允许 {'/'.join(SVC_ACTIONS)}）",
                "denied": True}
    ACTION_NEEDS_SERVICE = {"restart_service", "restart_container", "reconnect_pool", "rotate_logs"}
    if action in ACTION_NEEDS_SERVICE and not task_service_name_ok(service):
        return {"ok": False, "error": "服务名含非法字符（只允许字母数字与 _.@:-）", "denied": True}
    # L3/L4 关键路径：unlink_*_conf 走「自动备份 → 原子改名 = 软删除」
    if action in ("unlink_nginx_conf", "unlink_mariadb_cnf"):
        file_arg = str(payload.get("file") or "").strip()
        if not task_filename_ok(file_arg):
            return {"ok": False, "error": f"文件名不符合安全字符集：{file_arg!r}", "denied": True}
        # 双层路径白名单（防 ../../../etc/passwd 之类）
        if action == "unlink_nginx_conf" and not re.fullmatch(r"[A-Za-z0-9._-]+\.conf", file_arg):
            return {"ok": False, "error": f"nginx conf 文件名非法：{file_arg!r}", "denied": True}
        if action == "unlink_mariadb_cnf" and not re.fullmatch(r"[A-Za-z0-9._-]+\.cnf", file_arg):
            return {"ok": False, "error": f"mariadb cnf 文件名非法：{file_arg!r}", "denied": True}
        # 路径完整性最终校验（杜绝 symlink 逃逸——必须真在指定目录下）
        target_path = f"/etc/nginx/conf.d/{file_arg}" if action == "unlink_nginx_conf" else f"/etc/my.cnf.d/{file_arg}"
        real_target = os.path.realpath(target_path)
        allowed_root = "/etc/nginx/conf.d" if action == "unlink_nginx_conf" else "/etc/my.cnf.d"
        if not real_target.startswith(allowed_root + "/") and real_target != allowed_root:
            return {"ok": False, "error": f"目标路径越界：{real_target}", "denied": True}
        # 自动备份：rename 原子 → 失败拒绝删（保证可恢复）
        import time as _t
        try:
            _os.makedirs("/var/backups/opsagent", exist_ok=True)
            backup_path = f"/var/backups/opsagent/{_t.strftime('%Y%m%d-%H%M%S')}.{file_arg}.bak"
            _os.rename(target_path, backup_path)  # 原子改名 = 删除
        except FileNotFoundError:
            return {"ok": False, "error": f"目标文件不存在：{target_path}"}
        except Exception as e:
            return {"ok": False, "error": f"备份失败拒绝删除以保可恢复：{e}"}
        log.info("L3/L4: 已安全备份并软删除 %s → %s", target_path, backup_path)
        return {"ok": True, "executed": "rename", "backup": backup_path, "src": target_path}
    cmd = [c.replace("{service}", service) for c in spec["cmd"]]
    # D112：僵死（SIGSTOP/D 状态）进程不响应 SIGTERM 且继续占用监听端口，
    # 普通 `systemctl restart` 会因新 master bind 失败而进入 failed 态（真机 L2 实测）。
    # 恢复语义=让服务重新可用：先 SIGKILL 整个 cgroup → reset-failed → start。
    # 仍只作用于**已校验的 unit 名**，无自由命令注入面。
    if action in ("restart_service", "reconnect_pool"):
        cmd = ["sh", "-c",
               f"systemctl kill -s SIGKILL {service} 2>/dev/null; sleep 1; "
               f"systemctl reset-failed {service} 2>/dev/null; systemctl start {service}"]
    t0 = time.time()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TASK_CMD_TIMEOUT_S)
        return {"ok": out.returncode == 0, "cmd": " ".join(cmd), "rc": out.returncode,
                "stdout": out.stdout[-2000:], "stderr": out.stderr[-2000:],
                "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "cmd": " ".join(cmd), "error": f"{type(e).__name__}: {e}"}


def post_check(payload: dict) -> dict:
    """动作后置校验（FR-411「无回滚不执行」的落点）。

    校验三项：systemd 单元是否 active / 端口是否可连 / 进程是否存活（任一通过即视为恢复）。
    平台据此决定是否回滚或转人工。
    """
    import socket

    service = str(payload.get("service") or "")
    port = payload.get("port")
    method = str(payload.get("mode") or "systemctl")
    res: dict = {"checks": []}
    if method == "systemctl":
        try:
            out = subprocess.run(["systemctl", "is-active", service], capture_output=True,
                                 text=True, timeout=10)
            active = out.stdout.strip() == "active"
            res["checks"].append({"name": "systemd_active", "ok": active, "detail": out.stdout.strip()})
        except Exception as e:  # noqa: BLE001
            res["checks"].append({"name": "systemd_active", "ok": False, "detail": str(e)[:80]})
    if port:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
                res["checks"].append({"name": "port", "ok": True, "detail": f"127.0.0.1:{port} 可连"})
        except Exception as e:  # noqa: BLE001
            res["checks"].append({"name": "port", "ok": False, "detail": str(e)[:80]})
    res["ok"] = any(c["ok"] for c in res["checks"]) if res["checks"] else False
    return res


# P0：认证自愈与自监控。
# 401 不再静默吞掉：置 expired 标志，主循环重新注册；403 明确进入 needs_reinstall
# （一次性安装令牌已用且平台没有这台主机），退避等待人工重新接入，不再 crash-loop。
AGENT_VERSION = "0.3.7"
_code_sha_cache: tuple[float, str] | None = None   # (mtime, digest)——文件没变不重算


def _code_sha256() -> str:
    """自身代码文件的 SHA256（D121：随 agent_meta 上报，平台据此判定版本漂移）。
    以文件指纹为准——改代码忘改版本号的坑就此消除。"""
    global _code_sha_cache
    try:
        p = Path(__file__).resolve()
        mt = p.stat().st_mtime
        if _code_sha_cache and _code_sha_cache[0] == mt:
            return _code_sha_cache[1]
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        _code_sha_cache = (mt, digest)
        return digest
    except OSError:
        return ""


def _ver_tuple(v: str):
    try:
        return tuple(int(x) for x in str(v or "").strip().split(".") if x.isdigit())
    except Exception:
        return ()


def self_upgrade(payload: dict, server: str) -> dict:
    """平台下发的自升级（D121，方案 A）：下载→SHA256 校验→原子替换→触发服务重启。

    信任链：本进程本就持有 agent_token 且以 root 运行——升级不引入新权限；
    文件来源是平台自身 serving 的 agent_main.py（与首次安装同一信任根）。
    任何一步失败都返回 ok=False，绝不落坏文件；无 systemd/launchd 的裸进程
    安装无法可靠自重启 → 明确失败并回退「升级命令」人工执行。
    """
    import urllib.request
    import urllib.error
    target_ver = str(payload.get("version") or "").strip()
    want = str(payload.get("sha256") or "").strip().lower()
    if not want:
        return {"ok": False, "error": "payload 缺 sha256（平台侧异常），拒绝升级"}
    # D125：判断"是否已是这份代码"必须用**文件指纹**，不能用版本号——
    # 平台侧 stale 判定已改为指纹驱动（D121），两侧口径必须一致：
    # 同版本号但内容已变（如新增能力而未 bump）时，这里要真升级而不是跳过。
    cur_sha = _code_sha256()
    if want and cur_sha and cur_sha == want:
        return {"ok": True, "note": "已是这份代码（文件指纹一致），跳过"}
    tgt, cur = _ver_tuple(target_ver), _ver_tuple(AGENT_VERSION)
    if tgt and tgt < cur:
        return {"ok": False, "error": f"拒绝降级（平台版本 {target_ver} < 本机 {AGENT_VERSION}）"}
    dst_dir = Path(__file__).resolve().parent
    tmp = dst_dir / "agent_main.py.new"
    try:
        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context  # 平台内网/自签证书兜底
        with urllib.request.urlopen(f"{server.rstrip('/')}/agent_main.py", timeout=120) as r:
            tmp.write_bytes(r.read())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"下载失败：{type(e).__name__}: {str(e)[:140]}"}
    got = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if got != want:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": f"SHA256 不匹配（期望 {want[:12]}… 实得 {got[:12]}…），已放弃替换"}
    os.replace(tmp, dst_dir / "agent_main.py")
    try:
        if Path("/run/systemd/system").exists():
            subprocess.Popen(["systemctl", "restart", "opsagent"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "note": f"已更新到 {target_ver or '最新'}，systemctl restart opsagent 已触发"}
        plist = Path.home() / "Library" / "LaunchAgents" / "com.aiops.agent.plist"
        if plist.exists():
            subprocess.Popen(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.aiops.agent"],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "note": f"已更新到 {target_ver or '最新'}，launchctl kickstart 已触发"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"文件已替换但触发重启失败：{type(e).__name__}: {str(e)[:140]}"}
    return {"ok": False, "error": "未检测到 systemd/launchd，无法自重启——请使用「升级命令」人工执行"}


_AUTH = {"expired": False, "state": "unknown", "last_success_at": "", "last_error": ""}


class AuthExpired(Exception):
    """Agent 凭证被平台拒绝（401）。由主循环接住并重新注册。"""


def _mark_auth_failure(why: str) -> None:
    _AUTH["expired"] = True
    _AUTH["state"] = "auth_failed"
    _AUTH["last_error"] = str(why)[:200]


class PlatformRejected(Exception):
    """平台明确拒绝了一次上报（HTTP 非 200）。异常消息已经是**人话**，直接展示。"""


def _platform_msg(resp) -> str:
    """从平台返回的错误体里取出人话说明（平台错误体：{"code":…,"msg":…,"data":null}）。

    没有它，Agent 只能报 httpx 的原始异常（"Client error '403 Forbidden' for url ..."），
    用户看到的是一串 URL 和状态码，看不出「是平台把这台下线了」这种真正的原因。
    """
    try:
        body = resp.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(body.get("msg") or body.get("detail") or "").strip()
    return ""


def _upload_error(resp, what: str = "上报") -> str:
    """把一次被平台拒绝的上报，翻成一句能照着做的话。"""
    msg = _platform_msg(resp)
    code = resp.status_code
    if code == 403:
        # 平台的 403 在这里只有一个出处：主机被「下线/移出」→ 主动拒绝上报。
        # 平台给的 msg 已经把话说明白了（预期行为 + 数据缓存在本地 + 恢复后自动续传），
        # 直接用它，别再叠一层造成重复。
        if msg:
            return f"{what}被平台拒绝：{msg[:150]}"
        return f"{what}被平台拒绝：主机已被下线或移出（数据已缓存在本地，恢复上线后自动续传）"
    if code == 404:
        return f"{what}被平台拒绝：平台没有这台主机的记录（可能已被移出，需重新接入）"
    if msg:
        return f"{what}被平台拒绝（HTTP {code}）：{msg[:110]}"
    return f"{what}失败（HTTP {code}）"



def _cache_depth(conn) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM cache").fetchone()
        return int(row[0] if row else 0)
    except Exception:  # noqa: BLE001
        return 0


def _agent_meta(conn) -> dict:
    return {
        "version": AGENT_VERSION,
        "code_sha256": _code_sha256(),
        "auth_state": _AUTH["state"],
        "last_success_at": _AUTH["last_success_at"],
        "cache_depth": _cache_depth(conn),
        "last_error": _AUTH["last_error"][:255],
    }


async def _register(client, args, *, initial: bool):
    """注册/重新注册。返回 (ident, token, error)。

    initial=True 时 403 必须退出让 install.sh 失败（D109 的既有语义）；
    运行期 403 返回 needs_reinstall，由主循环退避，避免 launchd/systemd 反复拉起。
    """
    r = await client.post(f"{args.server}/api/ops/agents/register", json={
        "fingerprint": machine_fingerprint(),
        "hostname": args.name or platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "ip": _detect_ip(),
        "agent_version": AGENT_VERSION,
        "install_token": args.install_token,
    })
    if r.status_code == 403:
        msg = f"注册被拒（安装令牌无效或已使用）：{r.text[:160]}"
        if initial:
            print(f"[agent] {msg}", flush=True)
            sys.exit(2)
        return None, None, "needs_reinstall"
    r.raise_for_status()
    data = r.json()["data"]
    _AUTH.update({"expired": False, "state": "ok", "last_error": ""})
    return data["ident"], data["token"], ""


async def fetch_declared_services(client, server: str, ident: str, token: str) -> list[dict] | None:
    """向平台要一份**本机应监控的服务清单**（平台侧声明）。

    取不到（老平台没有这个端点 / 网络抖动）返回 None —— 调用方保留上一份，首次则回落到
    安装时的 `--services`。这样"界面里加了个服务"能在一分钟内真的被探测到，
    而不是永远停在「未知」。
    """
    try:
        r = await client.get(f"{server}/api/ops/agents/config",
                             headers={"X-Ident": ident, "X-Token": token}, timeout=10)
        if r.status_code == 401:
            _mark_auth_failure("服务声明拉取 401")
            _note_config_failure("HTTP 401（Agent 凭证失效，将自动重新注册）")
            return None
        if r.status_code != 200:
            _note_config_failure(f"HTTP {r.status_code}")
            return None
        _config_ok()
        return (r.json().get("data") or {}).get("services") or []
    except Exception as e:  # noqa: BLE001 —— 声明拉取失败不得影响指标上报
        _note_config_failure(f"{type(e).__name__}: {e}")
        return None


async def fetch_task(client, server: str, ident: str, token: str) -> dict | None:
    """向平台要一个任务；无任务返回 None（平台回 204）。失败静默——绝不影响指标上报。"""
    try:
        r = await client.get(f"{server}/api/ops/agents/tasks",
                             headers={"X-Ident": ident, "X-Token": token}, timeout=10)
        if r.status_code == 401:
            _mark_auth_failure("任务拉取 401")
            return None
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            return None
        return (r.json().get("data") or {}).get("task")
    except Exception:  # noqa: BLE001
        return None


def execute_task(task: dict, server: str = "") -> dict:
    """执行任务。任何异常都收敛为 {ok: False, error} —— 任务失败不得拖垮 Agent 主循环。"""
    try:
        kind = task.get("kind")
        payload = task.get("payload") or {}
        if kind == "self_upgrade":
            return self_upgrade(payload, server)
        # D121 兜底：未识别的任务类型但载荷像升级（带 sha256+version）→ 仍走自升级。
        # 价值不在现在（旧文件救不了），而在未来：升级协议再演进时，已部署的
        # agent 不会被"未知任务类型"硬失败卡死，永远可以被下一个版本带走。
        if payload.get("sha256") and payload.get("version"):
            return self_upgrade(payload, server)
        if kind == "run_readonly":
            return run_readonly(payload)
        if kind == "log_fetch":
            return fetch_log(payload)
        if kind == "svc_action":
            res = run_action(payload)
            # 动作后置校验（FR-411：无回滚/校验不进入自动执行）
            if res.get("ok"):
                post_payload = {**payload, **(task.get("post") or {})}
                if not post_payload.get("service"):
                    post_payload["service"] = {"nginx_fix_bad_conf": "nginx",
                                               "mariadb_fix_bad_cnf": "mariadb",
                                               "nginx_reload_safe": "nginx"}.get(payload.get("action"), "")
                res["post_check"] = post_check(post_payload)
            return res
        return {"ok": False, "error": f"未知任务类型：{kind!r}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"任务执行异常：{type(e).__name__}: {e}"}


async def post_result(client, server: str, ident: str, token: str, task_id, result: dict) -> None:
    """回传结果。失败只打印（结果可在下一轮重试由平台侧过期清理兜底）。"""
    try:
        r = await client.post(f"{server}/api/ops/agents/tasks/{task_id}/result",
                              json={"result": result}, headers={"X-Ident": ident, "X-Token": token}, timeout=30)
        if r.status_code == 401:
            _mark_auth_failure("任务结果回传 401")
        elif r.status_code >= 400:
            print(f"[agent] 任务结果回传失败（task={task_id}）: HTTP {r.status_code}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[agent] 任务结果回传失败（task={task_id}）: {type(e).__name__}: {e}", flush=True)


# ---------- 主循环 ----------
# 服务发现的上报节拍（while 循环里每 4 拍发一次 ≈ 1 分钟，平台只保留最近一份）
_beat_state = type("B", (), {"n": 0})()
# 平台侧下发的服务声明（进程内缓存；平台不可达时保留上一份，首次回落到 --services）
_live_declared: list[dict] = []
_config_state = {"warned": False}   # 声明拉取失败只提醒一次（避免刷屏），恢复后重置


def _note_config_failure(why: str) -> None:
    """拉声明失败：只提醒一次并把原因打出来（静默吞掉会让"服务一直未知"无从诊断）。"""
    if not _config_state["warned"]:
        _config_state["warned"] = True
        print(f"[agent] 平台服务声明拉取失败（将回落到安装时的 --services）：{why}", flush=True)


def _config_ok() -> None:
    if _config_state["warned"]:
        print("[agent] 平台服务声明已恢复", flush=True)
        _config_state["warned"] = False


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    # 令牌来源两档：CLI --install-token 优先；未传时读环境变量 AIOPS_INSTALL_TOKEN
    # （compose 部署标准姿势：平台「接入新主机」生成令牌 → 写进 .env → 重建 agent 容器）
    ap.add_argument("--install-token", default=os.environ.get("AIOPS_INSTALL_TOKEN", ""))
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--name", default="", help="注册名（缺省用 hostname；IP 形态时建议指定）")
    ap.add_argument("--probes", default="", help='JSON 数组 [{"name":..,"type":..,"target":..}]')
    # 为什么要 --probes-file（验收实测的崩溃）：把 JSON 直接写进 systemd 单元时，
    # systemd 自己的引用解析会**吃掉双引号**，Agent 拿到的是 `[{name:...}]` → JSONDecodeError
    # → 服务反复崩溃重启（而接入向导默认就带 --probes，等于 Linux/systemd 用户一装就崩）。
    # 改用文件传递，绕开 systemd/launchd/shell 三层引号规则；环境变量 AIOPS_PROBES 同样支持。
    ap.add_argument("--probes-file", default="", help="从文件读探活配置（JSON），与 --probes 二选一")
    ap.add_argument("--services", default="[]",
                    help='P5：声明应用类服务 [{"type":"app","name":"订单服务","port":8081,"process":"order"}]'
                         '（内置的 MySQL/Redis/Nginx/MQ 会自动发现，无需声明）')
    args = ap.parse_args()
    _probes_raw = args.probes or ""
    if not _probes_raw and getattr(args, "probes_file", ""):
        try:
            with open(args.probes_file, "r", encoding="utf-8") as _f:
                _probes_raw = _f.read()
        except Exception as e:  # noqa: BLE001 —— 读不到就当没配探活，不能因此起不来
            print("[agent] 探活配置文件读取失败：%s: %s" % (type(e).__name__, e), flush=True)
    if not _probes_raw:
        _probes_raw = os.environ.get("AIOPS_PROBES", "")
    if not _probes_raw:
        _probes_raw = "[]"
    # 解析失败**不再让 Agent 起不来**：探活是附加能力，坏配置最多没有探活，
    # 但指标上报必须照常（否则一个引号问题就让整台机器离线，用户只会看到"装不上"）。
    try:
        probe_specs = json.loads(_probes_raw)
        if not isinstance(probe_specs, list):
            probe_specs = []
    except Exception as e:  # noqa: BLE001
        print("[agent] 探活配置不是合法 JSON，已忽略该配置继续启动：%s" % e, flush=True)
        probe_specs = []
    svc_specs = json.loads(args.services)
    cache_limit = max(3600 // args.interval, 10)

    conn = cache_init()
    ident, token = None, None
    async with httpx.AsyncClient(timeout=10) as client:
        # 注册（幂等；携带一次性安装令牌）
        # 重试而非退出：容器/边缘场景下 Agent 常先于平台就绪启动，
        # 早期实现注册失败即 raise → 容器 Exited(1)（D7 实测），实为脆弱。
        for attempt in range(1, 61):
            try:
                ident, token, err = await _register(client, args, initial=True)
                if ident:
                    print(f"[agent] 注册成功 ident={ident[:12]}…", flush=True)
                    break
                if err == "needs_reinstall":
                    print("[agent] 注册被拒：需要重新接入", flush=True)
                    return
            except Exception as e:  # noqa: BLE001
                wait = min(2 * attempt, 15)
                print(f"[agent] 等待平台就绪…（{attempt}/60，{wait}s 后重试）{type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(wait)
        if not ident:
            print("[agent] 注册失败：平台在约 10 分钟内不可达", flush=True)
            return

        while True:
            if not ident:
                # P0：运行期 401 → 用机器指纹重新注册。主机记录存在时免安装令牌，
                # 会直接换发新 token；主机记录不存在时 403，明确进入 needs_reinstall。
                try:
                    ident, token, err = await _register(client, args, initial=False)
                except Exception as e:  # noqa: BLE001
                    _AUTH["last_error"] = f"重新注册失败：{type(e).__name__}: {e}"[:255]
                    print("[agent] 重新注册失败（15s 后重试）：%s: %s" % (type(e).__name__, e), flush=True)
                    await asyncio.sleep(15)
                    continue
                if err == "needs_reinstall":
                    _AUTH["state"] = "needs_reinstall"
                    _AUTH["last_error"] = "凭证失效且平台无此主机记录；请在平台生成新令牌后重跑安装命令"
                    print("[agent] ⚠ 需要重新接入：安装令牌已使用且平台没有这台主机记录。"
                          "请在平台『主机管理 → 接入新主机』生成新令牌，重跑安装命令。"
                          "Agent 将每 5 分钟重试一次。", flush=True)
                    await asyncio.sleep(300)
                    continue
                if not ident:
                    await asyncio.sleep(15)
                    continue
                print(f"[agent] 重新注册成功 ident={ident[:12]}…", flush=True)

            beat = _beat_state.n = getattr(_beat_state, "n", 0) + 1
            # P5：每拍顺便问一次有没有任务（空则 204，开销可忽略）；任务失败不影响指标上报
            _task = await fetch_task(client, args.server, ident, token)
            # 平台侧声明（服务清单）：与任务同频拉取——声明变了最多一拍即生效
            _declared = await fetch_declared_services(client, args.server, ident, token)
            if _declared is not None:
                _live_declared[:] = _declared
            if _AUTH["expired"]:
                _AUTH["expired"] = False
                _AUTH["state"] = "auth_failed"
                ident, token = None, None
                print("[agent] 认证失效（401）：停止本轮上报，下一轮重新注册", flush=True)
                await asyncio.sleep(2)
                continue
            if _task:
                _res = execute_task(_task, args.server)
                await post_result(client, args.server, ident, token, _task.get("id"), _res)
                print(f"[agent] 任务完成 kind={_task.get('kind')} ok={_res.get('ok')}", flush=True)
                if _AUTH["expired"]:
                    _AUTH["expired"] = False
                    _AUTH["state"] = "auth_failed"
                    ident, token = None, None
                    await asyncio.sleep(2)
                    continue

            payload = snapshot()
            payload["probes"] = run_probes(probe_specs)
            if beat % 4 == 0:   # D63：服务发现 + 磁盘分区快照约每分钟一次（平台保留最近一份）
                # D146：两件事**各自 try**——此前它们在一行里，服务发现抛异常（容器里
                # psutil.net_connections 需要特权 → AccessDenied）会连带把磁盘快照丢掉，
                # 主机详情于是永远显示「Agent 尚未上报分区明细」。互不影响才对。
                try:
                    payload["services"] = discover_services()
                except Exception as e:  # noqa: BLE001
                    print(f"[agent] 服务发现失败（忽略）: {type(e).__name__}: {e}", flush=True)
                try:
                    payload["disks"] = collect_disks()
                except Exception as e:  # noqa: BLE001
                    print(f"[agent] 磁盘快照失败（忽略）: {type(e).__name__}: {e}", flush=True)
            if beat % 4 == 1:   # P5：服务级监控探测（与发现错开一拍，避免同拍做太多本地命令）
                try:
                    # 平台声明优先（界面里登记的服务要真的被探测）；本地 --services 兜底，
                    # 按名字去重——平台那一份赢，与平台侧"声明优先、Agent 只补空缺"的口径一致。
                    _merged = {str(x.get("name") or ""): x for x in (_live_declared or svc_specs)}
                    for _x in svc_specs:
                        _merged.setdefault(str(_x.get("name") or ""), _x)
                    payload["service_probes"] = probe_services(list(_merged.values()))
                except Exception as e:  # noqa: BLE001 —— 探测失败不得影响指标上报
                    print("[agent] 服务探测失败: %s: %s" % (type(e).__name__, e), flush=True)
            payload["agent_meta"] = _agent_meta(conn)
            # D146：机器硬件配置（CPU 核数/型号、内存、开机时间）——主机详情要能一眼
            # 看出「几核几 G」。静态信息，每次心跳带一份很便宜。
            try:
                payload["hostinfo"] = collect_hostinfo()
            except Exception as e:  # noqa: BLE001 —— 采集失败不得影响心跳上报
                print(f"[agent] 硬件配置采集失败（忽略）: {type(e).__name__}: {e}", flush=True)
            payload["ts"] = datetime.now(timezone.utc).isoformat()
            try:
                _first_report_done = globals().get("_first_report_done", False)
                r = await client.post(
                    f"{args.server}/api/ops/agents/metrics",
                    json=payload, headers={"X-Ident": ident, "X-Token": token},
                )
                if r.status_code == 401:
                    raise AuthExpired()
                if r.status_code not in (200, 204):
                    # D160：平台 /agents/metrics 成功就是 **204 No Content**（成功不回包体，
                    # 省流量）；此前只认 200 → 每次成功上报都被当成失败 → 每 15 秒刷一条
                    # "上报失败"、数据被反复缓存重投、"首个上报已确认"信号永不点亮。
                    # D155：别只抛 httpx 的原始异常（用户看到的是 URL + 状态码，
                    # 看不出真正原因）→ 换成平台给的说明，尤其是 403（被下线/移出）。
                    raise PlatformRejected(_upload_error(r, "上报"))
                r.raise_for_status()
                _AUTH.update({
                    "state": "ok",
                    "last_success_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": "",
                })
                if not _first_report_done:
                    # D125：首个上报被平台接受 —— "装上了且真的在报数据"的端到端确认信号。
                    # （夜莺范本：绿色的服务却从不出现在 UI 里，是最坏的结果。）
                    _first_report_done = True
                    globals()["_first_report_done"] = True
                    print(f"[agent] 首个上报已确认 ident={ident[:12]}…（平台已接收本机指标）", flush=True)
                # 续传缓存
                while True:
                    batch = cache_pop_batch(conn)
                    if not batch:
                        break
                    rb = await client.post(
                        f"{args.server}/api/ops/agents/metrics",
                        json={"batch": batch}, headers={"X-Ident": ident, "X-Token": token},
                    )
                    if rb.status_code == 401:
                        raise AuthExpired()
                    if rb.status_code not in (200, 204):   # D160：续传同走 204
                        raise PlatformRejected(_upload_error(rb, "缓存续传"))
                    rb.raise_for_status()
            except AuthExpired:
                _AUTH.update({"expired": True, "state": "auth_failed", "last_error": "认证失效（401）"})
                ident, token = None, None
                print("[agent] 认证失效（401）：下一轮自动重新注册", flush=True)
                await asyncio.sleep(2)
                continue
            except Exception as e:  # noqa: BLE001 —— 断网走缓存
                cache_push(conn, payload, cache_limit)
                # 平台明确拒绝的（PlatformRejected）消息已是人话，直接用；
                # 其他异常（断网/超时）才用类型名兜底。
                detail = str(e)
                _AUTH["last_error"] = (detail[:255] if isinstance(e, PlatformRejected)
                                       else f"上报失败：{type(e).__name__}: {detail}"[:255])
                print(f"[agent] 上报失败（已缓存）: {_AUTH['last_error']}", flush=True)
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(_main())   # D63 教训：入口改名后此处必须跟上（容器实跑才抓到，单测只调了 discover_services）
