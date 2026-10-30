#!/usr/bin/env python3
"""把「每个端点需要什么权限」从**代码里**导出来（供接口文档生成使用）。

为什么不去解析源码或猜：权限是 `Depends(require("write"))` 这类依赖注入声明的，
只有把 app 真的 import 起来、遍历 `app.routes` 读 `dependant`，拿到的才是**运行时真实值**。
`require()` / `require_chat()` 返回的闭包上挂了 `_cap` 标记（见 core/security.py），
这里读它；没有 `_cap` 也没有认证依赖的端点 = 匿名可访问。

用法（必须能 import app，即要用后端 venv 且 cwd 在 backend/）：
    cd backend && python ../tools/dump_route_perms.py > /tmp/route_perms.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


def find_cap(dependant) -> str | None:
    """在依赖树里找带 `_cap` 标记的可调用对象。"""
    for dep in getattr(dependant, "dependencies", []) or []:
        call = dep.call
        cap = getattr(call, "_cap", None)
        if cap:
            return cap
        found = find_cap(dep)
        if found:
            return found
    return None


def has_auth(dependant) -> bool:
    """依赖树里是否出现认证依赖（get_current_user / require 系列都依赖它）。"""
    for dep in getattr(dependant, "dependencies", []) or []:
        name = getattr(dep.call, "__name__", "")
        if name in ("get_current_user", "_dep", "require_chat", "require"):
            return True
        if has_auth(dep):
            return True
    return False


def _iter_api_routes(app):
    """枚举所有 API 路由，兼容两种 FastAPI 形态。

    坑：本项目的 FastAPI 版本把 `include_router` 做成了**懒加载**的
    `_IncludedRouter` 占位符——`app.routes` 里看不到具体端点（只有 16 个顶层项），
    必须下钻到 `original_router.routes` 并补上 `include_context.prefix`。
    老版本是扁平化的，故两条路径都支持。
    """
    seen = set()
    for route in app.routes:
        name = type(route).__name__
        if name == "_IncludedRouter":
            orig = getattr(route, "original_router", None)
            prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            for sub in getattr(orig, "routes", []) or []:
                sub_path = getattr(sub, "path", None)
                if sub_path is None:
                    continue
                full = prefix + sub_path
                # 去重必须带方法：同一路径可以同时有 GET 与 POST 两个独立路由对象
                # （实测踩坑：`/api/ops/notify-channels` 的 POST 被纯路径去重丢掉，
                #   导致接口文档里那条端点的权限查不到 → 权限列显示"未知"）
                sig = (full, tuple(sorted(getattr(sub, "methods", []) or [])))
                if sig in seen:
                    continue
                seen.add(sig)
                yield _WithPath(sub, full)
        elif name in ("APIRoute", "_APIRoute"):
            yield route


class _WithPath:
    """给子路由套一个"带完整路径"的代理（只覆写 path）。"""

    def __init__(self, route, path: str):
        self._route = route
        self.path = path

    def __getattr__(self, item):
        return getattr(self._route, item)


def main() -> int:
    from app.core.security import PERM_MATRIX
    from app.main import create_app

    app = create_app()
    out = []
    for route in _iter_api_routes(app):
        path = getattr(route, "path", None)
        methods = sorted(getattr(route, "methods", []) or [])
        if not path or not path.startswith("/api"):
            continue
        dep = getattr(route, "dependant", None)
        cap = find_cap(dep) if dep else None
        authned = has_auth(dep) if dep else False
        if cap == "chat":
            roles = sorted(PERM_MATRIX["diag"]) + ["demo(可开关)"]
        elif cap:
            roles = sorted(PERM_MATRIX[cap])
        elif authned:
            roles = ["任意登录用户"]
        else:
            roles = ["匿名"]
        for m in methods:
            if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                out.append({"method": m.upper(), "path": path, "cap": cap,
                            "auth": authned, "roles": roles})
    json.dump({"endpoints": sorted(out, key=lambda x: (x["path"], x["method"])),
               "perm_matrix": {k: sorted(v) for k, v in PERM_MATRIX.items()}},
              sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
