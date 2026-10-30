#!/usr/bin/env bash
# install.sh —— Agent 一键安装 / 卸载（FR-B2；03 §1.4）
#
# 用法：
#   本机执行：  bash install.sh --server http://<平台> --token <一次性安装令牌>
#   管道执行：  curl -fsSL http://<平台>/install.sh | bash -s -- --server http://<平台> --token <令牌>
#   卸载：      bash install.sh --uninstall
#
# 依赖：Python 3.9+（Agent 使用 PEP 585 泛型标注）。脚本会自动在 python3.13/3.12/3.11/3.10/3.9
#       中挑选可用解释器；**找不到时自动准备独立 Python 3.10**（下载 Miniconda 装在
#       ~/.opsagent-python，不动系统 Python；`--no-autofix` 关闭该行为）。
#
# 说明（手册期实测修正的三处问题）：
#  1) 参数改为**真正的解析**（此前是位置式 ${2}/${4}，换个顺序或写 --token 就会错位）；
#     兼容 `--token` 与 `--install-token` 两种写法。
#  2) 管道执行时脚本没有"同级目录"，原实现 `cp "$(dirname "$0")/agent_main.py"` 必然失败 →
#     改为优先用同级文件，否则从平台 `GET /agent_main.py` 下载。
#  3) 补上真正的 `--uninstall`（此前帮助里写了卸载命令却没有对应分支，执行它会再次走安装流程）。
set -euo pipefail

SERVER="http://127.0.0.1:8848"
TOKEN=""
INTERVAL="15"
PROBES=""
PROBES_FILE_ARG=""
AGENT_NAME=""
USER_SUPPLIED_NAME=""
UNINSTALL="0"
AGENT_DIR="${AGENT_DIR:-$HOME/.opsagent}"
SERVICE_NAME="opsagent"
PLIST="$HOME/Library/LaunchAgents/com.aiops.agent.plist"

usage() {
  sed -n '2,10p' "$0" 2>/dev/null || echo "用法：bash install.sh --server http://<平台> --token <令牌> [--interval 15] [--probes '<json>'] | --uninstall"
  exit "${1:-0}"
}

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server)        SERVER="${2:?--server 需要参数}"; shift 2 ;;
    --token|--install-token) TOKEN="${2:?--token 需要参数}"; shift 2 ;;
    --interval)      INTERVAL="${2:?--interval 需要参数}"; shift 2 ;;
    --probes)        PROBES="${2:?--probes 需要参数}"; shift 2 ;;
    --probes-file)   PROBES_FILE_ARG="${2:?--probes-file 需要参数}"; shift 2 ;;
    --name)          AGENT_NAME="${2:?--name 需要参数}"; USER_SUPPLIED_NAME=1; shift 2 ;;
    --agent-dir)     AGENT_DIR="${2:?--agent-dir 需要参数}"; shift 2 ;;
    --no-autofix)    NO_AUTOFIX="1"; shift ;;
    --uninstall)     UNINSTALL="1"; shift ;;
    -h|--help)       usage 0 ;;
    *) echo "未知参数：$1"; usage 1 ;;
  esac
done
SERVER="${SERVER%/}"

# ---------- 卸载 ----------
if [[ "$UNINSTALL" == "1" ]]; then
  echo "==> 卸载 Agent"
  # 幂等检测（用户要求：重复执行卸载要明确告知"已卸载"，而不是静默跑完什么都不说）：
  # 任一安装痕迹存在才算"装过"——plist（macOS）/ systemd 单元 / 安装目录 / PID 文件。
  INSTALLED=0
  if [[ "$(uname)" == "Darwin" ]]; then
    [[ -f "$PLIST" ]] && INSTALLED=1
  else
    [[ -f "/etc/systemd/system/$SERVICE_NAME.service" ]] && INSTALLED=1
  fi
  [[ -d "$AGENT_DIR" ]] && INSTALLED=1
  if [[ "$INSTALLED" != "1" ]]; then
    echo "==> 本机未安装 Agent（或已卸载），无需再次执行。"
    echo "    如需重新接入监控：在平台『主机管理 → 接入新主机』生成安装命令后执行。"
    exit 0
  fi
  if [[ "$(uname)" == "Darwin" ]]; then
    launchctl bootout "gui/$(id -u)/com.aiops.agent" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
  else
    SUDO=""
    [[ "$(id -u)" != "0" ]] && SUDO="sudo"
    # nohup 降级路径的进程（无 systemd 环境）
    if [[ -f "$AGENT_DIR/agent.pid" ]]; then
      kill "$(cat "$AGENT_DIR/agent.pid")" 2>/dev/null || true
      echo "    已停止 nohup 常驻进程（PID $(cat "$AGENT_DIR/agent.pid" 2>/dev/null || echo '?')）"
    fi
    command -v systemctl >/dev/null 2>&1 && {
      $SUDO systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
      $SUDO rm -f "/etc/systemd/system/$SERVICE_NAME.service"
      $SUDO systemctl daemon-reload 2>/dev/null || true
    }
  fi
  rm -rf "$AGENT_DIR"
  echo "==> 卸载完成（服务已停止并移除开机自启；${AGENT_DIR} 已删除，本机指纹随之清除）"
  # 卸载后引导（用户要求把细节做完美）：
  #   下线阈值 45s 来自平台 `services/ingest.py::mark_offline_hosts(offline_after_s=45)`
  #   ——45 秒收不到心跳即判离线，此处如实引用，不编数字。
  echo "    平台侧（无需操作）："
  echo "      · 约 45 秒后，该主机在『主机管理』状态将自动变为 offline"
  echo "      · 主机记录、历史监控数据与案例库全部保留，不会删除"
  echo "      · 该主机的探活不再执行，相关告警随主机离线自然停止"
  echo "    重新接入监控："
  echo "      · 在平台『主机管理 → 接入新主机』生成新的安装命令（令牌一次性）后执行"
  echo "      · 重新接入后按新主机记录（注册名可在命令里用 --name 指定）"
  exit 0
