"""认证与授权：JWT 签发/校验、bcrypt、角色依赖（FR-A1；03 §1.1）。

权限矩阵（唯一权威）：admin 全权；member 读 + 诊断/巡检触发 + 告警操作；
demo 只读（AI 助手只读问答默认开放，可用 DEMO_CHAT_ENABLED=false 关闭）。

**级别划分依据**（测试阶段修正：此前 member 的"告警操作"被误挡，与 03 权限矩阵不符）：
  read       只读查询
  diag       诊断 / 巡检触发 —— 只读 + 调 LLM，不改业务状态
  alert_ops  告警与事件操作（认领 / 关闭 / 合并 / 拆分 / 屏蔽）—— 运维日常动作，member 应具备
  write      配置与执行类（规则 / 渠道 / 处置执行 / LLM / License / 技能 / Prom / 巡检任务）—— admin 专属

前端的按钮可见性**必须**取自 `GET /api/auth/perms` 的 `can` 映射，不要在页面里按 role 硬编码
（测试阶段实测：硬编码导致演示模式点「AI 助手」→ 403，见 03 §权限矩阵）。
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.db.session import get_db
from app.models.shell import User

settings = get_settings()
_bearer = HTTPBearer(auto_error=False)

# 权限矩阵：级别 → 允许角色
PERM_MATRIX: dict[str, set[str]] = {
    "read": {"admin", "member", "demo"},          # GET
    "diag": {"admin", "member"},                  # 诊断 / 巡检触发 / ChatOps
    "alert_ops": {"admin", "member"},             # 告警与事件操作（认领/关闭/合并/拆分/屏蔽）
    "write": {"admin"},                           # 配置与执行类
}


def capabilities(user: User) -> dict[str, bool]:
    """当前用户的能力集（前端按钮可见性的唯一来源）。"""
    role = user.role
    chat = role in PERM_MATRIX["diag"] or (role == "demo" and bool(settings.demo_chat_enabled))
    return {level: role in allowed for level, allowed in PERM_MATRIX.items()} | {"chat": chat}


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(user_id: int, role: str, expires_hours: int | None = None) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=expires_hours or settings.jwt_expire_hours)
    payload = {"sub": str(user_id), "role": role, "exp": exp}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db=Depends(get_db),
) -> User:
    if cred is None:
        raise HTTPException(401, "未认证")
    try:
        payload = jwt.decode(cred.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "令牌无效或已过期")
    user = await db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(401, "用户不存在")
    return user


def require(level: str):
    """角色门禁：require("read"|"diag"|"alert_ops"|"write")。"""

    async def _dep(
        request: Request,
        user: User = Depends(get_current_user),
    ) -> User:
        allowed = PERM_MATRIX[level]
        if user.role not in allowed:
            # 越权拒绝必须留痕（FR-A3）：拒绝路径用独立短会话落库，失败不影响主流程
            await audit(db=None, request=request, user=user, action="perm_denied",
                        resource=request.url.path, detail={"required": level, "role": user.role})
            raise HTTPException(403, f"当前角色（{user.role}）无权执行该操作，需要 {level} 权限")
        return user

    # 打标记供**接口文档生成**读取（tools/dump_route_perms.py）：
    # 「每个端点需要什么权限」只允许有一个来源——就是这份代码；文档由它机械生成，不会过期。
    _dep._cap = level  # type: ignore[attr-defined]
    return _dep


def require_chat():
    """ChatOps 门禁：admin/member 恒可用；demo 由 DEMO_CHAT_ENABLED 决定（只读问答）。"""

    async def _dep(request: Request, user: User = Depends(get_current_user)) -> User:
        if user.role in PERM_MATRIX["diag"]:
            return user
        if user.role == "demo" and settings.demo_chat_enabled:
            return user
        await audit(db=None, request=request, user=user, action="perm_denied",
                    resource=request.url.path, detail={"required": "chat", "role": user.role})
        raise HTTPException(
            403,
            "演示模式的 AI 助手已关闭（管理员可置 DEMO_CHAT_ENABLED=true 开放只读问答）"
            if user.role == "demo" else f"当前角色（{user.role}）无权执行该操作，需要 chat 权限",
        )

    _dep._cap = "chat"  # type: ignore[attr-defined]
    return _dep


async def audit(*, db, request: Request | None, user: User | None, action: str, resource: str = "", detail: dict | None = None) -> None:
    """审计埋点（FR-A3）。db 为 None 时自建短会话落库（拒绝路径也必须有痕）。

    自建会话失败一律吞掉：审计不可用不能反过来把请求打成 500。
    """
    import logging

    from app.models.shell import AuditLog

    row = AuditLog(
        user_id=user.id if user else None,
        action=action,
        resource=resource,
        detail=detail or {},
        ip=(request.client.host if request and request.client else "") if request else "",
    )
    if db is not None:
        db.add(row)
        await db.commit()
        return
    try:
        from app.db.session import SessionLocal

        async with SessionLocal() as s:
            s.add(row)
            await s.commit()
    except Exception:  # noqa: BLE001
        logging.getLogger("security").warning("审计落库失败 action=%s resource=%s", action, resource, exc_info=True)
