"""LLM 配置路由（FR-A2；05 §1）+ License 路由（FR-A4）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update

from app.core.license import generate_license, machine_fingerprint, verify_license
from app.core.security import require
from app.db.session import get_db
from app.models.llm import LLMConfig
from app.models.shell import LicenseRow, User
from app.services import llm_gateway

router = APIRouter(prefix="/api", tags=["shell"])


# ---------- LLM 配置 ----------
class LLMConfigIn(BaseModel):
    name: str
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str
    fast_model: str = ""
    extra_config: dict = {}
    enabled: bool = True
    is_default: bool = False
    # D163-3：降级通道由用户显式指定（主模型额度/限流不可用时改用这套配置）
    fallback_enabled: bool = False


@router.get("/llm-configs")
async def list_configs(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(LLMConfig).order_by(LLMConfig.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": [
        {"id": r.id, "name": r.name, "provider": r.provider, "base_url": r.base_url,
         "model": r.model, "fast_model": r.fast_model, "enabled": r.enabled, "is_default": r.is_default,
         "fallback_enabled": r.fallback_enabled,
         "key_tail": (r.api_key_encrypted and "…") or ""} for r in rows
    ]}


async def _validate_llm_in(body: LLMConfigIn, db, exclude_id: int | None = None) -> None:
    """LLM 配置入参校验（D59）：name/model 是 unique/nullable=False 列，
    空值或重名原先直接撞 DB 变 500；base_url 填了但不是 http(s) 会在调用期才炸。"""
    if not (body.name or "").strip():
        raise HTTPException(400, "配置名称为必填项（1~64 字符）")
    if len(body.name.strip()) > 64:
        raise HTTPException(400, "配置名称过长（≤64 字符）")
    dup = (await db.execute(select(LLMConfig).where(LLMConfig.name == body.name.strip()))).scalar_one_or_none()
    if dup and dup.id != exclude_id:
        raise HTTPException(409, f"配置名称「{body.name.strip()}」已存在，请换一个名字")  # 409 = 全局重名口径
    if not (body.model or "").strip():
        raise HTTPException(400, "主模型为必填项（如 MiniMax-M3）")
    if len(body.model.strip()) > 64:
        raise HTTPException(400, "主模型名过长（≤64 字符）")
    if len(body.fast_model or "") > 64:
        raise HTTPException(400, "快模型名过长（≤64 字符）")
    if (body.base_url or "").strip() and not body.base_url.strip().lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Base URL 需以 http:// 或 https:// 开头（留空 = 用官方默认地址）")
    if len(body.base_url or "") > 255:
        raise HTTPException(400, "Base URL 过长（≤255 字符）")


@router.post("/llm-configs")
async def create_config(body: LLMConfigIn, db=Depends(get_db), user: User = Depends(require("write"))):
    await _validate_llm_in(body, db)
    # D147：**默认只能有一套**。此前允许多套 is_default=True，而 pick_config 取
    # "第一个 is_default"（按主键顺序）→ 用户把新供应商设为默认后**仍然调用旧供应商**
    # （公网实测：新建通义千问设默认，测试却打到已欠费的 MiniMax，报「额度用完」）。
    if body.is_default:
        await db.execute(update(LLMConfig).where(LLMConfig.is_default.is_(True)).values(is_default=False))
    cfg = LLMConfig(
        name=body.name.strip(), provider=body.provider, base_url=body.base_url.strip(),
        api_key_encrypted=llm_gateway.encrypt_secret(body.api_key),
        model=body.model, fast_model=body.fast_model,
        extra_config=body.extra_config, enabled=body.enabled, is_default=body.is_default,
        fallback_enabled=body.fallback_enabled,
    )
    db.add(cfg)
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"id": cfg.id}}


@router.put("/llm-configs/{cfg_id}")
async def update_config(cfg_id: int, body: LLMConfigIn, db=Depends(get_db), _: User = Depends(require("write"))):
    cfg = await db.get(LLMConfig, cfg_id)
    if cfg is None:
        raise HTTPException(404, "NOT_FOUND")
    await _validate_llm_in(body, db, exclude_id=cfg_id)
    if body.is_default:
        await db.execute(update(LLMConfig).where(
            LLMConfig.is_default.is_(True), LLMConfig.id != cfg_id).values(is_default=False))
    for k in ("name", "provider", "base_url", "model", "fast_model", "extra_config", "enabled",
              "is_default", "fallback_enabled"):
        setattr(cfg, k, getattr(body, k))
    cfg.name, cfg.base_url = cfg.name.strip(), cfg.base_url.strip()
    if body.api_key:
        cfg.api_key_encrypted = llm_gateway.encrypt_secret(body.api_key)
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"id": cfg.id}}


@router.delete("/llm-configs/{cfg_id}")
async def delete_config(cfg_id: int, db=Depends(get_db), _: User = Depends(require("write"))):
    """删除一套模型配置（D147：此前只有增/改，MiniMax 换通义后旧配置**删不掉**）。

    安全性：删的是默认配置时，自动把**剩余第一套启用配置**提升为默认——
    否则删完会没有默认模型，`pick_config` 只能回退 .env，用户会以为"AI 突然坏了"。
    """
    cfg = await db.get(LLMConfig, cfg_id)
    if cfg is None:
        raise HTTPException(404, "NOT_FOUND")
    was_default = bool(cfg.is_default)
    await db.delete(cfg)
    await db.flush()
    promoted = None
    if was_default:
        nxt = (await db.execute(
            select(LLMConfig).where(LLMConfig.enabled.is_(True)).order_by(LLMConfig.id))).scalars().first()
        if nxt is not None:
            nxt.is_default = True
            promoted = nxt.id
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"id": cfg_id, "promoted_default": promoted}}


@router.post("/llm-configs/{cfg_id}/test")
async def test_config(cfg_id: int, db=Depends(get_db), _: User = Depends(require("write"))):
    cfg = await db.get(LLMConfig, cfg_id)
    if cfg is None:
        raise HTTPException(404, "NOT_FOUND")
    try:
        # D184 修：测试必须用**被测的这条配置**（_call_llm_with_config 直连），
        # 不能走 model_role 选择——否则测的是默认配置，被测配置坏了也显示通过（实测踩过）。
        res = await llm_gateway._call_llm_with_config(
            db, "summary", [{"role": "user", "content": "回复 pong"}], None, cfg, timeout_s=30,
            quota_role="")
        return {"code": 0, "msg": "ok", "data": {"ok": True, "latency_ms": res.usage["latency_ms"], "reply": res.content[:50]}}
    except HTTPException as e:
        return {"code": 0, "msg": "ok", "data": {"ok": False, "detail": e.detail}}


@router.get("/llm-configs/usage")
async def get_usage(days: int = 7, db=Depends(get_db), user: User = Depends(require("read"))):
    """按模块分账的用量 + **当日配额状态**（前端据此提示"今日额度将尽/已用尽"）。"""
    return {"code": 0, "msg": "ok", "data": {
        "items": await llm_gateway.usage_summary(db, days),
        "quota": await llm_gateway.quota_state(db, user.role),
    }}


# ---------- License ----------
class LicenseIn(BaseModel):
    license_key: str


@router.post("/license/activate")
async def activate(body: LicenseIn, db=Depends(get_db), user: User = Depends(require("write"))):
    if not (body.license_key or "").strip():
        raise HTTPException(400, "请输入激活码（License 串）")
    data = verify_license(body.license_key.strip(), machine_fingerprint())
    row = LicenseRow(
        license_key=body.license_key.strip(), machine_fingerprint=data["fingerprint"],
        expires_at=datetime.fromisoformat(data["expires_at"]), modules_json={"modules": data["modules"]},
    )
    db.add(row)
    await db.commit()
    from app.services import license_state as lic

    lic.invalidate()   # 激活即刻生效，不用等 5 分钟缓存过期
    return {"code": 0, "msg": "ok", "data": {"expires_at": data["expires_at"], "modules": data["modules"]}}


@router.get("/license/state")
async def license_gate_state(_: User = Depends(require("read"))):
    """门禁状态（前端据此显示锁定横幅与按模块位渲染导航，FR-A4）。"""
    from app.services import license_state as lic

    return {"code": 0, "msg": "ok", "data": await lic.license_state(force=True)}


@router.get("/license")
async def current_license(db=Depends(get_db), _: User = Depends(require("read"))):
    row = (await db.execute(select(LicenseRow).order_by(LicenseRow.id.desc()))).scalars().first()
    if row is None:
        return {"code": 0, "msg": "ok", "data": None}
    return {"code": 0, "msg": "ok", "data": {
        "expires_at": str(row.expires_at), "modules": (row.modules_json or {}).get("modules", []),
    }}


def _register_dev_routes() -> None:
    """D163-9：dev 辅助路由**仅 debug 模式注册**——生产 OpenAPI 不再暴露签发入口。

    原实现路由永远注册、靠运行时 403 拦（有双防护无实际风险），但交付买家
    在 /openapi.json 里能看到"签发激活码"的入口，观感与攻击面都不该有。
    """
    from app.core.config import get_settings

    if not get_settings().debug:
        return

    @router.get("/dev/mint-license")  # 开发辅助：签发测试激活码（仅 debug）
    async def mint(days: int = 365, _: User = Depends(require("write"))):
        if not settings_debug():
            raise HTTPException(403, "仅 debug 模式")
        return {"code": 0, "msg": "ok", "data": {"license_key": generate_license(machine_fingerprint(), days)}}


_register_dev_routes()


def settings_debug() -> bool:
    from app.core.config import get_settings
    return get_settings().debug
