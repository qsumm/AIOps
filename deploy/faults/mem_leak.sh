#!/usr/bin/env bash
# 故障 5：内存泄漏 —— 在 mock-web-01 内以缓慢增长方式持续吃内存（模拟泄漏曲线），N 秒后释放
# 预期：mem.percent 单调爬升 → 触发内存类规则；3σ 规则也能识别"缓慢漂移"型异常
# ⚠️ 安全：容器看到的是宿主/VM 的内存（本机实测 11.7GB），--vm-bytes 是**比例或绝对量**。
# 默认申请 2G（有界，不会把宿主内存吃满触发大量 swap）；要更猛用 BYTES=6G 显式指定，
# 不建议用 85%（在 11.7G 的 VM 上 = 10G，会让你的 Mac 大量换页）。180s 后自动释放。
source "$(dirname "$0")/_lib.sh"
need_docker

WORKERS="${WORKERS:-1}"
# 同上：容器内看到的是整机内存，用 85% 才可能触及阈值（按需调 BYTES）
docker exec -d "$CT1" stress-ng --vm "$WORKERS" --vm-bytes "${BYTES:-2G}" --vm-hang 0 --timeout "${DURATION}s"
auto_recover "内存泄漏（${WORKERS} worker × ${BYTES:-2G}）" docker exec "$CT1" pkill -f stress-ng
watch_hint "mock-web-01 的 mem.percent 曲线"
