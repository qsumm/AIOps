"""AI 诊断评测端点（FR-428d；15 号方案 §3.1）。admin 专属（demo 403）。

  GET  /api/ops/eval/cases        case 池清单（含池/来源/期望根因/标注状态）
  POST /api/ops/eval/runs         跑一轮评测 {pool: tune|regress, case_ids?}
  GET  /api/ops/eval/runs         历次评测（含版本指纹与汇总指标，对比矩阵数据源）
  PUT  /api/ops/eval/cases/{id}   人工标注（expected/label_status/pool 调整）

评测数字只进本端点消费的内部页面（红线 2：不进任何对外宣传材料）。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.security import audit, get_current_user, require
from app.db.session import get_db
from app.models.eval import EvalCase, EvalRun
from app.services import eval as ev_svc

router = APIRouter(prefix="/api/ops/eval", tags=["eval"])


def _admin(user) -> None:
    if user.role != "admin":
        raise HTTPException(403, "诊断评测仅管理员可用")


@router.get("/cases")
async def list_cases(pool: str = "", page: int = 1, page_size: int = 50,
                     db=Depends(get_db), user=Depends(require("write"))):
    _admin(user)
    q, qc = select(EvalCase).order_by(EvalCase.id.desc()), select(func.count()).select_from(EvalCase)
    if pool in ("tune", "regress"):
        q, qc = q.where(EvalCase.pool == pool), qc.where(EvalCase.pool == pool)
    total = (await db.execute(qc)).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {
        "total": total,
        "items": [{"id": c.id, "title": c.title, "origin": c.origin, "pool": c.pool,
                   "incident_id": c.incident_id, "host_ident": c.host_ident[:12],
                   "expected": c.expected,
                   "expected_label": (ev_svc.FAULT_EXPECTATIONS.get(c.expected) or {}).get("label", c.expected),
                   "fingerprint": c.fingerprint, "label_status": c.label_status,
                   "created_at": str(c.created_at)} for c in rows]}}


class RunIn(BaseModel):
    pool: str = "regress"
    case_ids: list[int] | None = None


@router.post("/runs")
async def create_run(body: RunIn, request: Request, db=Depends(get_db),
                     user=Depends(require("write"))):
    _admin(user)
    if body.pool not in ("tune", "regress"):
        raise HTTPException(400, "pool 只能是 tune 或 regress")
    # run_eval 内部自管 SessionLocal；请求级 db 只用于审计（实测：把请求 session
    # 传进 runner 会在 aiomysql 上炸 "object list can't be used in await"）。
    out = await ev_svc.run_eval(pool=body.pool, case_ids=body.case_ids)
    await audit(db=db, request=request, user=user, action="eval_run",
                resource=f"pool:{body.pool}", detail={"total": out["summary"]["total"]})
    return {"code": 0, "msg": "ok", "data": out["summary"] | {"details_preview": out["details"][:20]}}


@router.get("/runs")
async def list_runs(page: int = 1, page_size: int = 20,
                    db=Depends(get_db), user=Depends(require("write"))):
    _admin(user)
    total = (await db.execute(select(func.count()).select_from(EvalRun))).scalar() or 0
    rows = (await db.execute(select(EvalRun).order_by(EvalRun.id.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"total": total, "items": [
        {"id": r.id, "fingerprint": r.fingerprint, "pool": r.pool, "total": r.total,
         "match_rate": r.match_rate, "hard_conflict_rate": r.hard_conflict_rate,
         "evidence_rate": r.evidence_rate, "calibration_rate": r.calibration_rate,
         "avg_tokens": r.avg_tokens, "created_at": str(r.created_at)} for r in rows]}}


class CasePatch(BaseModel):
    expected: str | None = None
    expected_note: str | None = None
    pool: str | None = None
    label_status: str | None = None


@router.put("/cases/{cid}")
async def patch_case(cid: int, body: CasePatch, request: Request,
                     db=Depends(get_db), user=Depends(require("write"))):
    _admin(user)
    c = await db.get(EvalCase, cid)
    if c is None:
        raise HTTPException(404, "case 不存在")
    if body.pool is not None:
        if body.pool not in ("tune", "regress"):
            raise HTTPException(400, "pool 只能是 tune 或 regress")
        c.pool = body.pool
    if body.expected is not None:
        c.expected = body.expected[:64]
    if body.expected_note is not None:
        c.expected_note = body.expected_note[:255]
    if body.label_status is not None:
        if body.label_status not in ("pending", "confirmed", "rejected"):
            raise HTTPException(400, "label_status 非法")
        c.label_status = body.label_status
    await db.commit()
    await audit(db=db, request=request, user=user, action="eval_case_label",
                resource=f"eval_case:{cid}",
                detail={"expected": body.expected, "pool": body.pool, "status": body.label_status})
    return {"code": 0, "msg": "ok", "data": {"id": cid}}
