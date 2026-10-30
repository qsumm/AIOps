#!/usr/bin/env bash
# 故障 4：探活失败 —— 直接停掉 mock-db-01 的业务服务进程，N 秒后拉起
# 预期：探活「业务端口/业务HTTP」down + 进程探活失败 → 触发 S1 探活告警
source "$(dirname "$0")/_lib.sh"
need_docker

docker exec "$CT2" pkill -f mock_service.py || true
auto_recover "业务服务进程被杀" docker exec -d "$CT2" sh -c "cd /agent && python mock_service.py"
watch_hint "mock-db-01 的探活状态与告警中心"
