"""D74：接入前的**环境自检脚本**（先检测 → 给结论 → 用户决定 → 再安装）。

为什么要有它（用户要求）：Agent 对目标机有硬要求——操作系统、Python 版本、能否出站访问平台、
服务托管（launchd/systemd）。以前这些只在文档里，用户装到一半才失败、也不知道该改什么。
现在给两条只读的自检命令（macOS / Linux 各一份），先给结论与修复指引，**装不装由用户决定**。

本文件锁四件事：
  ① 两个脚本都守 **bash 3.2**（macOS 自带）兼容——同一个坑 D31 已经在 install.sh 上踩过：
     变量后紧跟中文全角字符会被误当变量名（实测我自己在新脚本里又踩了一次）；
  ② 自检**只读**：不得出现安装/改系统/删文件的命令（否则"先检测后决定"就变成了偷偷动手）；
  ③ 平台要能分发这两个脚本（管道执行的入口）；
  ④ 行为正确：能连上平台 → 退出 0 且给"可安装"结论；连不上 → 退出 1 且给阻断项与修法；
     系统用错 → 退出 2 并指向正确的那个脚本。
"""
import http.server
import json
import platform
import re
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
AGENT = ROOT / "agent"
SCRIPTS = [AGENT / "preflight_macos.sh", AGENT / "preflight_linux.sh"]


# ── ① bash 3.2 兼容（与 D31 同一类缺陷，扩到全部 agent 脚本）──
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_no_unbraced_var_before_non_ascii(path):
    hits = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])", line):
            hits.append(f"L{i}: ${m.group(1)}")
    assert not hits, (
        f"{path.name}: 变量后紧跟非 ASCII 字符会触发 macOS bash 3.2 'unbound variable'，"
        f"须写成 ${{VAR}}：{hits}"
    )


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_no_bash4_only_features(path):
    src = path.read_text(encoding="utf-8")
    bad = [p for p in ("declare -A", "&>>", "mapfile", "readarray", "${!", "**/") if p in src]
    assert not bad, f"{path.name} 使用了 bash 4+ 专有特性（macOS bash 3.2 不支持）：{bad}"


# ── ② 只读纪律：自检不得安装/改系统/删文件 ──
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_preflight_is_read_only(path):
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("#")]
    forbidden_pat = re.compile(
        r"^(sudo\b|rm\b|tee\b|mkdir\b|cp\b|mv\b|chmod\b|chown\b|"
        r"systemctl\s+(enable|start|restart|stop|disable)\b|"
        r"launchctl\s+(bootstrap|bootout|kickstart|load|unload)\b|"
        r"dnf\b|yum\b|apt-get\b|apt\b|pip\b|pip3\b|curl\s+-o\b|wget\b|"
        r"firewall-cmd\s+--(add|remove|permanent))")
    hits = [ln for ln in body if forbidden_pat.search(ln)]
    assert not hits, (
        f"{path.name} 违反「只读」纪律——自检不得安装软件/改系统/删文件/开端口，"
        f"否则「先检测后决定」就变成了偷偷动手：{hits}")


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_preflight_declares_readonly_and_decides_nothing(path):
    src = path.read_text(encoding="utf-8")
    assert "只读" in src, "脚本必须自报「只读」（用户要知道自检不会动他的机器）"
    assert "退出码：0 = 可以安装" in src, "必须写明退出码语义（可作 CI/脚本判据）"
    # 结论必须两种都给：能装 / 先别装（并给修法）
    assert "可以安装" in src and "暂不建议安装" in src, "必须同时给出「可装」与「暂不建议」两种结论"


# ── ③ 平台分发 ──
def test_platform_serves_preflight_scripts():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        for name in ("preflight_macos.sh", "preflight_linux.sh", "install.sh", "agent_main.py"):
            r = c.get(f"/{name}")
            assert r.status_code == 200, f"/{name} 必须能被 curl 拿到（管道执行的入口），实际 {r.status_code}"
            assert r.content, f"/{name} 内容为空"
        r = c.get("/preflight_macos.sh")
        assert "shellscript" in r.headers.get("content-type", ""), r.headers.get("content-type")


