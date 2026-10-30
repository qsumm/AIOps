"""D1 单测：认证流 / License 四错误码 / 角色权限矩阵（06 §2 M1 用例 UT 部分）。"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.license import generate_license, machine_fingerprint, verify_license
from app.db.session import SessionLocal
from app.main import app
from app.models.shell import User


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_login_and_me():
    async with client() as ac:
        r = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        tok = r.json()["data"]["access_token"]
        me = await ac.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert me.json()["data"]["role"] == "admin"
        bad = await ac.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert bad.status_code == 401


@pytest.mark.asyncio
async def test_role_matrix():
    async with client() as ac:
        _, demo_tok = await _login(ac, "demo", "demo")
        # demo 只读：GET hosts 放行
        ok = await ac.get("/api/ops/hosts", headers={"Authorization": f"Bearer {demo_tok}"})
        assert ok.status_code == 200
        # demo 写操作被拒（403）
        denied = await ac.post("/api/llm-configs", headers={"Authorization": f"Bearer {demo_tok}"},
                               json={"name": "x", "model": "gpt"})
        assert denied.status_code == 403


async def _login(ac, u, p):
    r = await ac.post("/api/auth/login", json={"username": u, "password": p})
    return r.status_code, r.json()["data"]["access_token"]


def test_license_four_errors():
    fp = machine_fingerprint()
    good = generate_license(fp, days=30)
    assert verify_license(good, fp)["modules"] == ["ops"]
    # 坏签名
    with pytest.raises(Exception):
        verify_license(good[:-3] + "aaa", fp)
    # 换机
    try:
        verify_license(good, "other-machine-fp")
        assert False, "应抛 MACHINE_MISMATCH"
    except Exception as e:
        assert "MACHINE_MISMATCH" in str(e) or "MISMATCH" in str(e.detail if hasattr(e, 'detail') else e)
    # 过期
    expired = generate_license(fp, days=-1)
    try:
        verify_license(expired, fp)
        assert False
    except Exception as e:
        assert "EXPIRED" in str(getattr(e, "detail", e))
    # 时钟回拨（签发时间在未来）
    import base64, hashlib, hmac, json as J
    from datetime import datetime, timedelta, timezone
    from app.core.config import get_settings
    payload = J.dumps({"fingerprint": fp,
                       "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
                       "modules": ["ops"],
                       "issued_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
                      separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(get_settings().jwt_secret.encode(), payload, hashlib.sha256).digest()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    future_key = f"{b64(payload)}.{b64(sig)}"
    try:
        verify_license(future_key, fp)
        assert False
    except Exception as e:
        assert "CLOCK_ROLLBACK" in str(getattr(e, "detail", e))


@pytest.mark.asyncio
async def test_agent_register_and_ingest():
    """Agent 协议：注册→上报→查询（真实 SeekDB + 内存环形）。"""
    from app.services import ingest

    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(db, {"fingerprint": "test-fp-001", "hostname": "t1", "os": "t", "install_token": tok})
        assert data["token"]
        # 重复指纹幂等（同 ident 新 token）
        data2 = await ingest.register_agent(db, {"fingerprint": "test-fp-001", "hostname": "t1"})
        assert data2["ident"] == data["ident"]
        await ingest.ingest_metrics(db, data["ident"], data2["token"], {
            "ts": "2026-10-13T10:00:00+00:00",
            "metrics": [{"metric": "cpu.percent", "value": 55.5}],
            "probes": [{"name": "p1", "type": "tcp", "target": "x:80", "status": "down"}],
        })
    from app.store import ring

    from datetime import datetime
    pts = ring.query_points(data["ident"], "cpu.percent", datetime(2026, 10, 13, 9, 0), datetime(2026, 10, 13, 11, 0))
    assert pts and pts[0][1] == 55.5
    # 清理
    from app.models.host import Host

    async with SessionLocal() as db:
        h = (await db.execute(select(Host).where(Host.ident == data["ident"]))).scalar_one()
        await db.delete(h)
        await db.commit()
