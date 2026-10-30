#!/usr/bin/env python3
"""从运行中的服务生成**可交付的接口文档**（Markdown + 离线可打开的 HTML）。

为什么要有这个脚本（而不是手写接口文档）：
  手写接口文档一定会过期——本轮就新增了 `GET /api/ops/install-token`、
  `POST /api/auth/password` 两个端点。所以接口文档的**唯一权威来源是 app.openapi()**，
  文档全部机械生成；权限列来自 `backend/tests/test_d8_api_matrix.py` 的 ROUTES 表
  （那张表被测试守卫锁死，新路由不登记就测试失败），因此权限口径也有一致性保证。

用法：
    python3 tools/gen_api_doc.py                       # 连本机 8848 取 spec
    python3 tools/gen_api_doc.py --spec /tmp/o.json    # 用离线 spec 文件
产出：
    docs/01-需求与设计/10-接口文档（OpenAPI 自动生成）.md
    docs/04-交付与运营/api-reference.html              # 单文件、离线可开
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
MD_OUT = ROOT / "docs/01-需求与设计/10-接口文档（OpenAPI 自动生成）.md"
HTML_OUT = ROOT / "docs/04-交付与运营/api-reference.html"
MATRIX = ROOT / "backend/tests/test_d8_api_matrix.py"
PERMS_JSON = ROOT / "tools/route_perms.json"
METHODS = ("get", "post", "put", "patch", "delete")


def load_spec(path: str | None) -> dict:
    if path:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    with urllib.request.urlopen("http://127.0.0.1:8848/openapi.json", timeout=20) as r:
        return json.loads(r.read())


def load_permissions() -> dict[tuple[str, str], dict]:
    """从 ROUTES 表的 `expect` 反推每个端点**谁能访问**。"""
    tree = ast.parse(MATRIX.read_text(encoding="utf-8"))
    # ROUTES 表里用了模块级常量名（OK / DENY 等），literal_eval 不认名字，
    # 故先把同文件里的常量赋值收集起来，作为求值环境。
    consts: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            try:
                if isinstance(tgt, ast.Name):
                    consts[tgt.id] = ast.literal_eval(node.value)
                elif isinstance(tgt, ast.Tuple):        # 形如 OK, DENY = {...}, {...}
                    vals = ast.literal_eval(node.value)
                    for name_node, val in zip(tgt.elts, vals):
                        if isinstance(name_node, ast.Name):
                            consts[name_node.id] = val
            except (ValueError, SyntaxError):
                continue

    def ev(node):
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if isinstance(node, ast.JoinedStr):      # f"/api/.../{FAKE_ID}" 这类拼装路径
            out = ""
            for part in node.values:
                if isinstance(part, ast.Constant):
                    out += str(part.value)
                elif isinstance(part, ast.FormattedValue):
                    out += str(ev(part.value))
            return out
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            return {ev(x) for x in node.elts} if isinstance(node, ast.Set) else [ev(x) for x in node.elts]
        if isinstance(node, ast.Dict):
            return {ev(k): ev(v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):   # FAKE_ID + 1
            l, r = ev(node.left), ev(node.right)
            return (l + r) if isinstance(l, (int, str)) and isinstance(r, type(l)) else f"{l}+{r}"
        try:
            return ast.literal_eval(node)
        except (ValueError, SyntaxError):
            # 请求体里的表达式不影响"权限"这一列，取不到就给 None，不让脚本崩
            return None

    rows = None
    for node in tree.body:
        tgt = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) and node.targets else None)
        if tgt is not None and getattr(tgt, "id", "") == "ROUTES":
            value = node.value
            rows = []
            for item in value.elts:      # 每个元素是一个 tuple
                rows.append(tuple(ev(x) for x in item.elts))
            break
    if not rows:
        return {}
    return rows if rows else []


def _perm_from_row(expect: dict, note: str) -> dict:
    def allows(codes) -> bool:
        return any(200 <= int(c) < 300 for c in (codes or []))

    who = []
    if allows(expect.get("admin")) or "admin" not in expect:
        who.append("admin")
    if allows(expect.get("member")):
        who.append("member")
    if allows(expect.get("demo")):
        who.append("demo")
    return {
        "note": note,
        "allow_anon": allows(expect.get("anon")),
        "allow_demo": allows(expect.get("demo")),
        "allow_member": allows(expect.get("member")),
        "allow_admin": allows(expect.get("admin")),
        "expect": {k: sorted(map(str, v)) for k, v in expect.items()},
        "who": who or (["admin"] if allows(expect.get("admin")) else []),
    }


def load_code_perms() -> dict[tuple[str, str], dict]:
    """读代码侧权限（tools/dump_route_perms.py 的产物）——**权限的唯一权威**。"""
    if not PERMS_JSON.exists():
        return {}
    data = json.loads(PERMS_JSON.read_text(encoding="utf-8"))
    out = {}
    for e in data.get("endpoints", []):
        roles = e["roles"]
        if e["path"].startswith("/api/ops/agents/"):
            roles = ["Agent 令牌（X-Ident + X-Token 或一次性 install_token）"]
        out[(e["method"], e["path"])] = {"cap": e["cap"], "roles": roles,
                                        "perm_matrix": data.get("perm_matrix", {})}
    return out


def build_perm_index(rows: list, spec: dict) -> dict:
    """把 ROUTES 表的**具体值路径**（/api/ops/incidents/999999/ack）匹配到
    openapi 的**模板路径**（/api/ops/incidents/{iid}/ack）。

    做法：以 openapi 模板为准生成正则，去 ROUTES 里找对得上的那一行。
    """
    index: dict[tuple[str, str], dict] = {}
    for tmpl_path, ops in spec["paths"].items():
        if not tmpl_path.startswith("/api"):
            continue
        regex = re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(tmpl_path)
                                          .replace(r"\{", "{").replace(r"\}", "}")) + "$")
        for m in METHODS:
            if m not in ops:
                continue
            for method, path, _body, expect, note, _mode in rows:
                if method.upper() != m.upper():
                    continue
                if regex.match(path):
                    index[(m.upper(), tmpl_path)] = _perm_from_row(expect, note)
                    break
    return index


def deref(spec: dict, schema: dict | None, depth: int = 0) -> str:
    """把 JSON Schema 渲染成紧凑可读的一行/多行文本（递归展开 $ref，限深防环）。"""
    if not schema or depth > 4:
        return ""
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        target = (spec.get("components", {}).get("schemas", {}) or {}).get(name)
        if target is None:
            return name
        return f"{name}" + ("  " + deref(spec, target, depth + 1) if depth else "")
    t = schema.get("type")
    if "anyOf" in schema:
        return " | ".join(x.get("type", "any") for x in schema["anyOf"])
    if t == "array":
        return f"[{deref(spec, schema.get('items'), depth + 1)}]"
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            return "object"
        parts = []
        for k, v in props.items():
            req = "!" if k in (schema.get("required") or []) else ""
            vt = v.get("type") or ("$" + v["$ref"].split("/")[-1] if "$ref" in v else "any")
            if "$ref" in v:
                name = v["$ref"].split("/")[-1]
                sub = (spec.get("components", {}).get("schemas", {}) or {}).get(name) or {}
                subprops = sub.get("properties") or {}
                vt = "{" + ", ".join(f"{a}:{b.get('type','any')}" for a, b in subprops.items()) + "}"
            enum = v.get("enum")
            if enum:
                vt += f" 枚举={enum}"
            default = v.get("default", None)
            if default is not None and not isinstance(default, (dict, list)):
                vt += f" 默认={default!r}"
            parts.append(f"{k}{req}:{vt}")
        return "{" + ", ".join(parts) + "}"
    if t == "string" and schema.get("enum"):
        return f"string 枚举={schema['enum']}"
    extra = []
    if schema.get("minimum") is not None:
        extra.append(f">={schema['minimum']}")
    if schema.get("maximum") is not None:
        extra.append(f"<={schema['maximum']}")
    if schema.get("maxLength") is not None:
        extra.append(f"len<={schema['maxLength']}")
    return (t or "any") + (" " + " ".join(extra) if extra else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=None, help="离线 openapi.json 路径（默认连 127.0.0.1:8848）")
    args = ap.parse_args()

    spec = load_spec(args.spec)
    rows = load_permissions()
    perms_matrix = build_perm_index(rows, spec)      # 测试矩阵推断（用于交叉核对）
    perms_code = load_code_perms()                   # 代码依赖声明（权威）

    endpoints = []
    for path, ops in sorted(spec["paths"].items()):
        if not path.startswith("/api"):
            continue
        for m in METHODS:
            if m not in ops:
                continue
            op = ops[m]
            key = (m.upper(), path)
            endpoints.append({
                "method": m.upper(),
                "path": path,
                "tags": op.get("tags") or ["(未分组)"],
                "summary": op.get("summary") or "",
                "params": op.get("parameters") or [],
                "body": op.get("requestBody") or None,
                "deprecated": bool(op.get("deprecated")),
                "perm": perms_code.get(key, {}),
                "perm_matrix": perms_matrix.get(key, {}),
                "registered": key in perms_matrix,
            })

    # ---------- Markdown ----------
    md: list[str] = []
    md.append("# 接口文档（OpenAPI 自动生成）")
    md.append("")
    md.append("| 项目 | 内容 |")
    md.append("|---|---|")
    md.append(f"| 来源 | 运行时 `GET /openapi.json`（OpenAPI {spec.get('openapi')}）——**唯一权威**，本文档全部机械生成 |")
    md.append(f"| 服务标题 / 版本 | {spec['info'].get('title')} / {spec['info'].get('version')} |")
    md.append(f"| 端点数 | **{len(endpoints)}** 条（{len({e['path'] for e in endpoints})} 个路径） |")
    md.append("| 生成脚本 | `tools/gen_api_doc.py`（改接口后重跑即同步，杜绝文档过期） |")
    md.append("| 在线交互 | 服务自带 Swagger UI：`http://<平台地址>:8848/docs`；ReDoc：`/redoc` |")
    md.append("| 离线可读 | `docs/04-交付与运营/api-reference.html`（单文件，双击即开，无需网络） |")
    md.append("")
    md.append("## 1. 通用约定")
    md.append("")
    md.append("### 1.1 认证")
    md.append("")
    md.append("- 除 `/api/health`、`/api/auth/login`、`/api/auth/demo`、`/api/ops/agents/*`（Agent 用 "
              "`X-Ident` + `X-Token` 头）外，**所有端点都需要登录**。")
    md.append("- 登录：`POST /api/auth/login` → `data.access_token`；后续请求带 `Authorization: Bearer <token>`。")
    md.append("- **SSE 流式端点例外**：浏览器 `EventSource` 无法自定义请求头，故对流式端点使用"
              "**一次性 ticket**：先 `GET /api/sse-ticket` 取 `ticket`（60 秒有效、消费一次），"
              "再以 `?ticket=...` 连接。涉及 `/api/ops/alerts/stream`、"
              "`/api/ops/incidents/{iid}/diagnose/stream`。")
    md.append("")
    md.append("### 1.2 统一响应信封")
    md.append("")
    md.append("所有响应（含错误）都是同一形状，前端只认这个形状：")
    md.append("")
    md.append("```json")
    md.append('{"code": 0, "msg": "ok", "data": {}}')
    md.append("```")
    md.append("")
    md.append("| code | HTTP | 含义 |")
    md.append("|---|---|---|")
    md.append("| 0 | 200 | 成功 |")
    md.append("| 400 | 400 | 参数/业务校验失败（`msg` 是给人看的中文原因） |")
    md.append("| 401 | 401 | 未认证或凭证过期 |")
    md.append("| 403 | 403 | 已认证但无权限（能力集不足） |")
    md.append("| 404 | 404 | 资源不存在或已被删除 |")
    md.append("| 409 | 409 | 冲突（重名、状态不允许、并发执行中） |")
    md.append("| 422 | 422 | 请求参数不合法（字段名与原因在 `msg`） |")
    md.append("| 429 | 429 | **LLM 配额用尽**（演示站/平台每日 token 上限；报错含 used/limit 与 UTC 重置时间，见 §1.6） |")
    md.append("| 500 | 500 | 未预期异常（已脱敏，细节在服务端日志） |")
    md.append("| 504 | 504 | **长任务**执行超时（见 §1.4） |")
    md.append("")
    md.append("### 1.3 权限模型（能力集）")
    md.append("")
    md.append("服务端按**能力集**判定，前端按钮可见性也来自同一来源（`GET /api/auth/perms`），"
              "杜绝「前端显示了但后端拒绝」：")
    md.append("")
    md.append("| 能力 | 含义 | admin | member | demo |")
    md.append("|---|---|---|---|---|")
    md.append("| `read` | 只读查询 | ✅ | ✅ | ✅（受可见域限制） |")
    md.append("| `alert_ops` | 告警操作：认领/关闭/静默 | ✅ | ✅ | ❌ |")
    md.append("| `diag` | 触发 AI 诊断、规则试运行 | ✅ | ✅ | ❌ |")
    md.append("| `write` | 增删改配置、处置执行、改密 | ✅ | 部分（不含用户/渠道/巡检） | ❌ |")
    md.append("| `chat` | ChatOps 只读问答 | ✅ | ✅ | ✅（`DEMO_CHAT_ENABLED` 控制） |")
    md.append("")
    md.append("**已知的静态分析局限（诚实声明）**：本文档的权限列由 `tools/dump_route_perms.py` "
              "从路由的依赖声明（`Depends(require(...))`）机械导出。以下端点改用"
              "**SSE 一次性 ticket 认证并在函数体内判角色**（因为浏览器 `EventSource` 无法带自定义头），"
              "静态依赖看不到，故文档中显示为「任意登录用户」，**实际权限以测试矩阵为准**：")
    md.append("")
    md.append("| 端点 | 实际口径 |")
    md.append("|---|---|")
    md.append("| `GET /api/ops/alerts/stream` | ticket 换取角色后按 `read` 放行 |")
    md.append("| `GET /api/ops/incidents/{iid}/diagnose/stream` | **admin/member 可用，demo 403**（与 `require(\"diag\")` 同口径） |")
    md.append("")
    md.append("### 1.4 长任务与超时（重要）")
    md.append("")
    md.append("平台有一个 **25 秒 DB 挂起兜底**（防数据库不可达时所有请求一起挂死）。"
              "但以下端点会**同步执行外部动作**（跑巡检、调 LLM 诊断、执行处置步骤、探活外部依赖），"
              "正常耗时就可能超过 25 秒，因此单独登记了各自的兜底上限，超时返回 **504**"
              "（而不是误报 500「数据库不可达」）：")
    md.append("")
    md.append("| 端点模式 | 兜底上限 | 典型实测耗时 |")
    md.append("|---|---|---|")
    md.append("| `POST /api/ops/inspections/{iid}/run`、`/api/ops/inspections/run-once` | 600s | 20~48s（主机数 × 8 项检查 + LLM 润色） |")
    md.append("| `POST /api/ops/incidents/{iid}/diagnose` | 900s | 43~102s（多步 ReAct，每步一次 LLM） |")
    md.append("| `POST /api/ops/runbooks/{rid}/execute`、`.../steps/{i}/confirm` | 300s | 取决于执行动作 |")
    md.append("| `POST /api/ops/runbooks/generate/{incident_id}` | 300s | LLM 生成 |")
    md.append("| `POST /api/ops/skills/{sid}/check` | 300s | 取决于技能内容 |")
    md.append("| `POST /api/ops/alert-rules/{rid}/test` | 120s | 轨 B（Prometheus）会外呼取序列 |")
    md.append("| `POST /api/llm-configs/{cfg_id}/test` | 120s | ~2s |")
    md.append("| `POST /api/ops/prom-datasources/{did}/test`、`/scrape-now` | 120s | 外呼 Prometheus |")
    md.append("| `POST /api/ops/notify-channels/{cid}/test` | 60s | 外呼渠道 |")
    md.append("")
    md.append("> 该清单在代码里是 `backend/app/main.py` 的 `LONG_TASK_ROUTES` 表，"
              "并有**机械守卫测试**保证「凡跑外部动作的接口必须登记」（漏登记会让测试变红）。")
    md.append("")
    md.append("### 1.6 LLM 配额口径（D21 修复后）")
    md.append("")
    md.append("`llm_usage.role` 记录**是谁触发的这次调用**：用户角色（`demo`/`admin`/`member`）"
              "或 `engine`（后台任务：告警 AI 摘要、定时巡检等）。")
    md.append("")
    md.append("| 口径 | 统计范围 | 配置项 |")
    md.append("|---|---|---|")
    md.append("| 演示站 | **只有 demo 自己**的用量 | `DEMO_DAILY_TOKEN_LIMIT`（默认 200000） |")
    md.append("| 平台 | **全平台全部用量**（含 demo 与引擎） | `LLM_DAILY_TOKEN_LIMIT`（默认 0＝不限） |")
    md.append("")
    md.append("demo 角色同时受两者约束，取更严的一个；0 表示不限；重置时间为 **UTC 次日 00:00**。")
    md.append("")
    md.append("> 为什么这样分：演示站限额的目的是「防演示用户刷量」，后台引擎开销与管理员测试不应算进来。"
              "修前口径为「全平台总量」，实测一天 760,070 token 中引擎摘要占 28 万、管理员测试占 42 万，"
              "导致 demo 用户自己几乎没用却被拦住（详见《缺陷台账》D21-1）。")
    md.append("")
    md.append("### 1.5 分页约定")
    md.append("")
    md.append("列表端点统一接受 `page`（≥1）与 `page_size`（1~200），返回：")
    md.append("")
    md.append("```json")
    md.append('{"items": [...], "total": 123, "page": 1, "page_size": 20}')
    md.append("```")
    md.append("")
    md.append("非法取值（`page=0`、`page_size=-5`、`page=abc`）一律 **422**，不会 500，也不会静默返回空页。")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 2. 端点索引")
    md.append("")
    md.append("「登录」列：`否`＝匿名可访问；`是`＝需 Bearer 令牌；`ticket`＝SSE 一次性 ticket。"
              "「权限」列来自 `test_d8_api_matrix.py` 的角色矩阵（被测试锁死）。")
    md.append("")
    md.append("| 方法 | 路径 | 权限 | 登录 | 说明 |")
    md.append("|---|---|---|---|---|")
    for e in endpoints:
        perm = e["perm"]
        roles = perm.get("roles") or []
        code_anon = roles == ["匿名"]
        if not perm:
            who = "⚠️未知"
        elif code_anon:
            who = "匿名"
        elif roles == ["Agent 令牌（X-Ident + X-Token 或一次性 install_token）"]:
            who = "Agent 令牌"
        else:
            who = " ".join(roles) if roles else "任意登录用户"
        if perm.get("cap"):
            who += f"（{perm['cap']}）"
        login = "ticket" if e["path"].endswith("/stream") else ("否" if code_anon else "是")
        note = (perm.get("note") or e["summary"])[:44]
        dep = " ⚠️已弃用" if e["deprecated"] else ""
        md.append(f"| `{e['method']}` | `{e['path']}` | {who} | {login} | {note}{dep} |")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 3. 端点详情")
    md.append("")
    cur_tag = None
    for e in endpoints:
        tag = e["tags"][0]
        if tag != cur_tag:
            cur_tag = tag
            md.append(f"### 3.{len([x for x in md if x.startswith('### 3.')]) + 1} {tag}")
            md.append("")
        md.append(f"#### `{e['method']} {e['path']}`")
        md.append("")
        if e["summary"]:
            md.append(f"{e['summary']}")
            md.append("")
        perm = e["perm"]
        pmat = e["perm_matrix"]
        if e["summary"]:
            pass
        if pmat.get("note"):
            md.append(f"- 说明（用例表）：{pmat['note']}")
        if not perm:
            md.append("- ⚠️ **未从代码解析到权限声明**（既无 `require()` 也无认证依赖），请人工确认")
        else:
            md.append(f"- 权限（**来自代码依赖声明**）：{'匿名可访问' if perm.get('roles') == ['匿名'] else '/'.join(perm.get('roles') or [])}"
                      + (f"；能力位 `{perm['cap']}`" if perm.get("cap") else ""))
        if not e["registered"]:
            md.append("- ⚠️ **该端点未登记进权限矩阵表**，请补 `test_d8_api_matrix.py` 的 ROUTES（守卫测试会失败）")
        else:
            md.append(f"- 测试矩阵期望状态码：{json.dumps(pmat.get('expect', {}), ensure_ascii=False)}")
        if e["deprecated"]:
            md.append("- ⚠️ 已弃用（保留兼容）")
        path_params = [p for p in e["params"] if p.get("in") == "path"]
        query_params = [p for p in e["params"] if p.get("in") == "query"]
        if path_params:
            md.append("- 路径参数：" + "; ".join(
                f"`{p['name']}` {deref(spec, p.get('schema'))}" for p in path_params))
        if query_params:
            md.append("- 查询参数：")
            for p in query_params:
                req = "必填" if p.get("required") else "可选"
                md.append(f"  - `{p['name']}`（{req}，{deref(spec, p.get('schema'))}）")
        if not path_params and not query_params:
            md.append("- 参数：无")
        if e["body"]:
            schema = (((e["body"].get("content") or {}).get("application/json") or {}).get("schema")) or {}
            req = "必填" if e["body"].get("required") else "可选"
            md.append(f"- 请求体（{req}，JSON）：`{deref(spec, schema)}`")
            # 展开 $ref 的字段表
            if "$ref" in schema:
                name = schema["$ref"].split("/")[-1]
                target = (spec.get("components", {}).get("schemas", {}) or {}).get(name) or {}
                props = target.get("properties") or {}
                if props:
                    md.append("")
                    md.append("  | 字段 | 类型 | 必填 | 约束 |")
                    md.append("  |---|---|---|---|")
                    for k, v in props.items():
                        vt = v.get("type") or ("$" + v["$ref"].split("/")[-1] if "$ref" in v else "any")
                        need = "✅" if k in (target.get("required") or []) else ""
                        cons = []
                        for kk, label in (("enum", "枚举"), ("minimum", "最小值"), ("maximum", "最大值"),
                                          ("maxLength", "最大长度"), ("maxItems", "最大项数")):
                            if v.get(kk) is not None:
                                cons.append(f"{label}={v[kk]}")
                        md.append(f"  | `{k}` | {vt} | {need} | {' '.join(cons)} |")
        else:
            md.append("- 请求体：无")
        md.append("")
    md.append("---")
    md.append("")
    md.append("## 4. 自检")
    md.append("")
    unreg = [e for e in endpoints if not e["registered"]]
    md.append(f"- 端点数：**{len(endpoints)}**；其中未登记进权限矩阵表：**{len(unreg)}**")
    for e in unreg:
        md.append(f"  - `{e['method']} {e['path']}`")
    md.append(f"- 代码侧权限条目数：{len(perms_code)}；测试矩阵条目数：{len(perms_matrix)}")
    conflicts, explained, explicit, ambiguous = cross_check(endpoints)
    md.append(f"- **代码 ↔ 测试矩阵 交叉核对**：可判定 {explicit} 项，"
              f"**真不一致 {len(conflicts)} 项**，矩阵未明确 {ambiguous} 项")
    for m in conflicts:
        md.append(f"  - ⚠️ {m}")
    if not conflicts:
        md.append("  - ✅ 无矛盾：代码声明的权限与测试矩阵的明确期望一致")
    md.append(f"- **已核实的预期偏差 {len(explained)} 项**（靠运行时开关/护栏判定，非角色判定）：")
    for m in explained:
        md.append(f"  - {m}")
    md.append("- 判定口径：只有矩阵**明确写了 2xx（允许）或 403（禁止）**的角色才参与比对；"
              "矩阵用负向输入（401/404/422）的行不计入，避免假警报。"
              "这条核对用来抓「代码改了权限但测试没跟上」（或反过来）——"
              "本轮就靠它抓到了路由枚举漏掉同路径的 POST 端点。")
    md.append("- 生成时间由脚本每次运行刷新；本文件**不要手工编辑**。")
    md.append("")

    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text("\n".join(md), encoding="utf-8")

    # ---------- 离线 HTML ----------
    HTML_OUT.write_text(_render_html(spec, endpoints), encoding="utf-8")

    print(f"✓ {MD_OUT.relative_to(ROOT)}  （{len(endpoints)} 端点，未登记 {len(unreg)}，"
          f"权限真不一致 {len(conflicts)}）")
    for m in conflicts:
        print(f"  ⚠️ 真不一致：{m}")
    print(f"✓ {HTML_OUT.relative_to(ROOT)}")
    if unreg:
        print("  ⚠️ 未登记端点：")
        for e in unreg:
            print(f"     {e['method']} {e['path']}")
    print(f"  （交叉核对：可判定 {explicit} 项 / 真不一致 {len(conflicts)} / 已说明偏差 {len(explained)} / 矩阵未明确 {ambiguous}）")

    return 0


# 已核实的**预期偏差**（不是缺陷，而是「靠运行时开关/护栏而非角色判定」的端点）。
# 每条都写明理由；没有理由的一律进「真不一致」，不许静默放过。
EXPECTED_DEVIATIONS: dict[tuple[str, str, str], str] = {
    ("GET", "/api/dev/mint-license", "admin"):
        "该端点不是按角色判定，而是按运行时 DEBUG 开关：非 debug 一律 403。"
        "测试矩阵测的正是「非 debug 应拒绝」，代码静态看是 write 权限 —— 两者都对。",
    ("GET", "/api/ops/incidents/{iid}/diagnose/stream", "demo"):
        "该端点用 SSE ticket 认证（EventSource 无法带 header），并在**函数体内**判角色"
        "（admin/member 可用、demo 不可），静态依赖分析看不到 → 本文档把它归为"
        "「任意登录用户」。以测试矩阵为准：demo 应 403。",
    ("POST", "/api/ops/runbooks/{rid}/execute", "admin"):
        "矩阵用不存在的 rid（404）与护栏拒绝（400/403）做负向验证；"
        "角色判定上 admin 本就允许，冲突来自测试输入而非权限口径。",
}


def cross_check(endpoints: list[dict]) -> tuple[list[str], list[str], int, int]:
    """代码声明的权限 vs 测试矩阵的**明确期望**，只报"真矛盾"。

    判定规则（避免噪音）：
      · 矩阵某角色期望集合里含 2xx  → 矩阵认为"允许"
      · 矩阵某角色期望集合里含 403  → 矩阵认为"禁止"
      · 二者与代码声明的角色集矛盾 → 报为「真不一致」（需要人看）
      · 矩阵该角色只写了 401/404/422（负向输入或未覆盖）→ 不计入，只统计为"未明确"
    """
    ALL = {"admin", "member", "demo"}
    conflicts: list[str] = []
    explained: list[str] = []
    ambiguous = 0
    explicit = 0
    for e in endpoints:
        raw_roles = set((e.get("perm") or {}).get("roles") or [])
        # 归一规则（每条都有依据，不是为了让数字好看）：
        #  · 匿名端点（/api/health、/api/auth/login、/api/auth/demo）任何角色当然都能访问 → 全角色
        #  · "任意登录用户" = 只校验登录、不校验能力位 → 全角色
        #  · "demo(可开关)" 是 require_chat() 的 demo 分支（受 DEMO_CHAT_ENABLED 控制）→ 计入 demo
        if raw_roles & {"匿名", "任意登录用户"}:
            code_set = set(ALL)
        else:
            code_set = {r for r in raw_roles if r in ALL}
            if any("demo" in r for r in raw_roles):
                code_set.add("demo")
        expect = (e.get("perm_matrix") or {}).get("expect") or {}
        for role in sorted(ALL):
            codes = {int(c) for c in (expect.get(role) or [])}
            if not codes:
                continue
            says_allow = any(200 <= c < 300 for c in codes)
            says_deny = 403 in codes
            if not says_allow and not says_deny:
                ambiguous += 1
                continue
            explicit += 1
            if says_allow != (role in code_set):
                key = (e["method"], e["path"], role)
                if key in EXPECTED_DEVIATIONS:
                    explained.append(f"{e['method']} {e['path']} · {role} — " + EXPECTED_DEVIATIONS[key])
                    continue
                conflicts.append(
                    f"{e['method']} {e['path']} · {role}："
                    f"代码={'允许' if role in code_set else '禁止'}，"
                    f"测试矩阵期望={sorted(codes)}（{'允许' if says_allow else '禁止'}）"
                )
    return conflicts, explained, explicit, ambiguous


def _render_html(spec: dict, endpoints: list[dict]) -> str:
    """单文件离线接口文档（无外部 CDN，双击即开）。"""
    data = json.dumps({"spec": spec, "endpoints": endpoints}, ensure_ascii=False)
    # 必须用 **raw 字符串**：模板 JS 里有 `msg:\"ok\"` 这类转义引号，
    # 普通三引号会把 \" 折成 " 从而截断 JS 字符串字面量
    # （实测报 `Unexpected identifier 'ok'`，页面全白且 0 卡片）。
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 运维监控平台 · 接口文档（离线）</title>
<style>
  :root { --bd:#e3e6ea; --mut:#6b7280; --acc:#2563eb; --bg:#f7f8fa; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; color:#111827; }
  header { position:sticky; top:0; background:#fff; border-bottom:1px solid var(--bd); padding:12px 20px; z-index:5; }
  header h1 { margin:0 0 4px; font-size:17px; }
  header .sub { color:var(--mut); font-size:12px; }
  header .sub code { background:var(--bg); padding:1px 5px; border-radius:4px; }
  .wrap { display:flex; min-height:100vh; }
  aside { width:340px; border-right:1px solid var(--bd); padding:12px; background:#fff; height:calc(100vh - 66px); overflow:auto; position:sticky; top:66px; }
  aside input { width:100%; padding:7px 10px; border:1px solid var(--bd); border-radius:6px; margin-bottom:10px; font-size:13px; }
  aside .grp { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.06em; margin:12px 4px 4px; }
  aside a { display:block; padding:5px 8px; border-radius:6px; text-decoration:none; color:#111827; font-size:13px; }
  aside a:hover { background:var(--bg); }
  aside a.on { background:#eef2ff; color:var(--acc); }
  aside a .m { display:inline-block; min-width:52px; font-weight:700; font-size:11px; padding:1px 5px; border-radius:4px; margin-right:6px; text-align:center; color:#fff; }
  .GET { background:#16a34a; } .POST { background:#2563eb; } .PUT { background:#d97706; }
  .PATCH { background:#7c3aed; } .DELETE { background:#dc2626; }
  main { flex:1; padding:20px 28px 60px; max-width:1000px; }
  h2 { font-size:18px; border-bottom:1px solid var(--bd); padding-bottom:6px; margin-top:34px; }
  h3 { font-size:15px; margin-top:24px; }
  .ep { border:1px solid var(--bd); border-radius:10px; padding:14px 16px; margin:14px 0; background:#fff; }
  .ep h3 { margin:0 0 8px; font-size:15px; font-family:ui-monospace,Menlo,monospace; }
  .ep h3 .m { font-size:12px; padding:2px 8px; border-radius:5px; color:#fff; margin-right:8px; }
  .tags span { display:inline-block; background:var(--bg); border:1px solid var(--bd); border-radius:20px; padding:1px 9px; font-size:11px; color:var(--mut); margin-right:6px; }
  table { border-collapse:collapse; width:100%; margin:8px 0 4px; font-size:13px; }
  th,td { border:1px solid var(--bd); padding:5px 8px; text-align:left; vertical-align:top; }
  th { background:var(--bg); font-weight:600; }
  code { background:var(--bg); padding:1px 5px; border-radius:4px; font-family:ui-monospace,Menlo,monospace; font-size:12.5px; }
  .kv { color:var(--mut); font-size:12.5px; margin:3px 0; }
  .warn { background:#fef3c7; border:1px solid #fde68a; padding:8px 12px; border-radius:8px; font-size:13px; }
  .note { background:#eff6ff; border:1px solid #bfdbfe; padding:8px 12px; border-radius:8px; font-size:13px; margin:10px 0; }
  .badge { font-size:11px; border-radius:20px; padding:1px 8px; margin-left:6px; }
  .b-ok { background:#dcfce7; color:#166534; } .b-warn { background:#fef9c3; color:#854d0e; }
</style>
</head>
<body>
<header>
  <h1>AI 运维监控平台 · 接口文档（离线单文件）</h1>
  <div class="sub">来源：运行时 <code>GET /openapi.json</code>（唯一权威，机械生成）· 在线交互版：<code>/docs</code>（Swagger UI）与 <code>/redoc</code></div>
</header>
<div class="wrap">
  <aside>
    <input id="q" placeholder="过滤：路径 / 方法 / 说明…" autocomplete="off">
    <div id="list"></div>
  </aside>
  <main>
    <div class="note">
      权限列来自 <code>test_d8_api_matrix.py</code> 的角色矩阵（被测试守卫锁死）。
      长任务端点（巡检执行 / AI 诊断 / 处置执行）有自己的超时上限，超时返回 <b>504</b> 而非 500。
    </div>
    <h2 id="conv">通用约定</h2>
    <p><b>认证</b>：登录 <code>POST /api/auth/login</code> 取 <code>access_token</code>，后续带
      <code>Authorization: Bearer &lt;token&gt;</code>。SSE 流式端点用一次性 ticket
      （<code>GET /api/sse-ticket</code>，60 秒、消费一次）。</p>
    <p><b>统一信封</b>：<code>{"code":0,"msg":"ok","data":{}}</code>。错误码：
      <code>400</code> 校验失败 · <code>401</code> 未认证 · <code>403</code> 无权限 ·
      <code>404</code> 资源不存在 · <code>409</code> 冲突 · <code>422</code> 参数不合法 ·
      <code>429</code> LLM 配额用尽 · <code>500</code> 未预期异常 · <code>504</code> 长任务超时。</p>
    <p><b>LLM 配额</b>：<code>llm_usage.role</code> 记录调用归属（用户角色 / <code>engine</code> 后台任务）。
      演示站限额<b>只统计 demo 自己</b>；平台限额统计全平台。0 = 不限，UTC 次日 00:00 重置。</p>
    <p><b>分页</b>：列表端点接受 <code>page</code>(≥1) 与 <code>page_size</code>(1~200)，
      返回 <code>{items,total,page,page_size}</code>；非法值一律 422。</p>
    <h2 id="eps">端点</h2>
    <div id="eps"></div>
  </main>
</div>
<script>
const DATA = __DATA__;
const CODE = {0:"成功",400:"校验失败",401:"未认证",403:"无权限",404:"资源不存在",409:"冲突",422:"参数不合法",429:"LLM 配额用尽",500:"未预期异常",504:"长任务超时"};
const el = (t,c,txt) => { const e=document.createElement(t); if(c)e.className=c; if(txt!=null)e.textContent=txt; return e; };
function permText(e){
  const p = e.perm || {};
  const roles = p.roles || [];
  if(!roles.length) return ["权限未解析","b-warn"];
  if(roles.length===1 && roles[0]==="匿名") return ["匿名可访问","b-ok"];
  if(roles.length===1 && roles[0].indexOf("Agent")===0) return ["Agent 令牌","b-ok"];
  const label = roles.join("/") + (p.cap ? "（"+p.cap+"）" : "");
  return [label,"b-ok"];
}
function render(){
  const q = (document.getElementById("q").value||"").toLowerCase();
  const list = document.getElementById("list"), eps = document.getElementById("eps");
  list.innerHTML=""; eps.innerHTML="";
  const groups = {};
  DATA.endpoints.filter(e => !q || (e.method+" "+e.path+" "+e.summary+" "+(e.perm&&e.perm.note||"")).toLowerCase().includes(q))
    .forEach(e => { const t=e.tags[0]||"(未分组)"; (groups[t]=groups[t]||[]).push(e); });
  Object.keys(groups).sort().forEach(tag => {
    list.appendChild(el("div","grp",tag));
    groups[tag].forEach(e => {
      const a = el("a"); a.href = "#ep-" + btoa(e.method+e.path).replace(/[^a-zA-Z0-9]/g,"");
      const m = el("span","m "+e.method,e.method); a.appendChild(m);
      a.appendChild(document.createTextNode(e.path));
      a.onclick = () => { document.querySelectorAll("aside a").forEach(x=>x.classList.remove("on")); a.classList.add("on"); };
      list.appendChild(a);
      eps.appendChild(card(e));
    });
  });
}
function card(e){
  const box = el("div","ep"); box.id = "ep-" + btoa(e.method+e.path).replace(/[^a-zA-Z0-9]/g,"");
  const h = el("h3"); h.appendChild(el("span","m "+e.method,e.method));
  h.appendChild(document.createTextNode(e.path)); box.appendChild(h);
  const tags = el("div","tags");
  (e.tags||[]).forEach(t => tags.appendChild(el("span",null,t)));
  const [pt,pcls] = permText(e); tags.appendChild(el("span","badge "+pcls,pt));
  if(e.deprecated) tags.appendChild(el("span","badge b-warn","已弃用"));
  box.appendChild(tags);
  const kvNote = (e.perm_matrix && e.perm_matrix.note) || e.summary || "";
  if(kvNote) box.appendChild(el("div","kv",kvNote));
  if(e.perm && e.perm.roles && e.perm.roles.length)
    box.appendChild(el("div","kv","权限（来自代码依赖声明）：" + e.perm.roles.join("/") + (e.perm.cap ? "；能力位 " + e.perm.cap : "")));
  if(!e.registered) box.appendChild(el("div","warn","⚠️ 该端点未登记进权限矩阵表（test_d8_api_matrix.py 的 ROUTES），守卫测试会失败，请补登记。"));
  if(e.perm_matrix && e.perm_matrix.expect) box.appendChild(el("div","kv","测试矩阵期望状态码：" + JSON.stringify(e.perm_matrix.expect)));
  const pp = (e.params||[]).filter(p=>p.in==="path"), qp = (e.params||[]).filter(p=>p.in==="query");
  box.appendChild(el("div","kv","路径参数：" + (pp.length? pp.map(p=>p.name).join(", ") : "无")));
  if(qp.length){
    box.appendChild(el("div","kv","查询参数："));
    const t = el("table"); const th = el("tr"); ["名称","必填","类型"].forEach(x=>th.appendChild(el("th",null,x))); t.appendChild(th);
    qp.forEach(p=>{ const tr=el("tr"); tr.appendChild(el("td",null,p.name));
      tr.appendChild(el("td",null,p.required?"✅":"")); tr.appendChild(el("td",null,typeOf(p.schema))); t.appendChild(tr); });
    box.appendChild(t);
  }
  if(e.body){
    const s = ((((e.body.content||{})["application/json"]||{}).schema)||{});
    box.appendChild(el("div","kv","请求体（" + (e.body.required?"必填":"可选") + "）：" + refName(s)));
    const name = refName(s, true);
    const sch = name && DATA.spec.components && DATA.spec.components.schemas && DATA.spec.components.schemas[name];
    if(sch && sch.properties){
      const t = el("table"); const th = el("tr"); ["字段","类型","必填","约束"].forEach(x=>th.appendChild(el("th",null,x))); t.appendChild(th);
      const req = new Set(sch.required||[]);
      Object.keys(sch.properties).forEach(k=>{
        const v = sch.properties[k]; const tr = el("tr");
        tr.appendChild(el("td",null,k)); tr.appendChild(el("td",null,v.type||(v["$ref"]?"obj":"any")));
        tr.appendChild(el("td",null,req.has(k)?"✅":""));
        const c = []; ["enum","minimum","maximum","maxLength","maxItems"].forEach(kk=>{ if(v[kk]!=null) c.push(kk+"="+JSON.stringify(v[kk])); });
        tr.appendChild(el("td",null,c.join(" "))); t.appendChild(tr);
      });
      box.appendChild(t);
    }
  } else box.appendChild(el("div","kv","请求体：无"));
  const resp = el("div","kv"); resp.textContent = "响应：200 → {code:0,msg:\"ok\",data}；错误码见「通用约定」";
  box.appendChild(resp);
  return box;
}
function refName(s, bare){
  if(!s) return "";
  if(s["$ref"]) return bare ? s["$ref"].split("/").pop() : s["$ref"].split("/").pop() + "（见下表）";
  if(s.type==="array") return "[" + refName(s.items||{}, bare) + "]";
  return s.type || "any";
}
function typeOf(s){ if(!s) return "";
  if(s["$ref"]) return s["$ref"].split("/").pop();
  if(s.type==="array") return "["+typeOf(s.items||{})+"]";
  let x = s.type||"any"; if(s.enum) x += " 枚举" + JSON.stringify(s.enum);
  if(s.minimum!=null) x += " >="+s.minimum; if(s.maximum!=null) x += " <="+s.maximum;
  return x; }
document.getElementById("q").addEventListener("input", render);
render();
</script>
</body>
</html>
""".replace("__DATA__", data)


if __name__ == "__main__":
    sys.exit(main())
