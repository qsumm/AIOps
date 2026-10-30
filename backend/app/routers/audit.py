"""审计路由（FR-A3；05 §1 GET /api/audit-logs）。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.security import require
from app.db.session import get_db
from app.models.shell import AuditLog, User

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit-logs")
async def list_audit(
    action: str = "",
    actor: str = "",
    date: str = "",
    user_id: int = 0,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db=Depends(get_db),
    user: User = Depends(require("read")),
):
    """审计检索（FR-A3）：动作精确、操作者按用户名、日期按天过滤；demo 角色 IP 脱敏。"""
    q = select(AuditLog).order_by(AuditLog.id.desc())
    qc = select(func.count()).select_from(AuditLog)
    if action:
        q, qc = q.where(AuditLog.action == action), qc.where(AuditLog.action == action)
    if user_id:
        q, qc = q.where(AuditLog.user_id == user_id), qc.where(AuditLog.user_id == user_id)
    if actor:
        uids = [u.id for u in (await db.execute(select(User).where(User.username.like(f"%{actor}%")))).scalars().all()]
        q, qc = q.where(AuditLog.user_id.in_(uids or [-1])), qc.where(AuditLog.user_id.in_(uids or [-1]))
    if date:
        from datetime import datetime, timedelta

        try:
            # D181：用户按**北京时间**选日子，列存 UTC——窗口 = 北京当天 00:00+08 → 次日 00:00+08，
            # 即 UTC [d0-8h, d0+1day-8h)。此前裸日期切 UTC 日窗，错位 8 小时。
            d0_utc = datetime.fromisoformat(date) - timedelta(hours=8)
            q, qc = q.where(AuditLog.created_at >= d0_utc, AuditLog.created_at < d0_utc + timedelta(days=1)), \
                qc.where(AuditLog.created_at >= d0_utc, AuditLog.created_at < d0_utc + timedelta(days=1))
        except ValueError:
            pass
    total = (await db.execute(qc)).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    names = {u.id: u.username for u in (await db.execute(select(User))).scalars().all()}
    # 筛选下拉的可选项（D96）：从审计表里取**真实存在**的操作者与动作码——
    # 用户不应该对着"如 alert_ack"这种占位符猜代码。操作者按 user_id 去重再映射用户名。
    op_ids = (await db.execute(select(AuditLog.user_id).distinct().limit(200))).scalars().all()
    actions = (await db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action).limit(100))).scalars().all()
    redact = user.role == "demo"
    return {"code": 0, "msg": "ok", "data": {
        "items": [{"id": r.id, "user_id": r.user_id, "username": names.get(r.user_id, ""),
                   "action": r.action, "resource": r.resource,
                   "detail": {} if redact else (r.detail or {}),
                   "ip": "***" if redact else r.ip, "created_at": str(r.created_at)} for r in rows],
        "total": total,
        "facets": {"operators": sorted({names.get(i, "") for i in op_ids} - {""}),
                   "actions": list(actions)},
    }}
