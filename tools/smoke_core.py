#!/usr/bin/env python3
"""核心路径冒烟：逐条打关键 GET 接口，确认 200。"""
import json, sys, urllib.error, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848"
# 真实 GET 清单（不含 SSE 流式 / 需路径参数的详情）；路径参数类在用例库 TC-* 里覆盖
PATHS = [
    "/api/health", "/api/auth/me", "/api/auth/perms", "/api/audit-logs?page=1&page_size=5",
    "/api/license", "/api/license/state", "/api/llm-configs", "/api/llm-configs/usage",
    "/api/ops/dashboard/overview",
    "/api/ops/hosts?page=1&page_size=5",
    "/api/ops/alerts?page=1&page_size=5",
    "/api/ops/alert-rules?page=1&page_size=5",
    "/api/ops/alert-mutes?page=1&page_size=5",
    "/api/ops/incidents?page=1&page_size=5",
    "/api/ops/inspections?page=1&page_size=5",
    "/api/ops/runbooks?page=1&page_size=5",
    "/api/ops/skills",
    "/api/ops/case-cards?page=1&page_size=5",
    "/api/ops/case-cards/stats",
    "/api/ops/action-allowlist", "/api/ops/action-audit?page=1&page_size=5",
    "/api/ops/notification-records?page=1&page_size=5",
    "/api/ops/todo-counts",
    "/api/ops/notify-channels", "/api/ops/prom-datasources",
    "/api/ops/chat/sessions?page=1&page_size=5",
    "/api/ops/dev/engine-state",
    "/api/sse-ticket",
    # P1-12：安装件哈希端点（非 JSON，但应稳定 200）
    "/agent_main.py.sha256",
]


def main():
    r = urllib.request.Request(BASE + "/api/auth/login",
                               data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                               method="POST")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=15) as resp:
        tok = json.loads(resp.read())["data"]["access_token"]
    ok = 0
    for p in PATHS:
        rr = urllib.request.Request(BASE + p)
        rr.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(rr, timeout=30) as resp:
                st, body = resp.status, resp.read().decode()
            mark = "✅" if st == 200 and '"code": 0' in body.replace('"code":0', '"code": 0') or st == 200 else "❌"
            ok += st == 200
            print(f"  {mark} {st} {p}")
        except urllib.error.HTTPError as e:
            print(f"  ❌ {e.code} {p}  {e.read()[:80].decode('utf-8','replace')}")
        except Exception as e:
            print(f"  ❌ --- {p}  {type(e).__name__}")
    print(f"\n冒烟：{ok}/{len(PATHS)} 返回 200")
    return 0 if ok == len(PATHS) else 1


if __name__ == "__main__":
    sys.exit(main())
