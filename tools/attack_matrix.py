#!/usr/bin/env python3
"""全量攻击矩阵：全部 API 路由 × 5 种攻击向量，穷举请求，只为找出服务端 5xx。

用法：python3 tools/attack_matrix.py [base_url] [--allow-remote]
退出码：0 = 服务端 5xx 为 0；1 = 存在 5xx（必须修）；2 = 目标非本机且未加 --allow-remote

**本工具会发真实的写请求**（V3/V5 用 admin 令牌打 POST/PATCH/DELETE），是会改数据的：
实测把内置技能 `core-investigation` 停用了（连带 d5 回归变红）；对演示站跑更糟——
会 ack 告警、关闭事件、停用规则。因此：
  · 默认**只允许对本机（127.0.0.1 / localhost）**运行，要打远端必须显式加 --allow-remote；
  · 写方法的路径变异只用"确定不存在"的 id（PATH_VARIANTS_WRITE），不去碰真实行；
  · 跑完按需清理（purge 逻辑 / 手工恢复被改的开关）。

向量定义
  V1 未认证：不带 Authorization 直接访问（应 401/403，不得 200/500）
  V2 demo 写：用 demo 角色（只读）尝试写操作（应 403，不得改数据）
  V3 路径参数变异：把路径里的整数 id 换成边界值
     [1, 0, -1, 999999999999, 1e999, 'abc', "' OR 1=1", '../../etc/passwd']
     （写方法换成不含 1 的 PATH_VARIANTS_WRITE——理由见上）
  V4 查询参数变异：page=0/-1/abc、page_size=-5/1e9、非法枚举
  V5 请求体变异：空体 / 类型错乱 / 超长字符串 / 嵌套炸弹

分类规则（血泪教训，两次踩坑）
  - 客户端超时（urllib TimeoutError → -1）**不算服务端 5xx**：
    长任务路由（巡检执行）正常就要 33~48s。必须单独归类并放宽该路径超时。
  - 只有服务端返回 5xx 才是缺陷。
"""
import json, re, sys, time, urllib.error, urllib.parse, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
LONG_TASK = re.compile(r"^/api/ops/inspections/(-?\d+/run|run-once)$")
# SSE 流式路由：连上就不返回，短超时即可（超时属正常，不算缺陷）
# 注意**不能加 `$` 锚点**：V4 会给路径追加 `?page=0`，加了锚点后
# `/api/ops/alerts/stream?page=0` 就不再匹配 → read_body=True → SSE 的 resp.read() 永久阻塞
# （连接常开、服务端持续推 keepalive）。第二版矩阵因此卡死两次。
STREAM = re.compile(r"/(stream|sse-ticket)(\?|$)")
# 外呼型路由（真发 LLM / HTTP 请求）：对**不存在**的 id 做 8 变体只会白等 30s×N。
# 它们"坏 id → 404/403 快速失败"这条路径已由 test_d8_api_matrix 与用例库专项覆盖，
# 故在 V3/V4/V5 里跳过（V1 未认证 / V2 demo 写仍打，越权与崩溃类问题不会漏）。
# 判定依据 = 该路由**是否会对外发请求**（LLM / HTTP）。这类路由在被喂坏参数时，
# 服务端仍会先尝试外呼，在额度耗尽或对端不可达时必等到超时（实测 30s/次），
# 8 变体 × 数条 = 数分钟纯等待，且结果与"能否崩溃"无关。
OUTBOUND = re.compile(
    r"(llm-configs"                          # LLM 配置与连通性测试
    r"|prom-datasources"                     # Prom 数据源（test / scrape-now 会真抓）
    r"|prom/query"                           # Prom 代理查询 → 外呼 Prometheus
    r"|case-cards/recall"                    # 向量检索 → 外呼 LLM 取 embedding
    r"|runbooks/(generate/|\d+/(execute|\d+/confirm))"  # 生成/执行/确认（有护栏，但会真跑）
    r"|skills/\d+/(check|toggle)"
    r"|incidents/\d+/diagnose"
    r"|notify-channels/\d+/test"
    r"|chat/stream)")