# ── ④ 行为：连得上/连不上/系统用错 ──
def _fake_platform(version="9.9.9"):
    """起一个极小的假平台（只提供 /api/health），用于确定性地验证脚本行为。"""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/health"):
                body = json.dumps({"code": 0, "data": {"status": "up", "version": version}},
                                  separators=(",", ":")).encode()   # 与真实 FastAPI 一致（紧凑）
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/health") or self.path == "/":
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(path, server, timeout=60):
    return subprocess.run(["bash", str(path), "--server", server],
                          capture_output=True, text=True, timeout=timeout)


def test_preflight_passes_against_reachable_platform():
    """连得上平台 → 退出 0，且结论是「可以安装」（本机 = 目标系统时才跑该系统的脚本）。"""
    is_mac = platform.system() == "Darwin"
    script = AGENT / ("preflight_macos.sh" if is_mac else "preflight_linux.sh")
    srv = _fake_platform()
    try:
        port = srv.server_address[1]
        r = _run(script, f"http://127.0.0.1:{port}")
    finally:
        srv.shutdown()
    assert r.returncode == 0, f"连通平台时应可安装：{r.stdout[-600:]}"
    assert "可以安装" in r.stdout
    assert "9.9.9" in r.stdout, "应回显平台版本（证明真的连上了）"


def test_preflight_blocks_when_platform_unreachable():
    """连不上平台 → 退出 1，并给出「怎么补」（放行出站访问 / 无需入站端口）。"""
    is_mac = platform.system() == "Darwin"
    script = AGENT / ("preflight_macos.sh" if is_mac else "preflight_linux.sh")
    r = _run(script, "http://127.0.0.1:9")   # 9 端口必然不可达
    assert r.returncode == 1, r.stdout[-400:]
    assert "不通过" in r.stdout and "暂不建议安装" in r.stdout
    assert "出站" in r.stdout and "入站" in r.stdout, "要讲清只需出站、无需入站"


def test_preflight_refuses_wrong_os_and_points_to_right_script():
    """系统用错 → 退出 2，并直接给出正确脚本的命令（不让用户自己猜）。"""
    is_mac = platform.system() == "Darwin"
    other = AGENT / ("preflight_linux.sh" if is_mac else "preflight_macos.sh")
    right = AGENT / ("preflight_macos.sh" if is_mac else "preflight_linux.sh")
    r = _run(other, "http://127.0.0.1:9")
    assert r.returncode == 2, r.stdout
    assert right.name in r.stdout, "必须直接指出该用哪个脚本（不让用户自己猜）"

@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_time_check_never_silently_skipped(path):
    """时间同步检查必须"要么给结论、要么明说未检查"——不得静默跳过。

    实测缺陷：脚本原用 `curl -fsSI`（HEAD）取响应头算时间偏差，而**部署实例上 HEAD
    /api/health 返回 404**（本地开发实例是 200）→ 取不到 Date 头 → 整项被静默跳过，
    用户以为"自检全过"，其实这一项根本没查。改为 GET 取头，且取不到就明确写"本项未检查"。
    """
    src = path.read_text(encoding="utf-8")
    assert "-D -" in src, "应通过 GET 取响应头（HEAD 在部分部署形态下不被支持）"
    assert "curl -fsSI" not in src, "不得用 HEAD 取平台时间（会静默跳过检查）"
    assert "本项**未检查**" in src, "取不到时间戳时必须明说未检查"


# ══════════════════════════════════════════════════════════════════
# ⑤ 不满足条件的场景（用户追问："你测了不满足条件的场景吗？"）
#    手法：造一个**沙盒 PATH**（symlink 真实基础命令 + 覆盖我要控制的命令），
#    把每一项不达标都逼出来，断言退出码与"到底哪一项不通过、怎么说"。
# ══════════════════════════════════════════════════════════════════
import os
import shutil

KEEP = ("sed", "awk", "tr", "head", "tail", "grep", "cat", "hostname", "date", "uname",
        "dirname", "sort", "wc", "printf", "df", "id", "command")


def _sandbox(tmp_path, shims=None, keep_extra=(), hide=()):
    """沙盒 bin：基础命令用 `exec` 包装器转发到真实二进制，被控制/隐藏的用脚本覆盖或不放。

    为什么用包装器而不是 symlink：macOS 上给受保护的系统二进制建 symlink 会 EPERM。
    """
    d = tmp_path / "bin"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for name in KEEP + tuple(keep_extra):
        if name in hide or name in (shims or {}):
            continue
        real = shutil.which(name)
        if not real:
            continue
        f = d / name
        f.write_text('#!/bin/sh\nexec "%s" "$@"\n' % real)
        f.chmod(0o755)
    for name, body in (shims or {}).items():
        f = d / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(0o755)
    return d


