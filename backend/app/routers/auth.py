"""认证路由（FR-A1；05 §1）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import (
    audit,
    capabilities,
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.db.session import get_db
from app.models.shell import User

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, db=Depends(get_db)):
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        # D181：登录失败也必须有痕（爆破尝试的唯一定位证据）；审计不可用不影响 401。
        await audit(db=db, request=request, user=None, action="login_failed",
                    resource="-", detail={"username": body.username})
        raise HTTPException(401, "用户名或密码错误")
    user.last_login = datetime.utcnow()
    await db.commit()
    # D181：审计覆盖「登录」此前只有文案没有实现——现在成功登录落 login（含来源 IP）。
    await audit(db=db, request=request, user=user, action="login",
                resource=f"user:{user.id}", detail={"username": user.username})
    return {"code": 0, "msg": "ok", "data": {
        "access_token": create_access_token(user.id, user.role),
        "role": user.role,
        "preferred_lang": user.preferred_lang,   # D97：UI 中英文切换
    }}


@router.post("/demo")
async def demo_login(request: Request, db=Depends(get_db)):
    if not settings.demo_enabled:
        raise HTTPException(403, "演示模式未开放")
    user = (await db.execute(select(User).where(User.role == "demo"))).scalar_one_or_none()
    if user is None:
        raise HTTPException(403, "演示用户未初始化")
    await audit(db=db, request=request, user=user, action="login",
                resource=f"user:{user.id}", detail={"username": user.username})
    return {"code": 0, "msg": "ok", "data": {
        "access_token": create_access_token(user.id, "demo"), "role": "demo", "username": user.username,
        "preferred_lang": user.preferred_lang}}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"code": 0, "msg": "ok", "data": {
        "username": user.username, "role": user.role, "last_login": str(user.last_login or ""),
        "preferred_lang": user.preferred_lang,   # D97
    }}


class LangIn(BaseModel):
    preferred_lang: str   # "zh" | "en"


@router.patch("/me/lang")
async def set_my_lang(body: LangIn, db=Depends(get_db), user: User = Depends(get_current_user)):
    """D97：右上角"中/EN"切换调用。写库保证刷新仍生效；不要求 write 权限（用户改自己）。"""
    v = (body.preferred_lang or "").strip().lower()
    if v not in ("zh", "en"):
        raise HTTPException(400, "preferred_lang 只能是 zh 或 en")
    user.preferred_lang = v
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"preferred_lang": v}}


@router.get("/perms")
async def perms(user: User = Depends(get_current_user)):
    """当前用户能力集——前端按钮可见性的**唯一来源**（D8：不再在前端按 role 硬编码）。"""
    return {"code": 0, "msg": "ok", "data": {
        "username": user.username, "role": user.role, "can": capabilities(user)}}


# SSE 换票（05 §8 / 03 §1.22）：路径为 /api/sse-ticket，故单独一个不带 /auth 前缀的路由
api_router = APIRouter(prefix="/api", tags=["sse"])


@api_router.get("/sse-ticket")
async def sse_ticket(user: User = Depends(get_current_user)):
    from app.services import sse_ticket as tickets

    return {"code": 0, "msg": "ok", "data": {"ticket": tickets.mint(user.id, user.role)}}


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/password")
async def change_password(body: PasswordIn, request: Request = None, db=Depends(get_db), user: User = Depends(get_current_user)):
    """自助改密（D18 补：手册 §1 承诺「首次登录后请立即修改」，但平台原先**没有改密能力**——
    买家部署到公网却改不掉默认口令 admin/admin123，属安全缺口）。

    规则：校验原密码 → 新密码 ≥8 位且不同于原密码 → bcrypt 落库 → 写审计。
    """
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(400, "原密码不正确")
    if len(body.new_password or "") < 8:
        raise HTTPException(400, "新密码至少 8 位")
    if body.new_password == body.old_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    await audit(db=db, request=request, user=user, action="password_change", resource=f"user:{user.id}")
    return {"code": 0, "msg": "ok", "data": {"username": user.username}}