fi

# ---------- 选择解释器（真机实测：系统 python3 可能是 3.6，而 Agent 需要 3.9+） ----------
pick_python() {
  local cands=()
  [[ -n "${PYTHON:-}" ]] && cands+=("$PYTHON")
  cands+=(python3.13 python3.12 python3.11 python3.10 python3.9)
  # macOS 系统 python（/usr/bin/python3，随 Xcode CLT）：不在 app bundle 内，
  # 是 launchd 下唯一可靠的兜底（见下方 D34）
  [[ "$(uname)" == "Darwin" ]] && cands+=(/usr/bin/python3)
  cands+=(python3 python)
  for c in "${cands[@]}"; do
    command -v "$c" >/dev/null 2>&1 || continue
    local real
    real="$(command -v "$c")"
    # D34（macOS 26 实测）：python.org 的 Framework / app-bundle 版 Python 在
    # launchd 下会**卡死在解释器初始化**（进程活着但不执行任何用户代码、日志恒为
    # 0 字节、平台看不到该主机）。系统/Homebrew 的 python 不受影响 → 跳过 app bundle 版。
    case "$real" in
      */Python.framework/*|*.app/Contents/MacOS/*) continue ;;
    esac
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      # 返回**绝对路径**（D32）：launchd/systemd 的 PATH 不含用户级 python 目录。
      echo "$real"; return 0
    fi
  done
  return 1
}

# ---------- 并发锁（D125，对标夜莺 Categraf 安装脚本）----------
# 批量装机时多个进程会同时往同一个 AGENT_DIR 写文件（探针配置被半写、agent 被替换到一半）。
# mkdir 是原子的：抢不到锁就明确退出，而不是安静地互相踩。
mkdir -p "$AGENT_DIR" 2>/dev/null || true
LOCK="$AGENT_DIR/.install.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "✗ 已有另一个安装进程正在进行（锁：${LOCK}）" >&2
  echo "  若确认没有其它安装在跑（可能是上次异常中断留下的），删除锁后重试：rmdir '${LOCK}'" >&2
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# ---------- 配置备份 ----------
# 升级覆盖时保住用户配置：探针声明是用户反复调过的东西，丢了要重新填。
BACKUP_DIR=""
if [[ -d "$AGENT_DIR" && ( -f "$AGENT_DIR/probes.json" || -f "$AGENT_DIR/services.json" ) ]]; then
  BACKUP_DIR="$AGENT_DIR.conf-backup-$(date +%Y%m%d%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  for f in probes.json services.json; do
    [[ -f "$AGENT_DIR/$f" ]] && cp -p "$AGENT_DIR/$f" "$BACKUP_DIR/"
  done
  echo "    现有配置已备份到 $BACKUP_DIR"
fi

echo "==> 1/5 依赖检测"
# 未找到 3.9+ 时**自动修复**（实测目标机：CentOS 7 系统自带 3.6、EPEL 无新版包，用户照提示
# 手工装要么没源要么编译失败——修一次就写进脚本，不让第二个用户再踩）：
#   Linux：下载 Miniconda py310（清华镜像优先、官方兜底）装到 $HOME/.opsagent-python，
#          不动系统 Python；装完直接作为解释器，后续升级重装免下载（检测到就复用）。
#   macOS：best-effort `brew install python@3.11`（macOS 一般已有 CLT python3，极少走到）。
#   `--no-autofix` 关闭自动修复，行为回退为提示后退出。
ensure_python() {
  if PY="$(pick_python)"; then return 0; fi
  if [[ "${NO_AUTOFIX:-0}" != "1" ]]; then
    echo "    未找到 Python 3.9+ → 自动准备独立解释器（不动系统 Python）…"
    # 复用上次自动装好的（幂等：升级/重装不再下载）
    local reuse
    for reuse in "$HOME/.opsagent-python/bin/python3.10" "/opt/opsagent-python/bin/python3.10"; do
      if [[ -x "$reuse" ]] && "$reuse" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        PY="$reuse"
        echo "    复用已就绪的解释器：${PY}（$($PY -V 2>&1)）"
        return 0
      fi
    done
    # ── 兼容矩阵（客户环境实测教训：用户镜像可能是 CentOS 5/6/7、Ubuntu 各代、Alpine、
    #    ARM、国产化系统——不能用"只对一种发行版有效"的装法）。按 libc/内核代际分派：
    #    ① musl（Alpine 系）      → apk add python3（Alpine 官方源带 3.11+，最干净）
    #    ② 现代发行版             → 包管理器 best-effort（apt python3.11 / dnf python3.11）
    #    ③ glibc ≥ 2.17（C7+/U14+）→ Miniconda py310（独立、零系统依赖）
    #    ④ glibc 2.12~2.16（C6 代）→ Miniconda py39 老构建（best-effort，可能失败）
    #    ⑤ glibc < 2.12（C5 代）   → 如实告知不支持（现代 Python 全都跑不起来），给出替代路径
    local libc="glibc" gver=""
    if ldd --version 2>/dev/null | head -1 | grep -qi musl; then
      libc="musl"
    else
      gver="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+(\.[0-9]+)*' | head -1)"
    fi
    # ① musl（Alpine）
    if [[ "$libc" == "musl" ]] && command -v apk >/dev/null 2>&1; then
      echo "    检测到 musl/Alpine → apk add python3"
      apk add --no-cache python3 py3-psutil >/dev/null 2>&1 || apk add --no-cache python3 >/dev/null 2>&1 || true
      if PY="$(pick_python)"; then return 0; fi
    fi
    # ② 现代发行版包管理器 best-effort（装完重新 pick；老的 el7 仓库没有 3.11 会失败，无妨）
    if command -v apt-get >/dev/null 2>&1; then
      echo "    尝试 apt-get install -y python3.11 …"
      apt-get install -y python3.11 >/dev/null 2>&1 || true
      if PY="$(pick_python)"; then return 0; fi
    elif command -v dnf >/dev/null 2>&1; then
      echo "    尝试 dnf install -y python3.11 …"
      dnf install -y python3.11 >/dev/null 2>&1 || true
      if PY="$(pick_python)"; then return 0; fi
    fi
    # ③④ Miniconda 独立解释器（按 glibc 代际选版本；镜像 URL 均已实测 200）
    local arch="x86_64"
    case "$(uname -m)" in aarch64|arm64) arch="aarch64";; esac
    local pyurl="" note=""
    if [[ -z "$gver" || "$(printf '%s\n2.17\n' "$gver" | sort -V | head -1)" == "2.17" ]]; then
      pyurl="Miniconda3-py310_24.7.1-0-Linux-${arch}.sh"; note="py310（glibc ≥ 2.17）"
    elif [[ "$(printf '%s\n2.12\n' "$gver" | sort -V | head -1)" == "2.12" ]]; then
      pyurl="Miniconda3-py39_4.12.0-Linux-${arch}.sh"; note="py39 老构建（glibc 2.12~2.16，老系统 best-effort）"
    fi
    if [[ -n "$pyurl" ]] && { command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; }; then
      echo "    下载独立 Python（${note}，清华镜像优先）…"
      local dest="$HOME/.opsagent-python"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o /tmp/opsagent-miniconda.sh "https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/${pyurl}" \
          || curl -fsSL -o /tmp/opsagent-miniconda.sh "https://repo.anaconda.com/miniconda/${pyurl}" || true
      else
        wget -q -O /tmp/opsagent-miniconda.sh "https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/${pyurl}" \
          || wget -q -O /tmp/opsagent-miniconda.sh "https://repo.anaconda.com/miniconda/${pyurl}" || true
      fi
      if [[ -s /tmp/opsagent-miniconda.sh ]] && bash /tmp/opsagent-miniconda.sh -b -p "$dest" >/dev/null 2>&1 \
         && "$dest/bin/python3.10" -c 'import ssl, sqlite3' 2>/dev/null; then
        PY="$dest/bin/python3.10"
        rm -f /tmp/opsagent-miniconda.sh
        echo "    已就绪：${PY}（$($PY -V 2>&1)）"
        return 0
      fi
      echo "    独立 Python 自动准备失败（下载或安装未成功）" >&2
    fi
    # macOS 兜底：brew（极少走到；macOS 自检一般已通过）
    if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
      echo "    尝试 brew install python@3.11 …"
      brew install python@3.11 >/dev/null 2>&1 || true
      if PY="$(pick_python)"; then return 0; fi
    fi
    # ⑤ 兜底失败：如实告知边界，不甩锅给用户环境
    echo "    自动修复未成功。本机信息：libc=${libc} glibc=${gver:-未知} arch=$(uname -m)" >&2
    if [[ "$libc" == "glibc" && -n "$gver" && "$(printf '%s\n2.12\n' "$gver" | sort -V | head -1)" != "2.12" ]]; then
      echo "    ⚠ 该系统的 glibc（${gver}）低于 2.12（约 CentOS 5 时代）：现代 Python 均无法运行，" >&2
      echo "      自动安装不支持。替代路径：升级系统，或在可达的机器上用 Docker 跑 Agent。" >&2
    fi
  fi
  echo "    ✗ 未找到 Python 3.9+，且自动修复未成功（Agent 用了 PEP 585 泛型标注）" >&2
  echo "      系统默认 python3 是：$(python3 -V 2>&1)" >&2
  echo "      手工方式：RHEL/CentOS/AlibabaCloudLinux → dnf install -y python3.11 python3.11-pip" >&2
  echo "                Debian/Ubuntu              → apt-get install -y python3.11 python3.11-venv" >&2
  echo "      或指定已有解释器：PYTHON=/usr/bin/python3.11 bash install.sh …" >&2
  return 1
}
if ! ensure_python; then
  exit 1
fi
echo "    使用解释器：${PY}（$($PY -V 2>&1)）"

# pip 兜底（实测：Ubuntu 22.04 云主机默认**有 python3 但没有 pip**，
# `ensurepip` 在该发行版的 python3 包下也不可用 → 必须走系统包管理器装）。
# 注意：全程用 `if ! { ...; }` 包住，避免 `set -e` 在兜底链中途静默退出
# （实测过：pip 三连失败时脚本直接退出，用户看不到下面那句可读指引）。
if ! $PY -m pip --version >/dev/null 2>&1; then
  echo "    $PY 缺少 pip，尝试自动安装 …"
  $PY -m ensurepip --upgrade >/dev/null 2>&1 || true
  if ! $PY -m pip --version >/dev/null 2>&1; then
    PKG=""
    command -v apt-get >/dev/null 2>&1 && PKG="apt-get install -y python3-pip"
    [[ -z "$PKG" ]] && command -v dnf >/dev/null 2>&1 && PKG="dnf install -y python3-pip"
    [[ -z "$PKG" ]] && command -v yum >/dev/null 2>&1 && PKG="yum install -y python3-pip"
    if [[ -n "$PKG" ]]; then
      SUDO_PKG=""
      [[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && SUDO_PKG="sudo"
      echo "    用系统包管理器安装 pip：$SUDO_PKG $PKG"
      $SUDO_PKG $PKG >/dev/null 2>&1 || true
    fi
  fi
fi
if ! $PY -c 'import psutil, httpx' 2>/dev/null; then
  echo "    安装 psutil httpx …"
  # 三种策略依次兜底；整条链用 if ! { ...; } 包住（防 set -e 静默退出）
  if ! { $PY -m pip install --user --quiet psutil httpx 2>/dev/null \
         || $PY -m pip install --quiet --break-system-packages psutil httpx 2>/dev/null \
         || $PY -m pip install --quiet psutil httpx; }; then
    echo "✗ 依赖安装失败。请手工执行：$PY -m pip install psutil httpx" >&2
    echo "  若提示缺少 pip：apt-get install -y python3-pip（Debian/Ubuntu）" >&2
    echo "                   dnf install -y python3-pip（RHEL/CentOS/AlibabaCloudLinux）" >&2
    exit 1
  fi
fi
$PY -c 'import psutil, httpx' 2>/dev/null || {
  echo "✗ 依赖安装后仍无法 import。请手工排查：$PY -m pip install psutil httpx" >&2
  exit 1
}

echo "==> 2/5 部署文件到 $AGENT_DIR"
mkdir -p "$AGENT_DIR"
# D144：版本一致性检测 —— 本机已有 agent_main.py 时先取它的指纹与版本号，
# 与远端一致就**不再覆盖文件**，明确打印「无需升级」（重跑安装命令升级
# 不再"假装升级了一遍"），只刷新探活/服务配置。
sha_of() { # $1=file → sha256（无可用工具时输出空串）
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" 2>/dev/null | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
  else echo ""; fi
}
ver_of() { # $1=file → AGENT_VERSION（解析不到输出空串）
  sed -n 's/^AGENT_VERSION *= *"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -1
}
LOCAL_MAIN="$AGENT_DIR/agent_main.py"
LOCAL_SHA=""; LOCAL_VER=""
if [[ -f "$LOCAL_MAIN" ]]; then
  LOCAL_SHA="$(sha_of "$LOCAL_MAIN")"
  LOCAL_VER="$(ver_of "$LOCAL_MAIN")"
fi
NEW_MAIN=""; NEW_VER=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/agent_main.py" ]]; then
  NEW_MAIN="$SCRIPT_DIR/agent_main.py"
  echo "    使用本地 agent_main.py"
else
  # 管道安装（curl | bash）没有同级文件 → 从平台下载。
  # 先下到 .new 再原子替换：下载失败/校验失败时旧文件原封不动。
  echo "    从 $SERVER 下载 agent_main.py"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$SERVER/agent_main.py" -o "$LOCAL_MAIN.new"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$SERVER/agent_main.py" -O "$LOCAL_MAIN.new"
  else
    echo "既没有本地 agent_main.py，也没有 curl/wget 可下载。请安装 curl 后重试。"; exit 1
  fi
  [[ -s "$LOCAL_MAIN.new" ]] || { echo "下载 agent_main.py 失败（$SERVER/agent_main.py 返回空）"; exit 1; }
  # 平台版本号（旧平台没有 /agent_version 端点时回退为解析下载内容）
  if command -v curl >/dev/null 2>&1; then
    NEW_VER="$(curl -fsSL "$SERVER/agent_version" 2>/dev/null | head -1 || true)"
  elif command -v wget >/dev/null 2>&1; then
    NEW_VER="$(wget -qO- "$SERVER/agent_version" 2>/dev/null | head -1 || true)"
  fi
  [[ -n "$NEW_VER" ]] || NEW_VER="$(ver_of "$LOCAL_MAIN.new")"
  # P1-12：下载后做 SHA256 校验（平台提供 /agent_main.py.sha256）。
  # 旧平台没有该端点时只警告不阻断（保持向后兼容）；有哈希但校验失败必须退出，
  # 避免装上一个被篡改/截断的 Agent。
  HASH=""
  if command -v curl >/dev/null 2>&1; then
    HASH="$(curl -fsSL "$SERVER/agent_main.py.sha256" 2>/dev/null || true)"
  elif command -v wget >/dev/null 2>&1; then
    HASH="$(wget -qO- "$SERVER/agent_main.py.sha256" 2>/dev/null || true)"
  fi
  if [[ -n "$HASH" ]]; then
    NEW_SHA_DL="$(sha_of "$LOCAL_MAIN.new")"
    if [[ -n "$NEW_SHA_DL" && "$NEW_SHA_DL" != "${HASH//[[:space:]]/}" ]]; then
      echo "agent_main.py SHA256 校验失败（下载内容与平台哈希不一致）"; exit 1
    elif [[ -z "$NEW_SHA_DL" ]]; then
      echo "    警告：系统没有 sha256sum/shasum，跳过哈希校验"
    fi
  else
    echo "    警告：平台未提供 agent_main.py.sha256，跳过哈希校验"
  fi
  NEW_MAIN="$LOCAL_MAIN.new"
fi
NEW_SHA="$(sha_of "$NEW_MAIN")"
if [[ -f "$LOCAL_MAIN" && -n "$NEW_SHA" && "$NEW_SHA" == "$LOCAL_SHA" ]]; then
  echo "    当前版本 ${LOCAL_VER:-未知} 与远端版本 ${NEW_VER:-${LOCAL_VER:-未知}} 一致，无需升级（仅刷新服务配置）"
  [[ "$NEW_MAIN" == "$LOCAL_MAIN.new" ]] && rm -f "$LOCAL_MAIN.new"
else
  if [[ "$NEW_MAIN" == "$LOCAL_MAIN.new" ]]; then
    mv -f "$LOCAL_MAIN.new" "$LOCAL_MAIN"
  else
    cp "$NEW_MAIN" "$LOCAL_MAIN"
  fi
  if [[ -n "$LOCAL_VER" || -n "$NEW_VER" ]]; then
    echo "    agent_main.py 已更新：${LOCAL_VER:-未知} → ${NEW_VER:-未知}"
  fi
fi

EXTRA_ARGS=()
# 探活配置**一律落文件再传路径**（--probes-file）：直接把 JSON 写进命令行/单元文件时，
# systemd 的引用解析会吃掉双引号 → Agent 拿到 `[{name:...}]` → JSONDecodeError → 崩溃重启循环
# （验收实测：Linux 用户按接入向导（默认带 --probes）安装就是崩溃循环）。
# 文件传递同时绕开 launchd/shell 的引号差异，改探活也不必改单元文件。
#
# D144（最严重的用户报障：升级后探活丢失）：重跑安装命令时不带 --probes，此前单元文件
# 被重写成**没有 --probes-file**，Agent 起来后探活全空。现在：不带 --probes 时
# **复用已有 probes.json**（探针声明是用户反复调过的东西，丢了要重新填）；
# 文件被手工清掉时还能从最近的备份目录找回。
PROBES_FILE=""
if [[ -n "$PROBES" ]]; then
  printf '%s' "$PROBES" > "$AGENT_DIR/probes.json"
elif [[ -n "$PROBES_FILE_ARG" ]]; then
  cp "$PROBES_FILE_ARG" "$AGENT_DIR/probes.json"
fi
if [[ ! -s "$AGENT_DIR/probes.json" ]]; then
  LATEST_BK="$(ls -1dt "$AGENT_DIR".conf-backup-* 2>/dev/null | head -1 || true)"
  if [[ -n "$LATEST_BK" && -s "$LATEST_BK/probes.json" ]]; then
    cp "$LATEST_BK/probes.json" "$AGENT_DIR/probes.json"
    echo "    已从备份恢复探活配置：$LATEST_BK/probes.json"
  fi
fi
if [[ -s "$AGENT_DIR/probes.json" ]]; then
  PROBES_FILE="$AGENT_DIR/probes.json"
  EXTRA_ARGS+=(--probes-file "$PROBES_FILE")
  [[ -z "$PROBES" && -z "$PROBES_FILE_ARG" ]] && echo "    保留既有探活配置：${PROBES_FILE}（重跑安装/升级不会丢失探活）"
fi

# 注册名生成（D56-1 修复 v2；真机实测复现的 bug：名称生成逻辑此前被嵌在
# "平台 agent_main.py 不支持 --name" 的 else 分支里——平台**支持** --name 时反而
# 跳过生成，注册名为空。现在无条件生成；显式 --name 时尊重用户起的名字、不加后缀）
HAVE_NAME=0
if grep -q 'add_argument("--name"' "$AGENT_DIR/agent_main.py" 2>/dev/null; then
  HAVE_NAME=1
else
  echo "    警告：平台的 agent_main.py 不支持 --name（旧版），跳过注册名功能"
fi
if [[ "$HAVE_NAME" == "1" ]]; then
  # 默认取 hostname；但 hostname 为空或是 IP（云厂商/Mac 常见，实测 192.168.0.185）时
  # 自动生成友好名（Mac-xxxx / host-xxxx）。
  if [[ -z "$AGENT_NAME" ]]; then
    AGENT_NAME="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo "")"
  fi
  if [[ -z "$AGENT_NAME" || "$AGENT_NAME" =~ ^[0-9.]+$ ]]; then
    # D56-1 修复：不要让 tr 直读无界的 /dev/urandom——head 取完字节就退出，tr 收到 SIGPIPE
    # 非零退出，在 set -euo pipefail 下整条赋值失败 → 脚本静默退出（用户 Mac 实测）。
    # 先 head 定量读取 64 字节再 tr，管道里没有谁会被 SIGPIPE 打死。
    RAND="$(head -c 64 /dev/urandom 2>/dev/null | LC_ALL=C tr -dc a-z0-9 | head -c 4 || true)"
    [[ "$(uname)" == "Darwin" ]] && AGENT_NAME="Mac-${RAND:-$(date +%s | tail -c 4)}" || AGENT_NAME="host-${RAND:-$(date +%s | tail -c 4)}"
  fi
  # 批量装机区分（用户要求）：自动名追加 4 位数字标识——**第 1 位取当前秒的个位**，
  # 其余 3 位随机。100 台同模板克隆机也能一眼认出本次注册的是哪台；
  # 同秒碰撞概率仅 1/1000。显式 --name 不追加（用户自己起名自己负责）。
  if [[ -z "$USER_SUPPLIED_NAME" ]]; then
    SEC_DIGIT="$(date +%S | cut -c 2)"
    R3="$(head -c 64 /dev/urandom 2>/dev/null | LC_ALL=C tr -dc 0-9 | head -c 3 || true)"
    AGENT_NAME="${AGENT_NAME}-${SEC_DIGIT}${R3}"
  fi
  # 控制台高亮显示最终注册名（蓝色，批量装机时一眼确认本次是哪台）
  printf '    本机将注册为：\033[1;94m%s\033[0m\n' "$AGENT_NAME"
  EXTRA_ARGS+=(--name "$AGENT_NAME")
fi

echo "==> 3/5 写入服务单元"
if [[ "$(uname)" == "Darwin" ]]; then
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.aiops.agent</string>
  <key>ProgramArguments</key><array>
    <string>${PY}</string><string>-u</string><string>${AGENT_DIR}/agent_main.py</string>
    <string>--server</string><string>${SERVER}</string>
    <string>--install-token</string><string>${TOKEN}</string>
    <string>--interval</string><string>${INTERVAL}</string>
    <string>--name</string><string>${AGENT_NAME}</string>
$( [[ -n "$PROBES_FILE" ]] && printf '    <string>--probes-file</string><string>%s</string>\n' "$PROBES_FILE" )
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${AGENT_DIR}/agent.log</string>
  <key>StandardErrorPath</key><string>${AGENT_DIR}/agent.err</string>
</dict></plist>
PLIST_EOF
  launchctl bootout "gui/$(id -u)/com.aiops.agent" 2>/dev/null || true
  if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    launchctl load "$PLIST"   # 旧系统回退
  fi
  echo "    已注册 launchd 服务：com.aiops.agent（开机自启，崩溃自拉起）"
else
  if [[ "$(id -u)" != "0" ]]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      SUDO="sudo"
    else
      echo "    需要 root 权限写 systemd 单元。请改用：" >&2
      echo "      curl -fsSL $SERVER/install.sh | sudo bash -s -- --server $SERVER --token $TOKEN" >&2
      exit 1
    fi
  else
    SUDO=""
  fi
  # 无 systemd 降级（夯实轮实测补）：Docker 容器、WSL1、精简镜像等环境没有 systemctl，
  # 此前会因 `set -e` 在 `systemctl daemon-reload` 处静默退出——安装"看起来跑了但没装成"。
  # 降级为 nohup 常驻 + 写 PID 文件；卸载分支同步处理（kill PID）。
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "    未检测到 systemd（容器/精简系统）→ 降级为 nohup 常驻启动"
    PIDFILE="$AGENT_DIR/agent.pid"
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      sleep 1
    fi
    # -u（无缓冲）：Python 在非 tty 下 stdout/stderr 是**全缓冲**，不加 -u 时
    # agent.log 会长期为空（实测：进程在跑、指标在上报，但日志 0 字节）——排障时看不到东西。
    nohup "$PY" -u "$AGENT_DIR/agent_main.py" --server "$SERVER" --install-token "$TOKEN" \
      --interval "$INTERVAL" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
      >"$AGENT_DIR/agent.log" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "    已启动（nohup, PID $(cat "$PIDFILE")）；日志：$AGENT_DIR/agent.log"
      echo "    ⚠ 该环境无 systemd：不会开机自启，重启后请重新执行安装命令"
    else
      echo "✗ 启动失败，请查看日志：$AGENT_DIR/agent.log" >&2
      tail -5 "$AGENT_DIR/agent.log" >&2 || true
      exit 1
    fi
  else
  $SUDO tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<UNIT_EOF
[Unit]
Description=AI Ops Platform Agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${PY} -u ${AGENT_DIR}/agent_main.py --server ${SERVER} --install-token ${TOKEN} --interval ${INTERVAL} ${EXTRA_ARGS[*]:-}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT_EOF
  $SUDO systemctl daemon-reload
  # 必须用 restart，不能用 `enable --now`：后者对**已在运行**的服务不会重启，
  # 于是"重跑安装命令升级 Agent"会静默失败——进程还是旧的（验收实测：服务器上的
  # 旧进程从昨天一直跑到当天中午，用户以为升级了、实际仍在跑旧代码，取证/新能力全都不生效）。
  # enable 只为开机自启，失败不阻断（用户级 sudo 权限受限时也能装上跑起来）。
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  $SUDO systemctl restart "$SERVICE_NAME"
  echo "    已启用 systemd 服务：${SERVICE_NAME}（开机自启，崩溃自拉起；重跑安装命令会重启到新版本）"
  fi
fi

echo "==> 4/5 启动完成"

# ---- 注册校验（D109）：等最多 45s，确认本机指纹真的出现在平台主机列表里。
# 用 curl 调 /api/ops/hosts（只要 read 权限——但平台上 read 需要登录，改用
# 轻量方式：直接查 Agent 本地日志里有没有"注册成功"字样，Agent 注册失败会
# 写明确错误并 exit 2（脚本 start 侧随 systemd Restart 会反复拉起，此处等待
# 日志出现成败信号即可判定）。无 curl/wget 时跳过校验（尽力而为）。 ----
service_running() {
  # 服务托管真的活着吗？（D125：日志说注册成功、服务却没跑起来是最容易被忽略的"半成功"）
  if [[ "$(uname)" == "Darwin" ]]; then
    launchctl print "gui/$(id -u)/com.aiops.agent" >/dev/null 2>&1
  else
    command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICE_NAME"
  fi
}

verify_registration() {
  local LOG="$AGENT_DIR/agent.log"
  local waited=0
  # macOS launchd 场景 agent.log 由 plist 指向；systemd 场景是 journalctl
  local last_line=""
  while (( waited < 45 )); do
    if command -v systemctl >/dev/null 2>&1 && [[ "$(uname)" != "Darwin" ]]; then
      last_line="$(journalctl -u "$SERVICE_NAME" --no-pager -n 3 --output=cat 2>/dev/null | tail -2 | tr '\n' ' ')"
    else
      last_line="$(tail -3 "$LOG" 2>/dev/null | tr '\n' ' ')"
    fi
    if [[ "$last_line" == *"注册成功"* || "$last_line" == *"首个上报已确认"* ]]; then
      echo "    ✓ 平台已确认注册（ident=$(echo "$last_line" | grep -oE 'ident=[0-9a-f]{12}' | head -1)）"
      if service_running; then
        echo "    ✓ 服务托管运行中"
        return 0
      fi
      echo "    ⚠ 已注册但**服务没有在运行**——平台会看到主机，但采集/告警全停" >&2
      echo "      Linux：sudo systemctl restart $SERVICE_NAME && systemctl status $SERVICE_NAME" >&2
      echo "      macOS：launchctl kickstart -k gui/$(id -u)/com.aiops.agent" >&2
      return 1
    fi
    if [[ "$last_line" == *"注册被拒"* ]]; then
      echo "✗ 平台拒绝注册：${last_line:0:200}" >&2
      echo "  常见原因：安装令牌无效/已使用——请在平台重新生成一次性令牌后重试" >&2
      return 1
    fi
    sleep 3
    waited=$((waited + 3))
  done
  # D125：45 秒未见确认 → 给出**可执行的排查三步**，而不是一句"人工确认状态"
  # （对标夜莺："绿色的服务却从不出现在 UI 里，是最坏的结果"——含糊的提示同样算失败）。
  echo "⚠ 45 秒内未确认安装成功——按序排查：" >&2
  echo "  1) 服务是否在跑：$([[ "$(uname)" == "Darwin" ]] && echo 'launchctl print gui/$(id -u)/com.aiops.agent' || echo "systemctl status $SERVICE_NAME")" >&2
  echo "  2) 能否出站访问平台：curl -I $SERVER/api/health" >&2
  echo "  3) 安装令牌是否有效（一次性、可能已被用过）：到平台『主机管理 → 接入新主机』重新生成" >&2
  if ! service_running; then
    echo "  ⚠ 当前检测到服务**未运行**——重新执行安装命令会覆盖修复" >&2
  fi
  return 0   # 不阻断主流程（可能只是网络慢），但排查路径已明确
}
verify_registration || true   # 校验失败不阻断主流程（让 5/5 照常打印收尾信息），但失败原因已打印

echo "==> 4/5 启动完成"
echo "==> 5/5 验证：稍候 20s 后在平台『主机管理』查看本机（状态应为 up）

$( [[ "$HAVE_NAME" == "1" ]] && printf '    本机注册名：\033[1;94m%s\033[0m\n    （在平台『主机管理』按此名字查找；可在列表中随时改名）\n' "$AGENT_NAME" || \
       printf '    （当前平台 agent_main.py 为旧版，未启用注册名；升级平台后重新执行安装即可）\n' )"
echo "    卸载（复制整条执行）：
      curl -fsSL ${SERVER}/install.sh | bash -s -- --uninstall
    （curl 管道安装后本机没有 install.sh 文件，直接敲 bash install.sh --uninstall 会报文件不存在）"
