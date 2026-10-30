"""D31：install.sh 的 bash 3.2（macOS 自带）兼容性回归。

缺陷（用户在 Mac 上实测，安装第一步即失败）：
  echo "使用解释器：$PY（...）"  —— 变量 $PY 后紧跟中文全角括号。
  macOS 自带 bash 3.2 会把多字节字符误当作变量名的一部分 →
  `PY?: unbound variable`（脚本用了 set -u）。Linux 的 bash 5.x 能正确断开，
  所以在服务器上测不出——典型的"只在 macOS 暴露"的兼容性缺陷。
"""
import re
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[2] / "agent" / "install.sh"


def test_no_unbraced_var_before_non_ascii():
    src = INSTALL.read_text(encoding="utf-8")
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])", line):
            hits.append(f"L{i}: ${m.group(1)}")
    assert not hits, (
        "变量后紧跟非 ASCII 字符会触发 bash 3.2 'unbound variable'，须写成 ${VAR}：" + str(hits)
    )


def test_no_bash4_only_features():
    src = INSTALL.read_text(encoding="utf-8")
    bad = [p for p in ("declare -A", "&>>", "mapfile", "readarray", "${!", "**/") if p in src]
    assert not bad, f"install.sh 使用了 bash 4+ 专有特性（macOS bash 3.2 不支持）：{bad}"


def test_pick_python_returns_absolute_path():
    """D32：pick_python 必须返回绝对路径。

    缺陷（用户在 Mac 上实测：agent 装了但平台看不到该主机）：
    plist/systemd 单元里写的是 pick_python 的返回值。若返回命令名 "python3"，
    launchd 的 PATH 不含用户 python 目录（macOS 的 python3 常在
    /Library/Frameworks/.../bin）→ 服务启动即失败（退出码 1）。
    Linux 的 /usr/bin/python3 恰在系统 PATH 里，所以只在 macOS 暴露。
    """
    src = INSTALL.read_text(encoding="utf-8")
    assert 'real="$(command -v "$c")"' in src and 'echo "$real"; return 0' in src, \
        "pick_python 必须返回绝对路径（否则 launchd/systemd 找不到解释器）"


def test_pick_python_skips_app_bundle_on_macos():
    """D34：macOS 上必须跳过 app-bundle 版 python。

    缺陷（用户在 Mac 上实测：agent 装了但平台看不到该主机）：
    python.org 的 Framework/app-bundle 版 Python 在 macOS 26 的 launchd 下**卡死在
    解释器初始化**（进程活着但不执行任何用户代码、日志恒 0 字节）。系统/Homebrew
    的 python 不受影响。故 install.sh 选解释器时必须跳过 app-bundle 版。
    """
    src = INSTALL.read_text(encoding="utf-8")
    assert "Python.framework" in src, "install.sh 必须识别 app-bundle python 并跳过"
    assert "app/Contents/MacOS" in src, "install.sh 必须识别 .app bundle 形式的 python"
    assert "/usr/bin/python3" in src, "macOS 回退候选需含系统 python"


def test_install_sh_restarts_running_service():
    """重跑安装命令（文档写的升级路径）必须**真的重启**服务。

    验收实测缺陷：原实现用 `systemctl enable --now`，对**已在运行**的服务不会重启——
    服务器上的旧进程从昨天一直跑到当天中午，用户重跑安装命令以为升级了，实际还在跑旧代码；
    新能力（服务级监控、日志取证）全都不生效，而且没有任何报错。
    修法：`systemctl enable`（只为开机自启）+ `systemctl restart`（真正换到新版本）。
    """
    import pathlib

    sh = (pathlib.Path(__file__).resolve().parent.parent.parent / "agent" / "install.sh").read_text(encoding="utf-8")
    assert "systemctl restart" in sh, "systemd 路径必须 restart（否则升级不生效）"
    assert "systemctl enable --now" not in sh, \
        "`enable --now` 对运行中的服务不会重启 → 重跑安装命令等于没升级（验收实测踩到）"


def test_install_sh_passes_probes_via_file_not_inline_json():
    """探活配置必须**落文件传路径**，不能把 JSON 内联进 systemd 单元。

    验收实测（服务端真机）：systemd 会吃掉 ExecStart 里的双引号 → Agent 收到
    `[{name:平台HTTP,type:...}]` → JSONDecodeError → 服务崩溃重启循环。
    而接入向导**默认就带 --probes**，等于 Linux 用户按向导装就装不上。
    """
    import pathlib

    sh = (pathlib.Path(__file__).resolve().parent.parent.parent / "agent" / "install.sh").read_text(encoding="utf-8")
    assert "--probes-file" in sh, "必须用 --probes-file 传探活配置"
    assert 'EXTRA_ARGS+=(--probes "$PROBES")' not in sh, "不得把 JSON 内联进命令行（systemd 会吃掉引号）"
    assert '<string>--probes</string>' not in sh, "plist 也不得内联 JSON（含 & < 时是非法 XML）"
    agent = (pathlib.Path(__file__).resolve().parent.parent.parent / "agent" / "agent_main.py").read_text(encoding="utf-8")
    assert '"--probes-file"' in agent, "Agent 必须支持 --probes-file"
    # 坏配置不得让 Agent 起不来（探活是附加能力，指标上报必须照常）
    assert "探活配置不是合法 JSON，已忽略该配置继续启动" in agent, "坏探活配置必须降级而不是崩溃"
    assert 'os.environ.get("AIOPS_PROBES"' in agent, "应支持 AIOPS_PROBES 环境变量兜底"