def _run_path(script, server, bindir, home=None, timeout=60, py=None):
    env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
    if home:
        env["HOME"] = home
    if py is not None:
        # 解释器候选可覆盖（脚本支持 AIOPS_PREFLIGHT_PYTHON；绝对路径的 /usr/bin/python3
        # 是沙盒挡不住的，所以负向用例走这个正式开关）
        env["AIOPS_PREFLIGHT_PYTHON"] = py
    return subprocess.run(["bash", str(script), "--server", server],
                          capture_output=True, text=True, env=env, timeout=timeout)


def _mac():
    return AGENT / "preflight_macos.sh"


def _linux():
    return AGENT / "preflight_linux.sh"


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_python_too_old_is_blocking(tmp_path):
    """Python 只有 3.8：必须**阻断**并给装法（不能"能用就行"糊过去）。"""
    srv = _fake_platform()
    try:
        b = _sandbox(tmp_path)
        r = _run_path(_mac(), f"http://127.0.0.1:{srv.server_address[1]}", b,
                      py="python3.8-that-does-not-exist")
    finally:
        srv.shutdown()
    assert r.returncode == 1, r.stdout
    assert "未找到 3.9+ 解释器" in r.stdout and "python.org" in r.stdout
    assert "暂不建议安装" in r.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_python_39_is_warning_not_blocking(tmp_path):
    """Python 3.9：**可装但提醒**（不误判成阻断——我们已做 3.9 兼容）。"""
    srv = _fake_platform()
    try:
        b = _sandbox(tmp_path, shims={"py39": "echo 3.9"})
        r = _run_path(_mac(), f"http://127.0.0.1:{srv.server_address[1]}", b, py="py39")
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stdout
    assert "建议 3.10+" in r.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_python_stdlib_missing_is_blocking(tmp_path):
    """Python 在，但 ssl/sqlite3 缺件（极简编译版常见）：必须阻断。"""
    shim = ('if echo "$*" | grep -q "import ssl"; then exit 1; fi\n'
            'echo 3.11')
    srv = _fake_platform()
    try:
        b = _sandbox(tmp_path, shims={"py311": shim})
        r = _run_path(_mac(), f"http://127.0.0.1:{srv.server_address[1]}", b, py="py311")
    finally:
        srv.shutdown()
    assert r.returncode == 1, r.stdout
    assert "标准库缺件" in r.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_disk_and_memory_thresholds(tmp_path):
    """磁盘 30MB → 阻断；120MB → 提醒；内存 100MB → 阻断。"""
    srv = _fake_platform()
    url = None
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        b1 = _sandbox(tmp_path / "d1", shims={"df": "echo 'Filesystem 1024-blocks Used Available Capacity Mounted'; echo '/dev/x 200000 169280 30720 85% /'"})
        r1 = _run_path(_mac(), url, b1)
        assert r1.returncode == 1 and "仅剩 30MB" in r1.stdout, r1.stdout

        b2 = _sandbox(tmp_path / "d2", shims={"df": "echo 'Filesystem 1024-blocks Used Available Capacity Mounted'; echo '/dev/x 200000 77120 122880 39% /'"})
        r2 = _run_path(_mac(), url, b2)
        assert r2.returncode == 0 and "仅剩 120MB" in r2.stdout, r2.stdout

        b3 = _sandbox(tmp_path / "d3", shims={"sysctl": "echo 104857600"})   # 100MB
        r3 = _run_path(_mac(), url, b3)
        assert "内存：仅 100MB" in r3.stdout, r3.stdout
    finally:
        srv.shutdown()


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_unreadable_disk_memory_are_stated_not_silent(tmp_path):
    """读不到磁盘/内存时必须明说"未检查"——不得静默跳过（这正是本轮修掉的缺陷形态）。"""
    srv = _fake_platform()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        b = _sandbox(tmp_path, shims={"df": "exit 1", "sysctl": "exit 1"})
        r = _run_path(_mac(), url, b)
    finally:
        srv.shutdown()
    assert "本项**未检查**" in r.stdout and r.stdout.count("未检查") >= 2, r.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_home_not_writable_is_blocking(tmp_path):
    srv = _fake_platform()
    try:
        r = _run_path(_mac(), f"http://127.0.0.1:{srv.server_address[1]}", _sandbox(tmp_path),
                      home="/nonexistent-home-for-preflight")
    finally:
        srv.shutdown()
    assert r.returncode == 1 and "不可写" in r.stdout, r.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_no_launchd_is_warning_and_says_what_happens(tmp_path):
    """没有 launchd：不阻断，但必须**如实告知**会降级为常驻进程（而不是含糊过去）。"""
    srv = _fake_platform()
    try:
        b = _sandbox(tmp_path, shims={"launchctl": "exit 1"})
        r = _run_path(_mac(), f"http://127.0.0.1:{srv.server_address[1]}", b)
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stdout
    assert "降级为 nohup 常驻" in r.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS 脚本的负向分支")
