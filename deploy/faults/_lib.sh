#!/usr/bin/env bash
# 故障注入公共库：统一日志、容器名解析、自动恢复计时
# 注意：不用 set -u——Auto-recover 里把描述交回子 shell，配合 set -u 会因未导出变量报
#      "desc: unbound variable"（D7 实测），这里改为显式传参。
set -eo pipefail

PLATFORM="${PLATFORM:-http://127.0.0.1:8848}"
DURATION="${DURATION:-180}"          # 故障持续秒数（默认 3 分钟，之后自动恢复）
CT1="${CT1:-aiops-agent-1}"
CT2="${CT2:-aiops-agent-2}"

log()  { printf "\033[36m[fault]\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m[ ok ]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[warn]\033[0m %s\n" "$*"; }

need_docker() {
  command -v docker >/dev/null 2>&1 || { echo "需要 docker"; exit 1; }
  docker ps --format '{{.Names}}' | grep -q "$CT1" || { echo "找不到容器 $CT1，请先 docker compose up -d"; exit 1; }
}

# 自动恢复：后台等待 DURATION 秒后执行恢复命令；描述通过位置参数显式传入子 shell
auto_recover() {
  local duration="$DURATION"
  log "故障已注入：$1（${duration}s 后自动恢复）"
  shift
  (
    sleep "$duration"
    "$@" >/dev/null 2>&1 || true
    log "已自动恢复"
  ) &
}

watch_hint() {
  log "在平台上观察：${PLATFORM}  →  告警中心 / 主机管理 → $1"
}
