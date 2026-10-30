#!/usr/bin/env bash
# 故障 3：进程假死 —— 让 mock-web-01 的业务服务 /health 不再返回（连接挂住），N 秒后恢复
# 预期：探活「业务HTTP」变为 down → 触发探活类规则；同时进程仍存在（running ≠ healthy 的经典场景）
source "$(dirname "$0")/_lib.sh"
need_docker

docker exec "$CT1" curl -sf "http://127.0.0.1:8080/_fault?hang=1" >/dev/null
auto_recover "业务服务假死" docker exec "$CT1" curl -sf "http://127.0.0.1:8080/_fault?hang=0"
watch_hint "mock-web-01 的探活（业务HTTP）与告警中心"
