"""D181 登录入审计 + 审计日期过滤时区修正的回归锁。

缺陷（台账 v1.91，用户实测）：
  ① 审计页头写「审计覆盖：登录」，实现里根本没有 login 动作——文案与实现不符；
  ② 日期过滤 `datetime.fromisoformat(date)` 按裸日期切 UTC 窗口：用户按北京时间选
     「10 月 24 日」，实际查的是北京 10-24 08:00 起的窗口（错位 8 小时）。
修法：登录成功/失败均入审计（login / login_failed）；日期过滤按北京时间日切
（UTC 窗口 = 当天 00:00+08 → 次日 00:00+08）。
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.shell import AuditLog, User

PFX = "d181"


async def _client_as_admin():
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "admin"))).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: u
    return app


async def _cleanup():
    async with SessionLocal() as db:
        await db.execute(delete(AuditLog).where(AuditLog.action.in_(["login", "login_failed"])))
        await db.commit()


@pytest.mark.asyncio
async def test_login_success_and_failure_are_audited():
    """登录成功/失败都必须落审计（D181 前两者皆无）。"""
    await _cleanup()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r1 = await ac.post("/api/auth/login",
                           json={"username": "admin", "password": "admin123"})
        assert r1.status_code == 200
        r2 = await ac.post("/api/auth/login",
                           json={"username": "admin", "password": "wrong-password"})
        assert r2.status_code == 401
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(AuditLog).where(AuditLog.action.in_(["login", "login_failed"]))
            .order_by(AuditLog.id.desc()))).scalars().all()
        actions = [r.action for r in rows]
        assert "login" in actions and "login_failed" in actions, f"审计缺登录记录：{actions}"
        ok = next(r for r in rows if r.action == "login")
        bad = next(r for r in rows if r.action == "login_failed")
        assert (ok.detail or {}).get("username") == "admin"
        assert (bad.detail or {}).get("username") == "admin"
    await _cleanup()


@pytest.mark.asyncio
async def test_audit_date_filter_is_beijing_day():
    """日期过滤按北京时间日切：UTC 10-23 18:00（=北京 10-24 02:00）应落在「10-24」。"""
    async with SessionLocal() as db:
        await db.execute(delete(AuditLog).where(AuditLog.action == "d181-tz-probe"))
        db.add(AuditLog(user_id=None, action="d181-tz-probe", resource="-", detail={},
                        created_at=datetime(2026, 10, 23, 18, 0, 0),
                        ip="203.0.113.10"))
        await db.commit()
    app = await _client_as_admin()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r24 = await ac.get("/api/audit-logs", params={"action": "d181-tz-probe", "date": "2026-10-24"})
            r23 = await ac.get("/api/audit-logs", params={"action": "d181-tz-probe", "date": "2026-10-23"})
        assert r24.status_code == 200 and r23.status_code == 200
        n24 = len(r24.json()["data"]["items"])
        n23 = len(r23.json()["data"]["items"])
        assert n24 == 1 and n23 == 0, \
            f"北京日切错误：10-24 查到 {n24} 条（应 1）、10-23 查到 {n23} 条（应 0）"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(AuditLog).where(AuditLog.action == "d181-tz-probe"))
            await db.commit()
