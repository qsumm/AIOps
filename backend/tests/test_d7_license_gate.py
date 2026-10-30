"""D7 集成单测：License 门禁（FR-A4）——四状态、白名单、模块位、宽限期。

设计取舍见 services/license_state.py 顶部注释（为何默认不强制、为何有宽限期）。
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.license import generate_license, machine_fingerprint
from app.db.session import SessionLocal
from app.main import app
from app.models.shell import LicenseRow
from app.services import license_state as lic


@pytest.fixture
def enforce(monkeypatch):
    """打开强制门禁并清缓存（默认配置是关闭的）。"""
    s = get_settings()
    monkeypatch.setattr(s, "license_enforce", True, raising=False)
    lic.invalidate()
    yield
    lic.invalidate()


async def _clear_licenses():
    async with SessionLocal() as db:
        await db.execute(delete(LicenseRow))
        await db.commit()
    lic.invalidate()


@pytest.mark.asyncio
async def test_gate_disabled_by_default(monkeypatch):
    """默认不强制：即使无 License，核心 API 也放行（源码交付开箱即用）。"""
    monkeypatch.setattr(get_settings(), "license_enforce", False, raising=False)
    lic.invalidate()
    st = await lic.license_state(force=True)
    assert st["state"] == "disabled"


@pytest.mark.asyncio
async def test_grace_then_locked(monkeypatch, enforce):
    """无 License：宽限期内 grace；宽限期过 → locked（403 引导激活）。"""
    await _clear_licenses()
    monkeypatch.setattr(get_settings(), "license_grace_days", 14, raising=False)
    st = await lic.license_state(force=True)
    assert st["state"] == "grace", st

    # 把宽限期调成全 0 天 → 立刻 locked（模拟已过宽限期）
    monkeypatch.setattr(get_settings(), "license_grace_days", 0, raising=False)
    lic.invalidate()
    st = await lic.license_state(force=True)
    assert st["state"] == "locked", st

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        # 业务 API 被锁
        r = await ac.get("/api/ops/hosts")
        assert r.status_code == 403 and "LICENSE_LOCKED" in r.json()["msg"], r.text
        # 白名单永不放行失败：健康与登录仍可用（否则无法激活）
        assert (await ac.get("/api/health")).status_code == 200
        assert (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).status_code == 200
        tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        assert (await ac.get("/api/license/state", headers={"Authorization": f"Bearer {tok}"})).status_code == 200
    await _clear_licenses()


@pytest.mark.asyncio
async def test_valid_license_unlocks(monkeypatch, enforce):
    """有效 License → valid，业务 API 放行。"""
    await _clear_licenses()
    key = generate_license(machine_fingerprint(), days=30)
    async with SessionLocal() as db:
        db.add(LicenseRow(license_key=key, machine_fingerprint=machine_fingerprint(),
                          expires_at=datetime.utcnow() + timedelta(days=30), modules_json={"modules": ["ops"]}))
        await db.commit()
    lic.invalidate()
    st = await lic.license_state(force=True)
    assert st["state"] == "valid" and st["modules"] == ["ops"], st
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        assert (await ac.get("/api/ops/hosts", headers={"Authorization": f"Bearer {tok}"})).status_code == 200
    await _clear_licenses()


@pytest.mark.asyncio
async def test_expired_and_mismatch_lock(monkeypatch, enforce):
    """过期 / 换机 → locked（对应 EXPIRED / MACHINE_MISMATCH 两个错误码）。"""
    await _clear_licenses()
    for key, label in ((generate_license(machine_fingerprint(), days=-1), "过期"),
                       (generate_license("another-machine-fp", days=30), "换机")):
        async with SessionLocal() as db:
            db.add(LicenseRow(license_key=key, machine_fingerprint=machine_fingerprint(),
                              expires_at=datetime.utcnow() + timedelta(days=30), modules_json={"modules": ["ops"]}))
            await db.commit()
        lic.invalidate()
        st = await lic.license_state(force=True)
        assert st["state"] == "locked", f"{label} 应锁定，实际 {st}"
        await _clear_licenses()


@pytest.mark.asyncio
async def test_module_bit_missing_locks(monkeypatch, enforce):
    """模块位不含 ops → locked（导航与 API 同时不可用，FR-A4）。"""
    await _clear_licenses()
    s = get_settings()
    fp = machine_fingerprint()
    payload = json.dumps({"fingerprint": fp,
                          "expires_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
                          "modules": ["data"],
                          "issued_at": datetime.now(timezone.utc).isoformat()},
                         separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(s.jwt_secret.encode(), payload, hashlib.sha256).digest()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    key = f"{b64(payload)}.{b64(sig)}"
    async with SessionLocal() as db:
        db.add(LicenseRow(license_key=key, machine_fingerprint=fp,
                          expires_at=datetime.utcnow() + timedelta(days=10), modules_json={"modules": ["data"]}))
        await db.commit()
    lic.invalidate()
    st = await lic.license_state(force=True)
    assert st["state"] == "locked" and "ops" in st["reason"], st
    await _clear_licenses()


def test_allow_path_whitelist():
    """白名单：登录/健康/License/静态资源免门禁；业务路径不免。"""
    for p in ("/api/auth/login", "/api/health", "/api/license/state", "/api/sse-ticket",
              "/js/api.js", "/vendor/echarts.min.js", "/ops.css", "/"):
        assert lic.allow_path(p), f"{p} 应在白名单"
    for p in ("/api/ops/hosts", "/api/ops/incidents", "/api/llm-configs", "/api/audit-logs"):
        assert not lic.allow_path(p), f"{p} 不应在门禁白名单"
