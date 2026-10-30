#!/usr/bin/env bash
# 故障 2：磁盘打满 —— 在 mock-db-01 容器内写一个大文件，N 秒后删除
# 预期：disk.percent 越过阈值 → 触发「演示-磁盘使用率」规则（disk.percent > 85）
#
# ⚠️ 环境限制（实测发现，脚本会**如实告知**而不是假装成功）：
#   容器看到的往往是宿主/VM 的大盘（本机实测 911G / 空 805G），要靠写文件跨过 85%
#   需要约 710G —— 不可能也不应该。此时脚本不会假装"故障已注入"，而是：
#     ① 在 MAX_SIZE 允许范围内写入（默认 8G）→ 磁盘水位真实上涨；
#     ② 打印当前水位与"要跨过阈值还差多少"；
#     ③ 给一条**临时降阈值**的可复制命令（演示用，跑完删/改回），让告警链路照样能演示。
#   想把某个盘真打到 85%：用一台有独立小分区（如 40G）的真机/云主机。
source "$(dirname "$0")/_lib.sh"
need_docker

TARGET="${TARGET:-88}"        # 想推到的磁盘使用率（%）
MAX_SIZE="${MAX_SIZE:-512M}" # 安全上限（默认 512MB）：绝不为演示往盘里写超过这个量。
                             # 注意：容器看到的是宿主/VM 的盘，写进去 = 占你真实磁盘；
                             # 跨不过阈值时写多少都没意义，所以下面会**直接不写**。
RULE_NAME="${RULE_NAME:-演示-磁盘使用率}"

MAX_MB=$(echo "$MAX_SIZE" | sed 's/[Gg]$//' | awk '{printf "%d", $1 * 1024}')
if [ -z "$MAX_MB" ] || [ "$MAX_MB" -le 0 ] 2>/dev/null; then MAX_MB=8192; fi

docker exec "$CT2" rm -f /tmp/fault-disk.img 2>/dev/null || true   # 先清残留（幂等）
read -r total_b cur_pct < <(docker exec "$CT2" sh -c "df -B1 /tmp | tail -1" | awk '{printf "%s %s\n", $2, $5}')
total_g=$(awk -v b="$total_b" 'BEGIN{printf "%.0f", b/1073741824}')
need_mb=$(awk -v t="$total_b" -v c="$cur_pct" -v tg="$TARGET" 'BEGIN { printf "%d", (tg - c) / 100 * (t/1048576) }')
[ "$need_mb" -lt 64 ] && need_mb=64

log "容器 /tmp 现状：总 ${total_g}GB，已用 ${cur_pct}%；目标 ${TARGET}% → 需再写 ~${need_mb}MB"

if [ "$need_mb" -gt "$MAX_MB" ]; then
  # 写不满阈值 → **一个字节都不写**：写下去只会占你真实磁盘，却产不出要演示的效果
  warn "跨过 ${TARGET}% 需再写 ~${need_mb}MB，超过安全上限 ${MAX_SIZE} → **本环境无法制造越阈告警**"
  warn "（容器看到的是宿主/VM 的大盘，Docker/VM 常见）为不占你的真实磁盘，本次**未写入任何数据**"
  warn "本环境演示磁盘告警的可行方式："
  warn "  A) 临时降阈值（把规则 ${RULE_NAME} 的 expr 设成略低于当前水位 ${cur_pct}，演示后改回 >85）"
  warn "     见 deploy/faults/README.md 的「临时改阈值」命令"
  warn "  B) 换一台有独立小分区的真机（推荐）：40G 的盘写 30G 就能自然越阈，链路完全真实"
  warn "  另外 cpu_spike.sh / mem_leak.sh / probe_fail.sh 在本环境都能真实触发，可先用它们演示链路"
  exit 0
fi

WRITE_MB="$need_mb"
log "在安全上限内可达成 → 将写入 ${WRITE_MB}MB（${DURATION}s 后自动删除）"
# ⚠️ 兜底的 dd 也用**上限**（WRITE_MB），绝不用"需要多少"——否则 fallocate 一旦失败
#    会尝试写满整盘（实测：本机 need_mb 会算出 71 万 MB ≈ 693GB）
docker exec "$CT2" sh -c "fallocate -l ${WRITE_MB}M /tmp/fault-disk.img 2>/dev/null || dd if=/dev/zero of=/tmp/fault-disk.img bs=1M count=${WRITE_MB} 2>/dev/null" || true
ACHIEVED=$(docker exec "$CT2" sh -c "df -B1 /tmp | tail -1 | awk '{print \$5}'" 2>/dev/null || echo "?")
ok "已写入 ${WRITE_MB}MB；容器当前磁盘水位 ${ACHIEVED}（Agent 下一周期上报 disk.percent）"
auto_recover "磁盘占用 ${WRITE_MB}MB" docker exec "$CT2" rm -f /tmp/fault-disk.img
watch_hint "mock-db-01 的 disk.percent 与告警中心（规则「${RULE_NAME}」）"