PATH_VARIANTS = ["1", "0", "-1", "999999999999", "1e999", "abc", "' OR 1=1", "../../etc/passwd"]
# 写方法专用：**不含 "1"**。`POST /api/ops/skills/1/toggle` 这类路由的副作用是真实生效的
# （实测：V3 打了一次就把内置技能 core-investigation 停用，d5 回归随即变红）。
# 本向量的目的是"坏 id 不崩"，用确定不存在的 id 完全够用，没必要拿真实行当靶子。
PATH_VARIANTS_WRITE = ["0", "-1", "999999999999", "1e999", "abc", "' OR 1=1", "../../etc/passwd"]
QUERY_VARIANTS = ["page=0", "page=-1", "page=abc", "page_size=-5", "page_size=1000000000",
                  "page=1&page_size=0", "limit=-1", "offset=-1"]
BODY_VARIANTS = [{}, {"x": None}, {"x": "A" * 5000}, {"x": {"a": {"b": {"c": 1}}}},
                 {"id": "abc"}, {"id": -1}, {"page": -1}, [], "not-a-dict", 12345]


def req(method, path, body=None, token=None, timeout=30, read_body=True):
    """read_body=False 用于 SSE：只取状态码/响应头，**绝不 read()**。

    踩坑记录：SSE 路由 `urlopen` 拿到 200 后 `resp.read()` 永不返回（连接常开、
    服务端持续推 keepalive）——`timeout` 只作用于 socket 建连与首次读，
    对"一直有数据但永不结束"的流无效。第一版矩阵因此卡在 /alerts/stream 9 分钟。
    """
    data = json.dumps(body).encode() if body is not None and not isinstance(body, str) else (
        body.encode() if isinstance(body, str) else None)
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        if not read_body:
            st = resp.status
            resp.close()
            return st, "(stream: 仅取状态)"
        with resp:
            return resp.status, resp.read()[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return -1, type(e).__name__          # 客户端侧：超时/DNS/连接被拒


def login(username="admin", password="admin123"):
    r = urllib.request.Request(BASE + "/api/auth/login",
                               data=json.dumps({"username": username, "password": password}).encode(),
                               method="POST")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read())["data"]["access_token"]


def demo_token():
    """demo 只读令牌（演示用户；拿不到就返回 None → V2 退化为未认证向量）。"""
    try:
        r = urllib.request.Request(BASE + "/api/auth/demo", data=b"{}", method="POST")
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=15) as resp:
            return json.loads(resp.read())["data"]["access_token"]
    except Exception:
        return None


