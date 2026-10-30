"""D18：把用例库里"标了待补/未执行"的用例补成自动化。

来源：用例库自检发现 144 条已执行、5~8 条未执行（含 1 个 P0）。
本文件把其中**可自动化**的全部补上；剩余不可自动化的（真机长跑/日志人工抽查）
在用例库里明确标注原因。
"""
import asyncio
import hashlib
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select, text

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.alert import NotificationChannel
from app.models.host import Host
from app.models.shell import User


async def _client_as(username: str = "admin"):
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == username))).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: u
    return app


# ── TC-ALERT-24（P0）：渠道 webhook 凭据必须掩码返回 ──
@pytest.mark.asyncio
async def test_channel_webhook_is_masked():
    """渠道接口不得明文返回 webhook_url/apprise_url（飞书/钉钉 webhook 即凭据）。

    修复到位但**当时没写测试**（用例库自检发现的漏项）——补上，防回退到明文。
    """
    app = await _client_as()
    async with SessionLocal() as db:
        await db.execute(delete(NotificationChannel).where(NotificationChannel.name == "d18-mask"))
        await db.commit()
        ch = NotificationChannel(name="d18-mask", type="feishu", enabled=True,
                                 config_json={"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef1234567890"})
        db.add(ch)
        await db.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/api/ops/notify-channels")
            assert r.status_code == 200
            item = next(c for c in r.json()["data"]["items"] if c["name"] == "d18-mask")
            url = (item.get("config_json") or {}).get("webhook_url", "")
            assert "abcdef1234567890" not in url, f"webhook 明文泄露：{url}"
            assert "…" in url or "***" in url, f"应掩码显示，实际 {url!r}"
            assert "open.feishu.cn" in url, "应保留可辨认前缀"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(NotificationChannel).where(NotificationChannel.name == "d18-mask"))
            await db.commit()
    app.dependency_overrides.clear()


# ── TC-AUTH-11：审计检索（动作/操作者/日期） ──
@pytest.mark.asyncio
async def test_audit_log_filters_work():
    app = await _client_as()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        # 产生一条可检索的审计（建规则 → 审计 action=rule_create）
        r = await ac.post("/api/ops/alert-rules", json={
            "name": "d18-audit-rule", "scope": "metric", "metric": "cpu.percent",
            "expr": ">1", "severity": 2})
        assert r.status_code == 200
        # 按动作筛选
        r = await ac.get("/api/audit-logs?action=rule_create&limit=10")
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        assert any(i["action"] == "rule_create" for i in items), f"按动作筛选无结果：{items[:2]}"
        assert all(i["action"] == "rule_create" for i in items), "筛选应精确匹配动作"
        # 按操作者筛选
        r = await ac.get("/api/audit-logs?operator=admin&limit=5")
        assert r.status_code == 200
        # 分页字段存在
        assert "total" in r.json()["data"]
        # 清理
        await ac.delete(f"/api/ops/alert-rules/{(r.json()['data'] or {}).get('x', 0)}") if False else None
    async with SessionLocal() as db:
        from app.models.alert import AlertRule
        await db.execute(delete(AlertRule).where(AlertRule.name == "d18-audit-rule"))
        await db.commit()
    app.dependency_overrides.clear()


