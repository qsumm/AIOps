"""模拟业务服务：供 Agent 做 http 探活（/health）与进程存活检测。

故障注入脚本会向 /_fault 端点发指令让本服务"假死"或"恢复"，用于 probe_fail / process_hang 场景。
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"hanging": False, "requests": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        STATE["requests"] += 1
        if self.path.startswith("/_health"):
            body = json.dumps({"ok": True, "pid": os.getpid(), "hanging": STATE["hanging"]}).encode()
            self.send_response(200)
        elif self.path.startswith("/metrics"):
            # node_exporter 风格的最小 /metrics：供 FR-B3+「exporter 直采」端到端演示
            body = (
                "# HELP node_load1 1m load average.\n# TYPE node_load1 gauge\nnode_load1 1.42\n"
                "# TYPE node_cpu_seconds_total counter\n"
                'node_cpu_seconds_total{cpu="0",mode="idle"} 90000.0\n'
                'node_cpu_seconds_total{cpu="0",mode="user"} 12000.0\n'
                'node_cpu_seconds_total{cpu="0",mode="system"} 3000.0\n'
                "# TYPE node_memory_MemAvailable_bytes gauge\nnode_memory_MemAvailable_bytes 2.5e+09\n"
                "# TYPE node_memory_MemTotal_bytes gauge\nnode_memory_MemTotal_bytes 8.0e+09\n"
                "# TYPE node_filesystem_size_bytes gauge\n"
                'node_filesystem_size_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 1.0e+11\n'
                "# TYPE node_filesystem_avail_bytes gauge\n"
                'node_filesystem_avail_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 1.2e+10\n'
                "# TYPE node_uptime_seconds gauge\nnode_uptime_seconds 4242\n"
            ).encode()
            self.send_response(200)
        elif self.path.startswith("/_fault"):
            # /_fault?hang=1 / ?hang=0
            hang = "hang=1" in self.path
            STATE["hanging"] = hang
            body = json.dumps({"hanging": hang}).encode()
            self.send_response(200)
        elif self.path.startswith("/health"):
            if STATE["hanging"]:          # 假死：连接建立后不返回，触发探活超时
                time.sleep(120)
            body = b'{"status":"ok"}'
            self.send_response(200)
        else:
            body = b"ok"
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.getenv("MOCK_PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"mock service on :{port}", flush=True)
    while True:
        time.sleep(3600)
