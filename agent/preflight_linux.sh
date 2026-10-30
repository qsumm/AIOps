#!/usr/bin/env bash
# preflight_linux.sh —— 目标机环境自检（Linux）· **只读，不修改系统任何东西**
#
# 用法（推荐管道执行，平台直接分发）：
#   curl -fsSL <平台地址>/preflight_linux.sh | bash -s -- --server <平台地址>
# 需 sudo 时：
#   curl -fsSL <平台地址>/preflight_linux.sh | sudo bash -s -- --server <平台地址>
# 本机执行：
#   bash preflight_linux.sh --server http://<平台地址>:8848
#
# 退出码：0 = 可以安装；1 = 有阻断项（照输出的提示处理后再跑一次）；2 = 系统用错了（该用 macOS 版）
#
# 为什么要先自检（而不是直接装）：Agent 对目标机有四个硬要求——操作系统、
# Python 版本、能否出站访问平台、有没有服务托管（systemd/launchd）。
# 这些在安装脚本里也会失败，但那时用户只看到一句报错、不知道要改什么。
# 自检把"能不能装、差什么、怎么补"一次说清，**装不装由用户决定**（本脚本不改任何东西）。
set -uo pipefail

SERVER="${AIOPS_SERVER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --server=*) SERVER="${1#*=}"; shift ;;
    -h|--help) sed -n '2,18p' "$0" 2>/dev/null || true; exit 0 ;;
    *) shift ;;
  esac
done
[ -n "${SERVER}" ] || SERVER="http://127.0.0.1:8848"
SERVER="${SERVER%/}"

OSNAME="$(uname -s 2>/dev/null || echo unknown)"
if [ "${OSNAME}" != "Linux" ]; then
  echo "这不是 Linux（检测到 ${OSNAME}）。请改用 macOS 版自检脚本："
  echo "  curl -fsSL ${SERVER}/preflight_macos.sh | bash -s -- --server ${SERVER}"
  exit 2
fi

PASS=0; WARN=0; FAIL=0
ok()   { printf '  [通过]   %s\n' "$1"; PASS=$((PASS + 1)); }
note() { printf '  [注意]   %s\n' "$1"; WARN=$((WARN + 1)); }
bad()  { printf '  [不通过] %s\n' "$1"; FAIL=$((FAIL + 1)); }
info() { printf '  [信息]   %s\n' "$1"; }