# ── TC-COLL-16：演示可见域（前缀隔离） ──
@pytest.mark.asyncio
async def test_demo_data_prefix_visibility(monkeypatch):
    """demo 角色 + 配了 DEMO_DATA_PREFIXES 时，只能看到前缀匹配的主机。"""
    from app.core import config as cfg
    from app.services import visibility

    s = cfg.get_settings()
    monkeypatch.setattr(s, "demo_data_prefixes", "d18-visible", raising=False)
    async with SessionLocal() as db:
        # 造两台主机：一台匹配前缀、一台不匹配
        for nm in ("d18-visible-host", "d18-hidden-host"):
            ident = hashlib.sha256(nm.encode()).hexdigest()[:32]
            await db.execute(delete(Host).where(Host.ident == ident))
            db.add(Host(ident=ident, name=nm, status="up"))
        await db.commit()
    try:
        # demo 用户
        app = await _client_as("admin")
        async with SessionLocal() as db:
            demo = (await db.execute(select(User).where(User.role == "demo"))).scalars().first()
        if demo is None:
            pytest.skip("无 demo 用户")
        app.dependency_overrides[get_current_user] = lambda: demo
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/api/ops/hosts")
            assert r.status_code == 200
            names = [h["name"] for h in r.json()["data"].get("items", [])]
            # 配了前缀时：可见的都应带前缀；隐藏主机不该出现
            if names:
                assert all(n.startswith("d18-visible") for n in names), f"可见域泄露：{names}"
            assert "d18-hidden-host" not in names, "隐藏主机不应可见"
        # 直接查隐藏主机 → 404（不泄露存在性）
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            ident = hashlib.sha256(b"d18-hidden-host").hexdigest()[:32]
            r = await ac.get(f"/api/ops/hosts/{ident}")
            assert r.status_code == 404, f"越界主机应 404（不泄露存在性），实际 {r.status_code}"
        app.dependency_overrides.clear()
    finally:
        async with SessionLocal() as db:
            for nm in ("d18-visible-host", "d18-hidden-host"):
                ident = hashlib.sha256(nm.encode()).hexdigest()[:32]
                await db.execute(delete(Host).where(Host.ident == ident))
            await db.commit()


# ── TC-AUTH-09：修改自己的密码（手册 §1 承诺"首次登录后请立即修改"）──
@pytest.mark.asyncio
async def test_change_own_password():
    """自助改密端到端：原密码校验 → 新密码登录成功 → 旧密码失效。

    注意（测试方法学的坑）：这里**不能 mock get_current_user** —— mock 返回的用户对象
    来自另一个已关闭的 session（detached），`user.password_hash = ...` 改了内存对象而
    `db.commit()` 不会落库，会出现"接口 200 但新密码登录失败"的假象。
    必须走真实依赖链：登录拿 token → 带 token 调改密。
    """
    from app.core.security import hash_password
    app = create_app()
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "d18-pass"))).scalars().first()
        if u:
            await db.delete(u)
            await db.commit()
        db.add(User(username="d18-pass", password_hash=hash_password("old-pass-123"), role="member"))
        await db.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            # ① 原密码登录
            r = await ac.post("/api/auth/login", json={"username": "d18-pass", "password": "old-pass-123"})
            assert r.status_code == 200, f"初始密码应能登录：{r.text[:100]}"
            tok = r.json()["data"]["access_token"]
            hdr = {"Authorization": f"Bearer {tok}"}
            # ② 原密码错 → 400
            r = await ac.post("/api/auth/password", json={"old_password": "wrong", "new_password": "new-pass-456"},
                              headers=hdr)
            assert r.status_code == 400, f"原密码错应 400，实际 {r.status_code}"
            # ③ 新密码太短 → 400
            r = await ac.post("/api/auth/password", json={"old_password": "old-pass-123", "new_password": "short"},
                              headers=hdr)
            assert r.status_code == 400, f"弱密码应 400，实际 {r.status_code}"
            # ④ 正常改密 → 200
            r = await ac.post("/api/auth/password", json={"old_password": "old-pass-123", "new_password": "new-pass-456"},
                              headers=hdr)
            assert r.status_code == 200, r.text[:120]
            # ⑤ 新密码能登录、旧密码失效
            r = await ac.post("/api/auth/login", json={"username": "d18-pass", "password": "new-pass-456"})
            assert r.status_code == 200, "新密码应能登录"
            r = await ac.post("/api/auth/login", json={"username": "d18-pass", "password": "old-pass-123"})
            assert r.status_code == 401, "旧密码应失效"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(User).where(User.username == "d18-pass"))
            await db.commit()
