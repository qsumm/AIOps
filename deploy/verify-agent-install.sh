#!/usr/bin/env bash
# verify-agent-install.sh —— 在**目标机**上自测 Agent 安装链路（不需要平台、不需要外网）
#
# 用途：买/部署前在目标机上一条命令验收 install.sh：解释器选择 → 取安装件 → 依赖安装
#      → 服务单元（systemd/launchd）→ 上报注册与指标 → 卸载清理。全程自包含。
#
# 用法（在目标机上）：
#   bash verify-agent-install.sh                  # 用本机自带的 stub 平台
#   bash verify-agent-install.sh --platform http://<平台>   # 换成真平台一起验
set -uo pipefail
PLATFORM=""
PORT="${PORT:-18999}"
WORK="$(mktemp -d)"
STUB_LOG="$WORK/stub.log"
PASS=0; FAIL=0
ok()   { echo "  ✅ $*"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
step() { echo; echo "── $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="${2:?}"; shift 2 ;;
    --port)     PORT="${2:?}"; shift 2 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

cleanup() {
  echo; step "清理"
  bash "$WORK/install.sh" --uninstall >/dev/null 2>&1 || true
  [[ -n "${STUB_PID:-}" ]] && kill "$STUB_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

step "0. 环境体检"
OS="$(uname -s)"; echo "    系统: $OS $(uname -r) / $(uname -m)"
command -v python3 >/dev/null 2>&1 && echo "    系统 python3: $(python3 -V 2>&1)"
for c in python3.13 python3.12 python3.11 python3.10 python3.9; do command -v $c >/dev/null 2>&1 && echo "    $c: $($c -V 2>&1)"; done
if [[ "$OS" == "Linux" ]]; then ps -p 1 -o comm= | grep -q systemd && echo "    init: systemd" || echo "    ⚠ init 不是 systemd（容器里常见），服务单元那一步会失败"; fi

# 1) 取安装件
step "1. 获取安装件"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_AGENT="$(cd "$SELF_DIR/../agent" 2>/dev/null && pwd || echo "")"
if [[ -n "$PLATFORM" ]]; then
  curl -fsSL "${PLATFORM%/}/install.sh" -o "$WORK/install.sh" && curl -fsSL "${PLATFORM%/}/agent_main.py" -o "$WORK/agent_main.py" \
    && ok "从平台 ${PLATFORM} 下载 install.sh + agent_main.py" || bad "从平台下载安装件失败"
elif [[ -n "$REPO_AGENT" && -f "$REPO_AGENT/install.sh" ]]; then
  cp "$REPO_AGENT/install.sh" "$REPO_AGENT/agent_main.py" "$WORK/"
  ok "使用仓库内安装件（$REPO_AGENT）"
else
  bad "既没有 --platform 也不在仓库里，无法取得安装件"; exit 1
fi
bash -n "$WORK/install.sh" && ok "install.sh 语法检查通过" || bad "install.sh 语法错误"

# 2) 启一个 stub 平台（无需外网）
if [[ -z "$PLATFORM" ]]; then
  step "2. 启动 stub 平台（:${PORT}，模拟 register/metrics 接口）"
  cat > "$WORK/stub.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HITS = []
class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        raw = json.dumps(body).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        HITS.append((self.path, body))
        print("RECV", self.path, json.dumps(body)[:160], flush=True)
        if self.path.endswith("/agents/register"):
            reg = body.get("fingerprint", "")
            import hashlib
            self._send(200, {"code": 0, "msg": "ok", "data": {"ident": hashlib.sha256(reg.encode()).hexdigest()[:32], "token": "stub-token"}})
        else:
            self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
  command -v python3 >/dev/null || { bad "没有 python3，无法跑 stub"; exit 1; }
  python3 "$WORK/stub.py" "$PORT" > "$STUB_LOG" 2>&1 &
  STUB_PID=$!
  sleep 2
  curl -s -m 3 -o /dev/null -X POST "http://127.0.0.1:$PORT/api/ops/agents/register" -d '{}' && ok "stub 平台已就绪" || bad "stub 平台未就绪"
  PLATFORM="http://127.0.0.1:$PORT"
else
  step "2. 使用指定平台 $PLATFORM（跳过 stub）"
fi

# 3) 执行一键安装
step "3. 执行一键安装"
TOKEN=""
if [[ "$PLATFORM" == *18999* ]]; then TOKEN="stub"; fi
if bash "$WORK/install.sh" --server "$PLATFORM" --token "$TOKEN" --interval 5 > "$WORK/install.log" 2>&1; then
  ok "install.sh 退出码 0"
else
  bad "install.sh 失败，日志尾部："; tail -6 "$WORK/install.log" | sed 's/^/      /'
fi
grep -E "使用解释器" "$WORK/install.log" | sed 's/^/      /'

# 4) 服务单元与进程
step "4. 服务自启与进程"
if [[ "$OS" == "Darwin" ]]; then
  launchctl list 2>/dev/null | grep -q com.aiops.agent && ok "launchd 已加载 com.aiops.agent" || bad "launchd 未加载"
else
  systemctl is-enabled opsagent >/dev/null 2>&1 && ok "systemd 单元已启用（开机自启）" || bad "systemd 单元未启用"
  systemctl is-active opsagent >/dev/null 2>&1 && ok "systemd 单元 active" || bad "systemd 单元未运行"
fi
sleep 8
pgrep -f agent_main.py >/dev/null && ok "agent 进程在跑（$(pgrep -f agent_main.py | head -1)）" || bad "未见 agent 进程"

# 5) 上报验证
step "5. 上报（注册 + 指标）"
if [[ -z "$PLATFORM" || "$PLATFORM" == *18999* ]]; then
  sleep 12
  grep -q "/agents/register" "$STUB_LOG" && ok "stub 收到注册请求" || bad "stub 未收到注册请求"
  grep -q "/agents/metrics" "$STUB_LOG" && ok "stub 收到指标上报" || bad "stub 未收到指标上报"
  MET="$(grep -m1 "/agents/metrics" "$STUB_LOG" || true)"
  echo "      首条上报预览：$(echo "$MET" | cut -c1-140)"
else
  echo "      请在平台『主机管理』确认本机已出现且状态 up"
fi

# 6) 卸载
step "6. 卸载验证"
bash "$WORK/install.sh" --uninstall >/dev/null 2>&1 || true
if [[ "$OS" == "Darwin" ]]; then
  launchctl list 2>/dev/null | grep -q com.aiops.agent && bad "launchd 仍有残留" || ok "launchd 已清理"
else
  systemctl is-enabled opsagent >/dev/null 2>&1 && bad "systemd 仍有残留" || ok "systemd 已清理"
fi
pgrep -f agent_main.py >/dev/null && bad "仍有 agent 进程残留" || ok "无进程残留"

echo; echo "══════ 结果：通过 $PASS 项 / 失败 $FAIL 项 ══════"
[[ "$FAIL" == "0" ]] && echo "✅ Agent 安装链路在本机验证通过" || { echo "❌ 存在失败项，请把上面输出发回"; exit 1; }