PRETTY="$(sed -n 's/^PRETTY_NAME="\?\([^"]*\)"\?/\1/p' /etc/os-release 2>/dev/null | head -1)"
echo "AI 运维监控平台 · 目标机环境自检（Linux）"
echo "平台地址：${SERVER}"
echo "本机：$(hostname 2>/dev/null || echo '?') · ${PRETTY:-未知发行版} · $(uname -r) · $(uname -m)"
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
  SRV_EPOCH="$(date -u -d "${SRV_DATE}" +%s 2>/dev/null || echo "")"
  [ -n "${SRV_EPOCH}" ] || SRV_EPOCH="$(python3 -c 'import sys,email.utils,calendar;print(calendar.timegm(email.utils.parsedate(sys.argv[1])))' "${SRV_DATE}" 2>/dev/null || echo "")"
  LOC_EPOCH="$(date -u +%s)"
  if [ -n "${SRV_EPOCH}" ]; then
    DIFF=$((SRV_EPOCH - LOC_EPOCH))
    [ "${DIFF}" -lt 0 ] && DIFF=$((-DIFF))
    if [ "${DIFF}" -le 60 ]; then
      ok "时间同步：本机与平台相差 ${DIFF}s"
      SKIPPED_TS=0
    elif [ "${DIFF}" -le 300 ]; then
      SKIPPED_TS=0
      note "时间同步：本机与平台相差 ${DIFF}s（建议开启 NTP：timedatectl set-ntp true；偏差过大会让日志取证的时间窗与告警时刻对不上）"
    else
      SKIPPED_TS=0
      bad "时间同步：本机与平台相差 ${DIFF}s ——请先校准：timedatectl set-ntp true && systemctl restart systemd-timesyncd"
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
PY_CANDS="${AIOPS_PREFLIGHT_PYTHON:-python3.13 python3.12 python3.11 python3.10 python3.9 python3}"
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
    bad "Python 标准库缺件（ssl 或 sqlite3 不可导入）——请安装完整版 python3（如 dnf install -y python3）"
  fi
elif [ -n "${PY9}" ]; then
  note "Python：${PY9}（${PYVER}）可用，但建议 3.10+（安装脚本会优先挑更高版本）"
else
  bad "Python：未找到 3.9+ 解释器"
  info "        无需手工处理：执行安装命令时脚本会**自动**下载独立 Python 3.10"
  info "        （清华镜像，装在 ~/.opsagent-python，不动系统 Python），装完即可用。"
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
MEM_MB="$(free -m 2>/dev/null | awk 'NR==2{printf "%d", $7}')"
[ -z "${MEM_MB}" ] && MEM_MB="$(free -m 2>/dev/null | awk 'NR==2{printf "%d", $4}')"
if [ -z "${MEM_MB}" ]; then
  info "内存：未能读取可用内存 ——本项**未检查**"
elif [ -n "${MEM_MB}" ]; then
  if [ "${MEM_MB}" -ge 256 ]; then ok "内存：可用 ${MEM_MB}MB"
  elif [ "${MEM_MB}" -ge 128 ]; then note "内存：可用仅 ${MEM_MB}MB（Agent 约 40MB，但机器已经很紧张）"
  else bad "内存：可用仅 ${MEM_MB}MB ——请先释放内存再装"; fi
fi
if [ -w "${HOME}" ]; then ok "写权限：${HOME} 可写（Agent 装在 ~/.opsagent）"
else bad "写权限：${HOME} 不可写 ——请用正常登录用户执行（或 sudo 时指定 -H 保留 HOME）"; fi
if [ "$(id -u)" = "0" ]; then
  ok "权限级别：root（可写 systemd 单元，开机自启 + 崩溃自拉起）"
elif command -v sudo >/dev/null 2>&1; then
  note "权限级别：非 root，但有 sudo ——安装时会用 sudo 写 systemd 单元（需输入密码）；也可不用 sudo（降级为用户级常驻）"
else
  note "权限级别：非 root 且无 sudo ——将降级为用户级 nohup 常驻（重启不自动拉起）"
fi

echo
echo "── 4. 服务托管与既有安装 ──────────────────────"
if [ -d /run/systemd/system ]; then
  ok "服务托管：systemd 可用（开机自启、崩溃自拉起）"
elif command -v systemctl >/dev/null 2>&1; then
  note "服务托管：有 systemctl 但 systemd 未作为 1 号进程运行（容器/旧系统？）——将降级为 nohup 常驻"
else
  note "服务托管：无 systemd ——将降级为 nohup 常驻（重启后需重新拉起，或自行加进启动脚本）"
fi
if [ -f /.dockerenv ] || grep -qa 'docker\|kubepods' /proc/1/cgroup 2>/dev/null; then
  note "运行环境：检测到容器（容器内通常无 systemd；容器重建后 Agent 需重新拉起）"
fi
if [ -f "${HOME}/.opsagent/agent_main.py" ]; then
  AVER="$(sed -n 's/.*"agent_version": "\([0-9.]*\)".*/\1/p' "${HOME}/.opsagent/agent_main.py" 2>/dev/null | head -1)"
  note "已安装 Agent${AVER:+（本机版本 ${AVER}）}——本次安装将**升级并重启**它（已注册主机免安装令牌）"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active opsagent >/dev/null 2>&1; then
    ok "        现有 Agent 服务：在运行（安装后会重启到新版本）"
  else
    note "        现有 Agent 服务：未在运行（安装后会拉起）"
  fi
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  info "防火墙：firewalld 在运行 ——**无需**为 Agent 开放任何入站端口（只需出站）"
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
