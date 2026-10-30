#!/usr/bin/env bash
# 故障 1：CPU 飙高 —— 在 mock-web-01 容器内起 N 个 CPU 满载进程，N 秒后自动结束
# 预期：cpu.percent 冲高 → 触发「演示-CPU高负载」规则 → 告警中心出现 S2 事件
source "$(dirname "$0")/_lib.sh"
need_docker

# 默认占满容器可见的全部核：容器内 Agent 上报的是**整机 CPU 百分比**（psutil 语义），
# 只压 2 核在高核数机器上可能只有 20%，够不到阈值（D7 实测踩过一次）。
CORES="${CORES:-$(docker exec "$CT1" nproc 2>/dev/null || echo 4)}"
docker exec -d "$CT1" stress-ng --cpu "$CORES" --timeout "${DURATION}s"
auto_recover "CPU 飙高（${CORES} 核 × ${DURATION}s）" docker exec "$CT1" pkill -f stress-ng
watch_hint "mock-web-01 的 cpu.percent 与告警中心"
