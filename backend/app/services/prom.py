"""Prometheus 轨（FR-B3）数据源访问与 PromQL 查询（03 §1.6）。

设计要点（03 §1.6 冻结）：
- 轨 B 数据不落本地 rings，评估时直接向数据源 `POST/GET /api/v1/query_range` 取点；
- 认证可选 none/basic/bearer（凭据 AES-GCM 加密存储）；
- 超时默认 10s；不可达/查询报错 → 抛 PromUnreachable，由规则侧记 eval_error（不等同"指纹消失"，不产生恢复误报）。
"""
from __future__ import annotations

from datetime import datetime

import httpx

from app.services.llm_gateway import decrypt_secret


class PromUnreachable(Exception):
    """数据源不可达 / 查询报错 / 响应非法。"""


def auth_headers(ds) -> dict:
    if ds.auth_type == "basic":
        import base64

        raw = f"{ds.username}:{decrypt_secret(ds.secret_encrypted)}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    if ds.auth_type == "bearer":
        return {"Authorization": f"Bearer {decrypt_secret(ds.secret_encrypted)}"}
    return {}


async def query_range(ds, query: str, start: datetime, end: datetime, step_s: int = 15) -> list[tuple[dict, float]]:
    """查询区间序列，返回 [(labels, value)]——每条序列取窗口内最后一个点（与轨 A detect 输入一致）。

    Prometheus 返回 matrix：{"data":{"result":[{"metric":{...},"values":[[ts, "v"], ...]}]}}
    """
    url = ds.base_url.rstrip("/") + "/api/v1/query_range"
    params = {"query": query, "start": int(start.timestamp()), "end": int(end.timestamp()), "step": f"{step_s}s"}
    try:
        async with httpx.AsyncClient(timeout=ds.timeout_s or 10) as c:
            r = await c.get(url, params=params, headers=auth_headers(ds))
    except httpx.HTTPError as e:
        raise PromUnreachable(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise PromUnreachable(f"HTTP {r.status_code}: {r.text[:160]}")
    try:
        body = r.json()
        if body.get("status") != "success":
            raise PromUnreachable(f"status={body.get('status')} error={body.get('error')}")
        result = body["data"]["result"]
    except PromUnreachable:
        raise
    except Exception as e:  # noqa: BLE001 —— 响应结构异常一律按不可达处理
        raise PromUnreachable(f"响应解析失败: {type(e).__name__}: {e}") from e

    out: list[tuple[dict, float]] = []
    for series in result:
        vals = series.get("values") or []
        if not vals:
            continue
        out.append((dict(series.get("metric") or {}), float(vals[-1][1])))
    return out


async def query_range_raw(ds, query: str, start: datetime, end: datetime, step_s: int = 15) -> dict:
    """指标查询代理用：原样返回数据源响应（不写 rings，03 §1.6）。"""
    url = ds.base_url.rstrip("/") + "/api/v1/query_range"
    params = {"query": query, "start": int(start.timestamp()), "end": int(end.timestamp()), "step": f"{step_s}s"}
    try:
        async with httpx.AsyncClient(timeout=ds.timeout_s or 10) as c:
            r = await c.get(url, params=params, headers=auth_headers(ds))
    except httpx.HTTPError as e:
        raise PromUnreachable(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:  # HTTP 500 等不是 httpx.HTTPError，须显式判为不可达
        raise PromUnreachable(f"HTTP {r.status_code}: {r.text[:160]}")
    try:
        return {"http_status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:2000]}
    except Exception as e:  # noqa: BLE001
        raise PromUnreachable(f"响应解析失败: {type(e).__name__}: {e}") from e


async def check(ds) -> tuple[bool, str]:
    """连接测试：调用 /api/v1/query 探活（up 指标不必存在，只要网关与 PromQL 引擎响应即可）。"""
    url = ds.base_url.rstrip("/") + "/api/v1/query"
    try:
        async with httpx.AsyncClient(timeout=ds.timeout_s or 10) as c:
            r = await c.get(url, params={"query": "1"}, headers=auth_headers(ds))
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:160]}"
        body = r.json()
        if body.get("status") != "success":
            return False, f"status={body.get('status')}"
        return True, f"ok（{ds.base_url}）"
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