def _is_loopback(url: str) -> bool:
    """目标是否本机（本工具会写数据，默认只允许打本机）。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal")


def routes(token):
    r = urllib.request.Request(BASE + "/openapi.json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=15) as resp:
        spec = json.loads(resp.read())
    out = []
    for path, ops in spec["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in ops:
            if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                out.append((method.upper(), path))
    return out


def main():
    if not _is_loopback(BASE) and "--allow-remote" not in sys.argv:
        print(f"拒绝执行：目标 {BASE} 不是本机。\n"
              "本工具会对真实数据发写请求（V3/V5 用 admin 令牌打 POST/PATCH/DELETE）——\n"
              "实测把内置技能停用过、也会 ack 告警/关闭事件/停用规则。\n"
              "确认该环境的数据可以被污染后，加 --allow-remote 重跑。")
        return 2
    tok = login()
    demo_tok = demo_token()
    rs = routes(tok)
    print(f"路由数：{len(rs)}  BASE={BASE}  demo令牌={'有' if demo_tok else '无'}\n", flush=True)
    stats, findings, slow = {}, [], []
    total = 0

    def hit(method, path, body=None, token=None, label=""):
        nonlocal total
        total += 1
        if total % 100 == 0:
            print(f"  …已发 {total} 请求（当前 {label} {method} {path}）", flush=True)
        if STREAM.search(path):
            tmo = 6          # 流式：只要握手成功即可
        elif LONG_TASK.match(path) or OUTBOUND.search(path):
            tmo = 300        # 长任务/外呼型：必须 > 25s DB 兜底，否则误杀会被当成客户端超时而漏掉
        else:
            tmo = 30
        t0 = time.time()
        st, msg = req(method, path, body, token, tmo, read_body=not STREAM.search(path))
        el = time.time() - t0
        key = str(st)
        stats[key] = stats.get(key, 0) + 1
        if st == -1:
            slow.append((round(el, 1), "客户端超时", method, path))
        elif el >= 5:
            slow.append((round(el, 1), f"http={st}", method, path))
        if st >= 500:
            findings.append((label, method, path, st, msg[:120]))
        return st

        return st

    # V1 未认证：全部路由不带令牌（应 401/403，不得 200/5xx）
    for method, path in rs:
        hit(method, path, None, None, "V1未认证")

    # V2 demo 写：只读角色打写接口（应 403，不得改数据）
    for method, path in rs:
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            hit(method, path, None, demo_tok, "V2demo写")

    # V3：路径参数变异（只针对带 {xx} 占位的路由；写方法只用"确定不存在"的 id）
    for method, path in rs:
        if "{" not in path:
            continue
        variants = PATH_VARIANTS_WRITE if method in WRITE_METHODS else PATH_VARIANTS
        for v in variants:
            # 必须 quote：`' OR 1=1` 里的空格/引号会让 urllib 抛 InvalidURL，
            # 请求**根本没发出**却被我上一版当成"客户端超时"（实测 0.0s 暴露了这一点），
            # 整行 SQL 注入变体等于白跑。编码后才是真的把注入串送进路由。
            hit(method, re.sub(r"\{[^}]+\}",
                               lambda _: urllib.parse.quote(v, safe=""), path),
                None, tok, f"V3={v}")

    # V4：查询参数变异
    for method, path in rs:
        if method != "GET":
            continue
        for q in QUERY_VARIANTS:
            sep = "&" if "?" in path else "?"
            hit(method, f"{path}{sep}{q}", None, tok, f"V4={q}")

    # V5：请求体变异
    for method, path in rs:
        if method not in ("POST", "PUT", "PATCH"):
            continue
        if LONG_TASK.match(path) or STREAM.search(path):
            continue  # 长任务/流式不吃 body 变异（长任务另有 V3 覆盖）
        for b in BODY_VARIANTS:
            hit(method, path, b, tok, f"V5={str(b)[:18]}")

    print(f"总请求：{total}（无跳过；外呼型/长任务用 300s 超时以便暴露 25s 误杀）")
    print("状态分布：" + json.dumps(dict(sorted(stats.items(), key=lambda x: -x[1])), ensure_ascii=False))
    client = stats.get("-1", 0)
    srv5xx = sum(v for k, v in stats.items() if k.isdigit() and int(k) >= 500)
    print(f"\n客户端侧超时/连接失败(-1)：{client}（≠服务端缺陷；长任务路由正常 33~48s）")
    if slow:
        print(f"\n=== 慢请求 / 客户端超时归属（{len(slow)} 条，按耗时降序前 30）===")
        for el, st, m, p_ in sorted(slow, reverse=True)[:30]:
            print(f"  {el:6.1f}s  {st:<10} {m} {p_}")
    print(f"服务端 5xx：{srv5xx}")
    if findings:
        print("\n=== 5xx 明细 ===")
        for label, m, p, st, msg in findings:
            print(f"  ❌ [{label}] {m} {p} → {st}  {msg}")
        return 1
    print("\n✅ 服务端 5xx = 0（无崩溃类缺陷）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
