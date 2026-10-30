"""License 门禁状态（FR-A4；03 §1.2）。

语义：
  disabled  —— 未开启强制（源码交付默认，买家可即刻运行；发布时置 LICENSE_ENFORCE=true）
  valid     —— 有有效 License（签名/指纹/有效期/签发时间四项全过）且模块位包含 ops
  grace     —— 无 License，但仍在首次启动的宽限期内（默认 14 天）
  locked    —— 无 License 且宽限期已过，或 License 无效（四错误码之一），或模块位不含 ops

设计取舍（写进文档，避免"为什么不是一装就锁"的疑问）：
  1. 强制开关默认关闭：这是**源码商品**，买家拿到代码即可运行；要管控分发时再开。
  2. 有宽限期：否则新装实例一登录就被自己的产品锁死，无法进设置页激活——那等于不可用。
  3. 每次校验缓存 5 分钟（03 §1.2），避免每请求都做 HMAC 校验。
  4. 白名单路径永不放行失败：login/health/license/静态资源——否则激活入口本身都被锁住。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.core.license import machine_fingerprint, verify_license
from app.db.session import SessionLocal
from app.models.shell import LicenseRow

log = logging.getLogger("license")

_CACHE: dict[str, object] = {"at": 0.0, "state": None}
CACHE_TTL_S = 300

# 免门禁路径（前缀匹配）：登录、健康检查、License 自身、静态资源
ALLOW_PREFIXES = (
    "/api/auth/", "/api/health", "/api/license", "/api/sse-ticket",
    "/js/", "/vendor/", "/ops.css", "/favicon", "/index.html",
)
ALLOW_EXACT = ("/", "/install.sh", "/agent_main.py")   # 安装件与首页始终可取（否则无法接入主机）


def allow_path(path: str) -> bool:
    return path in ALLOW_EXACT or any(path.startswith(p) for p in ALLOW_PREFIXES)


async def license_state(force: bool = False) -> dict:
    """返回 {state, reason, modules, expires_at, grace_days_left, enforce}。"""
    s = get_settings()
    if not s.license_enforce:
        return {"state": "disabled", "reason": "未开启强制（LICENSE_ENFORCE=false）", "modules": ["ops"],
                "expires_at": "", "grace_days_left": None, "enforce": False}
    now = time.time()
    if not force and _CACHE["state"] and now - float(_CACHE["at"]) < CACHE_TTL_S:
        return dict(_CACHE["state"])  # type: ignore[arg-type]

    async with SessionLocal() as db:
        row = (await db.execute(select(LicenseRow).order_by(LicenseRow.id.desc()))).scalar_one_or_none()
        first_row = (await db.execute(select(LicenseRow).order_by(LicenseRow.id))).scalars().first()

    state: dict
    if row is None or not row.license_key:
        # 无 License：看首次启动（库中最早一条记录）是否在宽限期内
        installed = first_row.activated_at if (first_row and first_row.activated_at) else None
        left = None
        if installed is None and first_row is None:
            left = s.license_grace_days          # 全新库：宽限期从此刻起算（不落库，按天展示）
        elif installed is not None:
            left = s.license_grace_days - (datetime.utcnow() - installed).days
        if left is not None and left > 0:
            state = {"state": "grace", "reason": f"未激活，宽限期剩余 {left} 天", "modules": ["ops"],
                     "expires_at": "", "grace_days_left": left}
        else:
            state = {"state": "locked", "reason": "无有效 License，且宽限期已过——请在系统设置页激活",
                     "modules": [], "expires_at": "", "grace_days_left": left}
    else:
        fp = machine_fingerprint()
        try:
            info = verify_license(row.license_key, fp)
            modules = info.get("modules") or []
            if "ops" not in modules:
                state = {"state": "locked", "reason": f"License 模块位不含 ops（当前：{modules}）",
                         "modules": modules, "expires_at": info.get("expires_at", ""), "grace_days_left": None}
            else:
                state = {"state": "valid", "reason": "", "modules": modules,
                         "expires_at": info.get("expires_at", ""), "grace_days_left": None}
        except Exception as e:  # noqa: BLE001 —— 四错误码统一为 locked
            detail = str(getattr(e, "detail", e))
            state = {"state": "locked", "reason": f"License 校验失败：{detail}",
                     "modules": [], "expires_at": "", "grace_days_left": None}

    state["enforce"] = True
    _CACHE["at"], _CACHE["state"] = now, dict(state)
    return state


def invalidate() -> None:
    _CACHE["at"] = 0.0
    _CACHE["state"] = None