def test_time_skew_zero_warns_and_blocks_by_degree(tmp_path):
    """时间偏差分三档：≤60s 通过；2 分钟 → 提醒（可装）；20 分钟 → **阻断**。

    为什么要分档：时间错会让日志取证的时间窗与告警时刻整体错位（我们踩过 8 小时的时区坑），
    但小偏差（NTP 未收敛）不该拦住安装。
    """
    import email.utils as eu
    import time as _t

    class S(http.server.BaseHTTPRequestHandler):
        skew = 0

        # 覆盖 date_time_string 而不是自己 send_header("Date")：BaseHTTPRequestHandler.send_response
        # 会**自动**再加一个 Date 头，脚本按第一个取值 → 自定义的那个根本不生效（本用例第一版就踩了）
        def date_time_string(self, timestamp=None):
            return eu.formatdate(_t.time() - S.skew, usegmt=True)

        def do_GET(self):  # noqa: N802
            body = b'{"code":0,"data":{"status":"up","version":"9.9.9"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), S)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        S.skew = 0
        r0 = _run_path(_mac(), url, _sandbox(tmp_path / "s0"), timeout=90)
        assert r0.returncode == 0 and "时间同步：本机与平台相差" in r0.stdout, r0.stdout[-300:]

        S.skew = 120
        r1 = _run_path(_mac(), url, _sandbox(tmp_path / "s1"), timeout=90)
        assert r1.returncode == 0, "2 分钟偏差不该阻断安装"
        assert "建议开启 NTP" in r1.stdout, r1.stdout

        S.skew = 1200       # 20 分钟
        r2 = _run_path(_mac(), url, _sandbox(tmp_path / "s2"), timeout=90)
        assert r2.returncode == 1, "20 分钟偏差必须阻断"
        assert "请先校准系统时间" in r2.stdout, r2.stdout
    finally:
        srv.shutdown()


def test_linux_memory_thresholds_and_unreadable(tmp_path):
    """Linux 分支的内存三档：可用 <256MB 提醒、<128MB 阻断、读不到则明说未检查。

    在 macOS 上用 `uname` 沙盒把脚本"伪装"成 Linux 来跑（其余检查项走真实命令）——
    这样 Linux 专属分支也能本地回归，不必等靶机。
    """
    srv = _fake_platform()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        head = "echo '              total        used        free'"

        b1 = _sandbox(tmp_path / "m1", shims={"uname": "echo Linux", "free": head + "; echo 'Mem: 2000 1800 0 0 0 180 0'"})
        r1 = _run_path(_linux(), url, b1)
        assert "内存：可用仅 180MB" in r1.stdout, r1.stdout

        b2 = _sandbox(tmp_path / "m2", shims={"uname": "echo Linux", "free": head + "; echo 'Mem: 2000 1900 0 0 0 100 0'"})
        r2 = _run_path(_linux(), url, b2)
        assert r2.returncode == 1 and "请先释放内存" in r2.stdout, r2.stdout

        b3 = _sandbox(tmp_path / "m3", shims={"uname": "echo Linux", "free": "exit 1"})
        r3 = _run_path(_linux(), url, b3)
        assert "内存：未能读取可用内存 ——本项**未检查**" in r3.stdout, r3.stdout
    finally:
        srv.shutdown()
