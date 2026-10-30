"""D56：Agent 注册名与主机改名（用户：「我希望可以起一个名字，不然用户怎么能知道自己机器注册上去了」）。

实测背景：用户的 Mac hostname 是局域网 IP（192.168.0.185），注册后主机管理里显示的就是 IP，
用户完全认不出哪台是自己的机器。

锁四件事：
① install.sh 必须支持 --name、必须检测"hostname 是 IP/为空"并自动生成友好名、必须把名字打印给用户；
② agent 注册 payload 里 --name 优先于 platform.node()；
③ 注册接口：新主机用上报名；**重注册不改名**（用户可能已在界面改名，agent 重启不能覆盖）；
④ 改名接口：只改 Host.name，ident 不变（Agent 凭证与数据连续），权限 write，审计留痕。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "agent" / "install.sh"
AGENT = ROOT / "agent" / "agent_main.py"


def test_install_sh_generates_friendly_name():
    s = INSTALL.read_text(encoding="utf-8")
    assert "--name" in s, "install.sh 必须支持 --name"
    assert "^[0-9.]+$" in s, "必须检测 hostname 为纯 IP 的形态（实测 192.168.0.185）"
    assert "Mac-${" in s or "Mac-$" in s, "Darwin 下应生成 Mac-xxxx 友好名"
    assert "host-${" in s or "host-$" in s, "Linux 下应生成 host-xxxx 友好名"
    assert "本机注册名" in s, "安装完成必须把注册名打印给用户（用户明确要求）"
    # plist/systemd/nohup 三个启动路径都要带上 --name
    assert "<string>--name</string>" in s, "launchd 路径需透传 --name"
    assert s.count('--name "$AGENT_NAME"') >= 2 or ("EXTRA_ARGS+=(--name" in s and "<string>--name</string>" in s), \
        "systemd/nohup 经 EXTRA_ARGS 透传 --name"


def test_agent_prefers_explicit_name():
    s = AGENT.read_text(encoding="utf-8")
    assert 'add_argument("--name"' in s, "agent 需支持 --name"
    assert '"hostname": args.name or platform.node(),' in s, "注册时 --name 优先于 platform.node()"


def test_register_uses_name_and_rename_keeps_ident():
    import asyncio
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select, text

    from app.core.security import get_current_user
    from app.main import create_app
    from app.models.host import Host
    from app.models.shell import User
    from app.services import ingest

    from app.db.session import SessionLocal

    async def main():
        app = create_app()
        async with SessionLocal() as db:
            u = (await db.execute(select(User).where(User.username == "admin"))).scalars().first()
        app.dependency_overrides[get_current_user] = lambda: u
        fp = "d56-test-fingerprint"
        ident = __import__("hashlib").sha256(fp.encode()).hexdigest()[:32]
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                # ① 首次注册：name 优先于 hostname
                async with SessionLocal() as db:
                    ingest.INSTALL_TOKENS["d56-tok"] = "unused"
                r = await ac.post("/api/ops/agents/register", json={
                    "fingerprint": fp, "hostname": "192.168.9.9", "name": "My-Mac-ab12",
                    "os": "Darwin 27", "ip": "", "agent_version": "0.1.0",
                    "install_token": "d56-tok"})
                assert r.status_code == 200, r.text[:200]
                async with SessionLocal() as db:
                    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
                    assert host.name == "My-Mac-ab12", f"注册名应优先用 name：{host.name}"
                # ② 重注册（不带 name）不得覆盖已有名
                r = await ac.post("/api/ops/agents/register", json={
                    "fingerprint": fp, "hostname": "192.168.9.9", "os": "Darwin 27",
                    "ip": "", "agent_version": "0.1.0"})
                assert r.status_code == 200
                async with SessionLocal() as db:
                    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
                    assert host.name == "My-Mac-ab12", "重注册不得改名"
                # ③ 改名：改显示名、ident 不变
                r = await ac.post(f"/api/ops/hosts/{ident}/rename", json={"name": "我的笔记本"})
                assert r.status_code == 200, r.text[:200]
                assert r.json()["data"]["ident"] == ident
                async with SessionLocal() as db:
                    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
                    assert host.name == "我的笔记本"
                # ④ 改名校验：空名 422、超长 422、不存在的主机 404
                r = await ac.post(f"/api/ops/hosts/{ident}/rename", json={"name": "  "})
                assert r.status_code == 422
                r = await ac.post(f"/api/ops/hosts/{ident}/rename", json={"name": "x" * 65})
                assert r.status_code == 422
                r = await ac.post("/api/ops/hosts/nonexistent/rename", json={"name": "x"})
                assert r.status_code == 404
        finally:
            async with SessionLocal() as db:
                await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
                await db.commit()
            app.dependency_overrides.clear()

    asyncio.run(main())
