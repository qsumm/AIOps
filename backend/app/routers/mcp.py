"""MCP 端点（FR-425；D154）：把平台已有的 AI 工具集暴露给外部 MCP 客户端。

设计要点（见 ADR-004 与《13-三项能力补齐》§6）：

1. **同一份工具定义**：工具清单与执行直接复用 `rca_tools.tool_specs()` / `invoke()`，
   不实现第二套（否则必然能力漂移）。
2. **写工具默认不注册**：`rca_tools` 当前只含只读工具 + `write_findings`（诊断终态写入）。
   `write_findings` 同样默认不注册——MCP 客户端默认**纯只读**；要放开写工具需显式配置
   `MCP_ENABLE_WRITES=true`（对齐夜莺"写工具要在配置里显式打开才会注册"）。
3. **权限与调用者一致**：MCP 客户端用**平台用户的 Bearer 令牌**调用（`require("read")`），
   每次工具调用全量审计（action=`mcp_tool_call`）——客户端拿不到令牌主人看不到的数据。
4. **端点默认关闭**：需 `MCP_ENABLED=true` 显式开启（避免无意间把内部 API 暴露出去）。
5. **协议**：MCP Streamable-HTTP 传输（JSON-RPC 2.0 over POST），**零新依赖**
   （AGENTS.md 红线：不引入清单外依赖——官方 SDK 因此不装）。

支持的方法：`initialize` / `tools/list` / `tools/call` / `ping`。
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.security import audit, get_current_user
from app.db.session import get_db
from app.models.shell import User

router = APIRouter(prefix="/mcp", tags=["mcp"])

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "aiops-platform"
SERVER_VERSION = "1.4.0"


def _mcp_enabled() -> bool:
    """端点开关（默认关闭——避免无意间把内部工具暴露出去）。"""
    return os.getenv("MCP_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _writes_enabled() -> bool:
    """写工具开关（默认关闭——MCP 客户端默认纯只读）。"""
    return os.getenv("MCP_ENABLE_WRITES", "").strip().lower() in ("1", "true", "yes", "on")


def _read_only_tool_names() -> set[str]:
    """允许通过 MCP 调用的工具名（tool_specs 里的全部 + 按开关放开写工具）。

    rca_tools.tool_specs() 的 `write_findings` 是**诊断终态写入**（写工具）——
    默认不注册。写工具放开后也不含"改配置"类动作（当前 DISPATCH 里没有这类工具）。
    """
    from app.services import rca_tools

    specs = {s["function"]["name"] for s in rca_tools.tool_specs()}
    if not _writes_enabled():
        specs.discard("write_findings")
    return specs


def _tool_list() -> list[dict]:
    """tools/list 的响应（OpenAI function-calling → MCP tool 形状转换）。"""
    from app.services import rca_tools

    allowed = _read_only_tool_names()
    out = []
    for s in rca_tools.tool_specs():
        fn = s.get("function") or {}
        name = fn.get("name") or ""
        if name not in allowed:
            continue
        out.append({
            "name": name,
            "description": fn.get("description") or "",
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


async def _call_tool(name: str, args: dict, db, user: User, request: Request) -> dict:
    """执行一个工具并审计。"""
    from app.services import rca_tools

    if name not in _read_only_tool_names():
        return {"error": f"工具 {name!r} 未通过 MCP 开放（未注册或属于写工具且 MCP_ENABLE_WRITES 未开启）"}
    result = await rca_tools.invoke(name, args or {})
    try:
        await audit(db=db, request=request, user=user, action="mcp_tool_call",
                    resource=f"tool:{name}")
    except Exception:  # noqa: BLE001 —— 审计失败不阻断调用
        pass
    # MCP tools/call 的结果形状：content[] 文本块
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)[:60000]}]}


def _rpc_err(code: int, msg: str, rid=None) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


@router.post("")
async def mcp_endpoint(request: Request, db=Depends(get_db), user: User = Depends(get_current_user)):
    """MCP Streamable-HTTP 传输端点（JSON-RPC 2.0）。

    鉴权：与平台其它 API 相同的 Bearer 令牌（get_current_user）——
    客户端在 MCP 配置的 headers 里放 `Authorization: Bearer <平台令牌>` 即可以
    **该用户自己的权限**调用工具。
    """
    if not _mcp_enabled():
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32000,
                                       "message": "MCP 端点未开启（需环境变量 MCP_ENABLED=true）"}},
                            status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_rpc_err(-32700, "解析错误：请求体不是合法 JSON"), status_code=400)

    rid = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tool_list()}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        result = await _call_tool(name, args, db, user, request)
        if "error" in result:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": result["error"]}], "isError": True}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    return _rpc_err(-32601, f"未知方法 {method!r}（支持 initialize/tools/list/tools/call/ping）", rid)


@router.get("")
async def mcp_info():
    """GET：仅提示端点状态（MCP 客户端用 POST）。"""
    return {"code": 0, "msg": "ok", "data": {
        "mcp_enabled": _mcp_enabled(),
        "writes_enabled": _writes_enabled(),
        "endpoint": "POST /mcp（JSON-RPC 2.0，Bearer 令牌鉴权）",
        "hint": "在 MCP 客户端配置 {\"url\": \"<平台>/mcp\", \"headers\": {\"Authorization\": \"Bearer <平台令牌>\"}}",
    }}
