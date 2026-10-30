# 故障注入脚本集（FR-G1）

一条命令触发一种故障，**到时自动恢复**（默认 180 秒）。用于演示「告警 → 收敛 → 诊断 → 变更单」全链路。

## 前置

```bash
docker compose -f deploy/docker-compose.yaml up -d --build
# 建规则（若是新环境）：
docker compose -f deploy/docker-compose.yaml exec platform python scripts/seed_demo.py
```

> 📌 **给客户演示时优先用 `process_hang.sh` / `probe_fail.sh`**：这两类只操作容器内的进程，
> **不占用宿主机任何资源**，可以现场放心跑。各场景的本机可行性判定与真机补验清单见
> 《docs/04-交付与运营/可验证场景清单与实测证据.md》（含「服务假死」全链路实测证据）。

## 五种故障

| 脚本 | 模拟现象 | 观察点 | 预期链路 |
|---|---|---|---|
| `cpu_spike.sh` | mock-web-01 CPU 飙高（2 核满载） | `cpu.percent` 冲高 | 触发 CPU 规则 → S2 事件 → 一键诊断 |
| `mem_leak.sh` | mock-web-01 内存缓慢爬升 | `mem.percent` 单调上升 | 阈值或 3σ 规则命中（缓慢漂移型异常） |
| `disk_full.sh` | mock-db-01 磁盘被大文件占满 | `disk.percent` 越过阈值 | 磁盘规则命中（新环境需先加一条 disk 规则） |
| `process_hang.sh` | 业务服务**假死**（进程在、/health 不返回） | 探活「业务HTTP」down | 证明"running ≠ healthy"；探活规则命中 S1 |
| `probe_fail.sh` | 业务服务进程被杀 | 探活 + 进程探活均失败 | S1 探活告警；处置建议"重启服务"需审批 |

## 用法

```bash
bash deploy/faults/cpu_spike.sh                     # 默认 180s 后自动恢复
DURATION=600 CORES=4 bash deploy/faults/cpu_spike.sh # 自定义时长与核数
PLATFORM=http://<平台地址> bash deploy/faults/probe_fail.sh
```

## 演示脚本（建议顺序，约 6 分钟）

1. `cpu_spike.sh` → 告警中心看到 S2 事件；点「一键诊断」看实时工具瀑布；报告出来后在问题看板点「生成变更单」。
2. `process_hang.sh` → 探活 S1 事件；说明"探测失败但进程还在"的经典误判场景。
3. 在「处置与变更」里执行只读步骤（放行）→ 执行"重启服务"（被拒：需开启白名单执行 + 审批）。
4. 等 `DURATION` 到期（或手动恢复）→ 看「告警中心」恢复通知与 `alert_his_events` 留痕。
5. 关闭事件 → 自动沉淀案例卡 → `case_cards` 里出现向量；下次同类故障诊断时会被 `search_cases` 召回。

## 注意

- 脚本需要容器内存在 `stress-ng`（`Dockerfile.agent` 已安装）；`disk_full.sh` 需要容器有 `fallocate` 或 `dd`（busybox 提供）。
- 故障只作用于**演示容器**，不会触碰宿主机。
- 自动恢复是"尽力而为"：若容器被重启，残留进程会随容器退出消失。

## ⚠️ 安全须知（先读这一节）

这些脚本**在容器里制造负载**，而容器看到的资源是**宿主/VM 的**（本机实测：磁盘 911G、CPU 大核数、
内存 11.7G）。所以它们会真实占用你机器的资源，虽然都是临时的。设计上的三重保护：

1. **安全上限**：`disk_full.sh` 默认最多写 **512MB**（`MAX_SIZE` 可调）；`mem_leak.sh` 默认申请
   **2G**（`BYTES` 可调）。不会为了演示把你的盘写满、把内存吃光。
2. **自动恢复**：每个脚本注入后 `DURATION`（默认 180）秒自动还原（删文件 / 杀 stress-ng）。
3. **不写无效数据**：`disk_full.sh` 会先算"跨过阈值需要多少"；如果超过安全上限，
   **一个字节都不写**，直接告诉你本环境做不到并给替代方案 —— 写下去只会占你真实磁盘，却产不出效果。

**请在闲置的机器/演示机上跑，不要在生产机上跑。** 万一哪里出了问题，手工回收就一句话：

```bash
docker exec aiops-agent-2 rm -f /tmp/fault-disk.img     # 立即释放磁盘
docker exec aiops-agent-1 pkill -f stress-ng            # 立即停掉 CPU/内存压测
```

### 本机（Docker Desktop）能演示哪些故障

| 脚本 | 本机能否真触发 | 说明 |
|---|---|---|
| `probe_fail.sh` | ✅ | 让 mock 服务的 /health 假死 → 探活超时 → 告警 |
| `process_hang.sh` | ✅ | 进程挂住 → 进程探活/状态告警 |
| `mem_leak.sh` | ✅（有界） | 默认申请 2G；内存类规则（3σ）可触发 |
| `cpu_spike.sh` | ⚠️ 够不到 85% | 容器 8 核压满只占宿主 VM 的 ~4.5%（VM 核数远多于 8）→ 需要一台少核真机，或临时降阈值 |
| `disk_full.sh` | ⚠️ 够不到 85% | 容器见宿主大盘（911G/空 805G），跨 85% 需 ~710G → 脚本**不会写**，直接给替代方案 |

**临时降阈值**（演示磁盘/CPU 告警时用，演示完改回）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8848/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')
# 磁盘：把 >85 临时改成 >5（当前水位 7% → 立刻触发）；CPU：把 >85 改成 >3
curl -s -X PUT http://127.0.0.1:8848/api/ops/alert-rules/2 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"expr":">5"}'
# 演示完改回：
curl -s -X PUT http://127.0.0.1:8848/api/ops/alert-rules/2 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"expr":">85"}'
```
