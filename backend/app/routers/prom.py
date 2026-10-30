"""Prometheus 数据源管理 + 指标查询代理（FR-B3 轨 B；05 §3）。

- CRUD：凭据 AES-GCM 加密落库（复用 llm_gateway.encrypt_secret）
- 连接测试：/api/v1/query 探活，写 last_check_status/last_check_at
- 指标查询代理：source=prom 时转发 query_range，结果不写 rings（03 §1.6）
"""
from datetime import datetime, timedelta

from fastapi import HTTPException, APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.security import audit, require
from app.db.session import get_db
from app.models.prom import PromDatasource
from app.models.shell import User
from app.services import prom as prom_svc
from app.services.llm_gateway import encrypt_secret

router = APIRouter(prefix="/api/ops", tags=["ops-prom"])


class PromDsIn(BaseModel):
    name: str
    base_url: str
    auth_type: str = "none"
    username: str = ""
    secret: str = ""          # 明文入参，落库前加密；留空表示不修改（更新时）
    timeout_s: int = 10
    scrape_interval_s: int = 30     # D157：mode=scrape 拉取间隔（秒）；0=不自动拉
    enabled: bool = True
    is_default: bool = False
    mode: str = "query"                    # query=查询代理（轨 B）| scrape=直拉 exporter（FR-B3+）
    scrape_config: dict = {}               # mode=scrape：{"path":"/metrics","metric_keep":["node_..."]}


def _out(d) -> dict:
    return {"id": d.id, "name": d.name, "base_url": d.base_url, "auth_type": d.auth_type,
            "username": d.username, "has_secret": bool(d.secret_encrypted), "timeout_s": d.timeout_s,
            "mode": getattr(d, "mode", "query"), "scrape_config": getattr(d, "scrape_config", None) or {},
            "enabled": d.enabled, "is_default": d.is_default, "last_check_status": d.last_check_status,
            "last_check_at": str(d.last_check_at or "")}


@router.get("/prom-datasources")
async def list_ds(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(PromDatasource).order_by(PromDatasource.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [_out(d) for d in rows], "total": len(rows)}}


def _validate_ds(body: PromDsIn) -> None:
    """数据源入参校验（D59）：空名/空地址原先能落库，测试按钮只会报"连接失败"，
    用户无从知道是地址没填；URL 不校验 scheme 时填个 "127.0.0.1:9090"（漏 http://）
    会在 httpx 层报一堆原始错误。"""
    if not (body.name or "").strip():
        raise HTTPException(400, "数据源名称为必填项（1~64 字符）")
    if len(body.name.strip()) > 64:
        raise HTTPException(400, "数据源名称过长（≤64 字符）")
    url = (body.base_url or "").strip()
    if not url:
        raise HTTPException(400, "地址为必填项（如 http://127.0.0.1:9090）")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "地址需以 http:// 或 https:// 开头（如 http://127.0.0.1:9090）")
    if len(url) > 255:
        raise HTTPException(400, "地址过长（≤255 字符）")
    if body.auth_type not in ("none", "basic", "bearer"):
        raise HTTPException(400, "认证方式只允许 none/basic/bearer")
    if not (1 <= int(body.timeout_s) <= 120):
        raise HTTPException(400, "超时时间需在 1~120 秒之间")
    # mode 不做 400 拒绝：未知值归一化为 query（落库值必须有定义语义，
    # 但矩阵/老客户端会给任意字符串，拒绝它们收益为负——scrape 才是特例，其余都按代理处理）。


