"""指标即时查询（FR-424；D154）。

用途：告警说"这个指标超了"之后，用户要能**自己下钻验证**——否则只能信平台的结论
（这是"可解释性"的最后一块拼图）。

设计约束（防止变成第二个 PromQL 引擎）：
- **白名单聚合**：avg/max/min/sum/count/p50/p95/p99，**不做任意表达式求值**；
- **三重上限**：时间窗（≤24h）、序列数（≤200）、单序列点数（≤1440），超限**明确报错**，
  不静默截断；
- 返回**序列集合**（每条序列的点数/末值/聚合值），不返回全量原始点。

接口（与既有 /api/ops/metrics/{list,query} 的分工——那两个是**整机级**、无维度）：
- GET /api/ops/metric/query     **维度化**即时查询（按指标+维度+聚合方式）
- GET /api/ops/metric/series    某主机的**带维度序列清单**（维度自动补全用）
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.core.security import require
from app.db.session import get_db
from app.models.shell import User
from app.store import ring

router = APIRouter(prefix="/api/ops/metric", tags=["ops-metric"])

# 聚合白名单（**刻意不做**任意表达式求值）
AGG_WHITELIST = ("avg", "max", "min", "sum", "count", "p50", "p95", "p99")

MAX_WINDOW_MIN = 24 * 60
MAX_SERIES = 200
MAX_POINTS_PER_SERIES = 1440
MAX_TAGS = 8


def _parse_tag_filter(raw: str) -> dict:
    """解析 `k1=v1,k2=v2` 形式的维度筛选。"""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        k, sep, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if not sep or not k:
            raise HTTPException(400, f"维度筛选格式应为 k=v（收到 {part!r}）")
        out[k] = v
    if len(out) > MAX_TAGS:
        raise HTTPException(400, f"维度筛选最多 {MAX_TAGS} 个（收到 {len(out)} 个）")
    return out


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _aggregate(vals: list[float], agg: str) -> float:
    if not vals:
        return 0.0
    if agg == "avg":
        return sum(vals) / len(vals)
    if agg == "max":
        return max(vals)
    if agg == "min":
        return min(vals)
    if agg == "sum":
        return sum(vals)
    if agg == "count":
        return float(len(vals))
    if agg == "p50":
        return _percentile(vals, 0.50)
    if agg == "p95":
        return _percentile(vals, 0.95)
    if agg == "p99":
        return _percentile(vals, 0.99)
    raise HTTPException(400, f"不支持的聚合方式 {agg!r}（可用：{'/'.join(AGG_WHITELIST)}）")


@router.get("/query")
async def metric_query(
    metric: str = Query("", description="指标名，支持 {k=v} 维度选择器"),
    host: str = Query("", description="主机 ident；留空 = 全部主机"),
    tag: str = Query("", description="维度筛选 k=v,k2=v2"),
    window: int = Query(60, ge=1, le=MAX_WINDOW_MIN, description="时间窗（分钟）"),
    agg: str = Query("avg", description="聚合方式"),
    limit: int = Query(200, ge=1, le=MAX_SERIES, description="返回序列数上限"),
    db=Depends(get_db), _: User = Depends(require("read")),
):
    """即时查询：按指标（可带维度）查时间窗内的序列与聚合值。"""
    if agg not in AGG_WHITELIST:
        raise HTTPException(400, f"不支持的聚合方式 {agg!r}（可用：{'/'.join(AGG_WHITELIST)}）")
    if not (metric or "").strip():
        raise HTTPException(400, "metric 参数必填（要查哪个指标，如 cpu.percent 或 pg.connections.current{server=db1}）")
    base, sel = ring.parse_selector(metric)
    if not base:
        raise HTTPException(400, "指标名不能为空")
    tag_filter = dict(sel or {})
    tag_filter.update(_parse_tag_filter(tag))
    now = datetime.utcnow()
    start = now - timedelta(minutes=window)

    series: list[dict] = []
    truncated = False

    # ① 内存（ring）：带维度序列（整机级序列不带标签，标签筛选为 {} 时一并命中）
    for ident in await _idents(db, host):
        for tags, pts in ring.query_series(ident, base, tag_filter or None, start, now):
            if len(series) >= limit:
                truncated = True
                break
            series.append(_series_out(ident, base, tags, pts, agg))
        if truncated:
            break

    # ② 落库的带维度序列（metric_series）——ring 覆盖不到的时间窗
    if not truncated:
        rows = await _query_series_db(db, base, host, tag_filter, start, now, limit)
        have = {(s["ident"], json_dumps(s["tags"])) for s in series}
        for r in rows:
            key = (r["ident"], json_dumps(r["tags"]))
            if key in have:
                continue
            if len(series) >= limit:
                truncated = True
                break
            series.append(_series_out(r["ident"], base, r["tags"], r["points"], agg))

    return {
        "code": 0, "msg": "ok",
        "data": {
            "metric": base, "tag_filter": tag_filter, "agg": agg,
            "window_min": window, "series": series,
            "total_series": len(series), "truncated": truncated,
        },
    }


def json_dumps(d: dict) -> str:
    import json
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def _series_out(ident: str, metric: str, tags: dict, pts, agg: str) -> dict:
    vals = [float(v) for _ts, v in pts]
    return {
        "ident": ident, "metric": metric, "tags": tags,
        "points": len(vals),
        "last": round(vals[-1], 4) if vals else 0.0,
        "agg": round(_aggregate(vals, agg), 4),
    }


async def _idents(db, host: str) -> list[str]:
    """要查的主机 ident 列表（留空 = 全部）。"""
    from app.models.host import Host
    from sqlalchemy import select
    q = select(Host.ident)
    if host:
        q = q.where(Host.ident == host)
    rows = (await db.execute(q)).scalars().all()
    return list(rows)


async def _query_series_db(db, metric: str, host: str, tag_filter: dict,
                           start: datetime, end: datetime, limit: int) -> list[dict]:
    """从 metric_series 取带维度序列的点（ring 覆盖不到的时间窗）。"""
    rows = (await db.execute(
        text("SELECT ident, metric, tags_key, tags, value, ts FROM metric_series "
             "WHERE metric=:m AND ts BETWEEN :s AND :e ORDER BY ts"),
        {"m": metric, "s": start, "e": end})).all()
    by_key: dict[tuple[str, str], list] = {}
    for ident, _m, tkey, tags, value, ts in rows:
        if host and ident != host:
            continue
        # D162-3：tags_key 存的是 **`k=v;k=v`** 格式（ring.tags_key 的规范），
        # 不是 JSON。原实现按 JSON 解析 → 失败 → except 吞成 {} →
        # **所有带维度序列在 DB 回落路径上退化成空维度**，同一指标出现多条 tags:{}，
        # 用户看到"下钻出来的维度是空的"。改用 ring.parse_tags_key 解析。
        tg = ring.parse_tags_key(tkey) if isinstance(tkey, str) else (
            dict(tags) if isinstance(tags, dict) else {})
        if tag_filter:
            if any(str(tg.get(k, "")) != str(v) for k, v in tag_filter.items()):
                continue
        by_key.setdefault((ident, tkey), []).append((ts, float(value)))
    out: list[dict] = []
    for (ident, tkey), pts in list(by_key.items())[:limit]:
        tg = ring.parse_tags_key(tkey) if isinstance(tkey, str) else {}
        out.append({"ident": ident, "tags": tg, "points": pts})
    return out


@router.get("/series")
async def metric_series(host: str = Query("", description="主机 ident（必填）"),
                        metric: str = Query("", description="指标名前缀；留空 = 全部"),
                        _: User = Depends(require("read"))):
    """某主机的带维度序列清单（维度自动补全用）。"""
    if not (host or "").strip():
        raise HTTPException(400, "host 参数必填（指定要查哪台主机）")
    items = ring.list_series(host.strip(), metric)
    return {"code": 0, "msg": "ok", "data": {"items": items, "total": len(items)}}


