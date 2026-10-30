#!/usr/bin/env bash
# preflight_macos.sh —— 目标机环境自检（macOS）· **只读，不修改系统任何东西**
#
# 用法（推荐管道执行，平台直接分发）：
#   curl -fsSL <平台地址>/preflight_macos.sh | bash -s -- --server <平台地址>
# 本机执行：
#   bash preflight_macos.sh --server http://<平台地址>:8848
#
# 退出码：0 = 可以安装；1 = 有阻断项（照输出的提示处理后再跑一次）；2 = 系统用错了（该用 Linux 版）
#
# 为什么要先自检（而不是直接装）：Agent 对目标机有四个硬要求——操作系统、
# Python 版本、能否出站访问平台、有没有服务托管（launchd/systemd）。
# 这些在安装脚本里也会失败，但那时用户只看到一句报错、不知道要改什么。
# 自检把"能不能装、差什么、怎么补"一次说清，**装不装由用户决定**（本脚本不改任何东西）。
#
# 兼容性：macOS 自带 bash 3.2（不用关联数组/不依赖 bash 4 语法）。
set -uo pipefail

SERVER="${AIOPS_SERVER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --server=*) SERVER="${1#*=}"; shift ;;
    -h|--help) sed -n '2,16p' "$0" 2>/dev/null || true; exit 0 ;;
    *) shift ;;
  esac
done
[ -n "${SERVER}" ] || SERVER="http://127.0.0.1:8848"
SERVER="${SERVER%/}"

OSNAME="$(uname -s 2>/dev/null || echo unknown)"
if [ "${OSNAME}" != "Darwin" ]; then
  echo "这不是 macOS（检测到 ${OSNAME}）。请改用 Linux 版自检脚本："
  echo "  curl -fsSL ${SERVER}/preflight_linux.sh | bash -s -- --server ${SERVER}"
  exit 2
fi

PASS=0; WARN=0; FAIL=0
ok()   { printf '  [通过]   %s\n' "$1"; PASS=$((PASS + 1)); }
note() { printf '  [注意]   %s\n' "$1"; WARN=$((WARN + 1)); }
bad()  { printf '  [不通过] %s\n' "$1"; FAIL=$((FAIL + 1)); }
info() { printf '  [信息]   %s\n' "$1"; }

echo "AI 运维监控平台 · 目标机环境自检（macOS）"
echo "平台地址：${SERVER}"
echo "本机：$(hostname 2>/dev/null || echo '?') · $(sw_vers -productVersion 2>/dev/null || echo '?') · $(uname -m)"
echo "说明：自检**只读**，不写文件、不装软件、不改系统配置。约 5~10 秒。"
echo
echo "── 1. 网络与平台 ─────────────────────────────"

HEALTH="$(curl -fsS --max-time 8 "${SERVER}/api/health" 2>/dev/null || true)"
PVER=""
if [ -n "${HEALTH}" ]; then
  PVER="$(printf '%s' "${HEALTH}" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  ok "平台连通性：${SERVER} 可达（平台版本 ${PVER:-未知}）"
else
  bad "平台连通性：访问 ${SERVER}/api/health 失败"
  info "        目标机只需**出站**能访问该地址（http/https + 端口）；不需要开放任何入站端口。"
  info "        公司网络受限时，请让网络管理员放行到 ${SERVER} 的出站访问，再重跑本自检。"
fi

# 用 GET 取响应头（+ -D - 输出头）：HEAD 在部分部署形态下不被支持（实测部署实例上
# HEAD /api/health 返回 404，而本地开发实例是 200）——用 HEAD 会**静默跳过**这项检查。
SRV_DATE="$(curl -fsS --max-time 8 -D - -o /dev/null "${SERVER}/api/health" 2>/dev/null | sed -n 's/^[Dd]ate: //p' | tr -d '\r' | head -1)"
SKIPPED_TS=1
if [ -n "${SRV_DATE}" ]; then
  SRV_EPOCH="$(date -u -j -f "%a, %d %b %Y %T GMT" "${SRV_DATE}" +%s 2>/dev/null || echo "")"
  LOC_EPOCH="$(date -u +%s)"
  if [ -n "${SRV_EPOCH}" ]; then
    DIFF=$((SRV_EPOCH - LOC_EPOCH))
    [ "${DIFF}" -lt 0 ] && DIFF=$((-DIFF))
    if [ "${DIFF}" -le 60 ]; then
      ok "时间同步：本机与平台相差 ${DIFF}s"
      SKIPPED_TS=0
    elif [ "${DIFF}" -le 300 ]; then
      SKIPPED_TS=0
      note "时间同步：本机与平台相差 ${DIFF}s（建议开启 NTP；偏差过大会让日志取证的时间窗与告警时刻对不上）"
    else
      SKIPPED_TS=0
      bad "时间同步：本机与平台相差 ${DIFF}s ——请先校准系统时间（系统设置 → 通用 → 日期与时间 → 自动设置）"
    fi
  fi
fi
if [ "${SKIPPED_TS}" = "1" ]; then
  info "时间同步：未能从平台响应取到时间戳 ——本项**未检查**（不影响安装；但请自行确认目标机时间与平台一致，偏差大会影响告警时刻与取证时间窗）"
fi
info "入站端口：无需开放（Agent 采用**拉取式**通道，主动外连平台取任务）"