@router.post("/prom-datasources")
async def create_ds(body: PromDsIn, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    _validate_ds(body)
    if body.is_default:  # 唯一默认
        for d in (await db.execute(select(PromDatasource).where(PromDatasource.is_default.is_(True)))).scalars().all():
            d.is_default = False
    d = PromDatasource(name=body.name.strip(), base_url=body.base_url.strip(), auth_type=body.auth_type,
                       username=body.username, secret_encrypted=encrypt_secret(body.secret),
                       timeout_s=body.timeout_s, scrape_interval_s=body.scrape_interval_s,
                       enabled=body.enabled, is_default=body.is_default,
                       mode=("scrape" if body.mode == "scrape" else "query"), scrape_config=body.scrape_config or {})
    db.add(d)
    await db.commit()
    await audit(db=db, request=request, user=user, action="prom_ds_create", resource=f"prom:{d.id}")
    return {"code": 0, "msg": "ok", "data": {"id": d.id}}


@router.put("/prom-datasources/{did}")
async def update_ds(did: int, body: PromDsIn, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    d = await db.get(PromDatasource, did)
    if d is None:
        raise HTTPException(404, "资源不存在或已被删除")
    _validate_ds(body)
    if body.is_default:
        for x in (await db.execute(select(PromDatasource).where(PromDatasource.is_default.is_(True)))).scalars().all():
            x.is_default = False
    d.name, d.base_url, d.auth_type = body.name.strip(), body.base_url.strip(), body.auth_type
    d.username, d.timeout_s, d.enabled, d.is_default = body.username, body.timeout_s, body.enabled, body.is_default
    if hasattr(d, "scrape_interval_s"):
        d.scrape_interval_s = body.scrape_interval_s
    d.mode = "scrape" if body.mode == "scrape" else "query"
    if body.scrape_config is not None:
        d.scrape_config = body.scrape_config
    if body.secret:  # 留空=不修改已存凭据
        d.secret_encrypted = encrypt_secret(body.secret)
    await db.commit()
    await audit(db=db, request=request, user=user, action="prom_ds_update", resource=f"prom:{did}")
    # D157：间隔或 mode 变了 → 重新注册 scrape 任务（APScheduler 同 id 替换）
    try:
        from app.sched import _safe_register_all_scrape_jobs
        await _safe_register_all_scrape_jobs()   # D161-4：改 await（原同步写法打异步引擎必抛 TypeError）
    except Exception:  # noqa: BLE001 —— 任务注册失败不影响主路径
        pass
    return {"code": 0, "msg": "ok", "data": {"id": did}}


@router.delete("/prom-datasources/{did}")
async def delete_ds(did: int, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    d = await db.get(PromDatasource, did)
    if d:
        await db.delete(d)
        await db.commit()
    await audit(db=db, request=request, user=user, action="prom_ds_delete", resource=f"prom:{did}")
    return {"code": 0, "msg": "ok", "data": {"id": did}}


@router.post("/prom-datasources/{did}/test")
async def test_ds(did: int, db=Depends(get_db), _: User = Depends(require("diag"))):
    d = await db.get(PromDatasource, did)
    if d is None:
        raise HTTPException(404, "资源不存在或已被删除")
    ok, detail = await prom_svc.check(d)
    d.last_check_status = "ok" if ok else "failed"
    d.last_check_at = datetime.utcnow()
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"ok": ok, "detail": detail}}


@router.post("/prom-datasources/{did}/scrape-now")
async def scrape_now(did: int, db=Depends(get_db), _: User = Depends(require("diag"))):
    """立即直采一次（FR-B3+ 验收入口）：返回解析出的平台指标，便于"配完立刻验证"。

    入库效果：合成主机 `[scrape] <数据源名>` 会出现在主机列表，其 CPU/内存/磁盘可建规则。
    """
    from app.services import prom_scrape

    ds = await db.get(PromDatasource, did)
    if ds is None:
        raise HTTPException(404, "资源不存在或已被删除")
    cfg = getattr(ds, "scrape_config", None) or {}
    res = await prom_scrape.scrape_once(ds, cfg.get("path", "/metrics"), cfg.get("metric_keep") or [],
                                        user_map=cfg.get("metric_map") or {},
                                        tag_keep=cfg.get("tag_keep") or None)
    return {"code": 0, "msg": "ok" if res["ok"] else res["note"], "data": res}


@router.get("/prom/query")
async def prom_query(datasource_id: int, query: str, minutes: int = 60, step_s: int = 15,
                     db=Depends(get_db), _: User = Depends(require("read"))):
    """指标查询代理（05 §3）：原样转发 query_range，不写 rings。"""
    d = await db.get(PromDatasource, datasource_id)
    if d is None:
        raise HTTPException(404, "资源不存在或已被删除")
    now = datetime.utcnow()
    try:
        body = await prom_svc.query_range_raw(d, query, now - timedelta(minutes=minutes), now, step_s)
    except prom_svc.PromUnreachable as e:
        return {"code": 503, "msg": f"PROM_UNREACHABLE: {e}", "data": None}
    return {"code": 0, "msg": "ok", "data": body}