echo
echo "── 2. Python 运行时 ───────────────────────────"
PY=""; PY9=""; PYVER=""
# 解释器候选：默认按推荐顺序找；客户把 python 装在非常规路径（如 /opt/py311/bin/python3）时
# 可用 AIOPS_PREFLIGHT_PYTHON 覆盖（空格分隔），自检与安装脚本挑同一个解释器。
PY_CANDS="${AIOPS_PREFLIGHT_PYTHON:-python3.13 python3.12 python3.11 python3.10 python3.9 /usr/bin/python3 python3}"
for c in ${PY_CANDS}; do
  command -v "${c}" >/dev/null 2>&1 || continue
  v="$("${c}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
  [ -n "${v}" ] || continue
  maj="${v%%.*}"; min="${v##*.}"
  [ "${maj}" = "3" ] || continue
  if [ "${min}" -ge 10 ] && [ -z "${PY}" ]; then PY="${c}"; PYVER="${v}"; fi
  if [ "${min}" -ge 9 ] && [ "${min}" -lt 10 ] && [ -z "${PY9}" ]; then PY9="${c}"; PYVER="${v}"; fi
done
if [ -n "${PY}" ]; then
  ok "Python：${PY}（${PYVER}）"
  if "${PY}" -c 'import ssl, sqlite3, json, asyncio, subprocess' 2>/dev/null; then
    ok "Python 标准库：ssl / sqlite3 / json / asyncio 均可用"
  else
    bad "Python 标准库缺件（ssl 或 sqlite3 不可导入）——请改装 python.org 官方版或 Homebrew 的 python"
  fi
elif [ -n "${PY9}" ]; then
  note "Python：${PY9}（${PYVER}）可用，但建议 3.10+（系统自带 3.9 能跑，安装脚本会优先挑更高版本）"
else
  bad "Python：未找到 3.9+ 解释器"
  info "        无需手工处理：安装脚本会尝试自动 brew install python@3.11（需已装 Homebrew）。"
  info "        没有 brew 或想手动：① 官方安装包 https://www.python.org/downloads/macos/ ；"
  info "        ② brew install python@3.12 ；③ xcode-select --install（自带 3.9）。"
  info "        装在非常规路径时，可用 AIOPS_PREFLIGHT_PYTHON=/opt/py312/bin/python3 指定后再跑自检"
fi

echo
echo "── 3. 资源与权限 ──────────────────────────────"
AVAIL_MB="$(df -Pk "${HOME}" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024}')"
if [ -z "${AVAIL_MB}" ]; then
  info "磁盘余量：未能读取 ${HOME} 的可用空间 ——本项**未检查**"
elif [ -n "${AVAIL_MB}" ]; then
  if [ "${AVAIL_MB}" -ge 200 ]; then ok "磁盘余量：${HOME} 可用 ${AVAIL_MB}MB"
  elif [ "${AVAIL_MB}" -ge 50 ]; then note "磁盘余量：${HOME} 仅剩 ${AVAIL_MB}MB（Agent 很轻，但建议先清理）"
  else bad "磁盘余量：${HOME} 仅剩 ${AVAIL_MB}MB ——请先清理（不足 50MB 会装不上）"; fi
fi
MEM_MB="$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%d", $1/1048576}')"
if [ -z "${MEM_MB}" ]; then
  info "内存：未能读取总内存 ——本项**未检查**"
elif [ "${MEM_MB}" -ge 512 ]; then ok "内存：${MEM_MB}MB"
else note "内存：仅 ${MEM_MB}MB（Agent 约 40MB，能跑但很紧）"; fi
if [ -w "${HOME}" ]; then ok "写权限：${HOME} 可写（Agent 装在 ~/.opsagent）"
else bad "写权限：${HOME} 不可写 ——请用当前用户的正常登录会话执行"; fi

echo
echo "── 4. 服务托管与既有安装 ──────────────────────"
if command -v launchctl >/dev/null 2>&1 && launchctl print "gui/$(id -u)" >/dev/null 2>&1; then
  ok "服务托管：launchd 可用（开机自启、崩溃自拉起）"
else
  note "服务托管：launchd 当前不可用 ——将降级为 nohup 常驻（重启不会自动拉起）"
fi
if [ -f "${HOME}/.opsagent/agent_main.py" ]; then
  AVER="$(sed -n 's/.*"agent_version": "\([0-9.]*\)".*/\1/p' "${HOME}/.opsagent/agent_main.py" 2>/dev/null | head -1)"
  note "已安装 Agent${AVER:+（本机版本 ${AVER}）}——本次安装将**升级并重启**它（已注册主机免安装令牌）"
  if launchctl print "gui/$(id -u)/com.aiops.agent" >/dev/null 2>&1; then
    ok "        现有 Agent 服务：在运行（安装后会重启到新版本）"
  else
    note "        现有 Agent 服务：未在运行（安装后会拉起）"
  fi
fi

echo
echo "── 自检结论 ───────────────────────────────────"
printf '  通过 %d 项 · 注意 %d 项 · 不通过 %d 项\n' "${PASS}" "${WARN}" "${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "  结论：**可以安装**${WARN:+（有 ${WARN} 项注意，不改也能装）}。"
  echo
  echo "  下一步（在平台『主机管理 → 接入新主机』里复制你的专属命令，形如）："
  echo "    curl -fsSL ${SERVER}/install.sh | bash -s -- --server ${SERVER} --token <安装令牌>"
  exit 0
fi
echo "  结论：**暂不建议安装**——请先处理上面 [不通过] 的项，处理完重跑本自检。"
exit 1
