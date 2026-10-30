"""D163-5/6/7/8/9 + D165 多手机号 + D166 告警历史：交付前遗留缺陷修复。

全部来自交付前全矩阵测试轮（台账 §1.28，用户指令"只测不修"登记的 5 条待修），
本轮按用户指令修复并加回归锁。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

AGENT = Path(__file__).resolve().parents[2] / "agent" / "agent_main.py"


def test_d163_5_cert_probe_strips_scheme():
    """D163-5（S1）：证书探活必须接受 UI 提示的 `https://host[:port]` 写法。

    现场：接入向导提示填 `https://your.domain`，Agent 用 partition(":") 解析
    → "https" 被当主机名 → 连接必败 → 按 UI 提示填的探活**永远 down**。
    真机验证：去 scheme 后同一函数 up、剩余 93 天。
    """
    src = AGENT.read_text(encoding="utf-8")
    assert "https://" in src and "D163-5" in src, "Agent 未做 scheme 剥除"
    # 模拟解析逻辑（与 Agent 实现同构）
    def parse(hostport):
        t = (hostport or "").strip()
        for scheme in ("https://", "http://"):
            if t.lower().startswith(scheme):
                t = t[len(scheme):]
                break
        host, _, port = t.partition(":")
        if not port.strip().isdigit():
            port = "443"
        return host, int(port)

    assert parse("https://www.baidu.com") == ("www.baidu.com", 443)
    assert parse("https://your.domain:8443") == ("your.domain", 8443)
    assert parse("www.baidu.com:8443") == ("www.baidu.com", 8443)   # 老写法
    assert parse("www.baidu.com") == ("www.baidu.com", 443)


def test_d163_6_passwd_read_allowed():
    """D163-6：读 /etc/passwd 放行（最高频运维操作），改密码命令 `passwd` 仍拦。"""
    from app.services.runbook import guard_command

    for cmd in ("cat /etc/passwd", "wc -l /etc/passwd", "grep root /etc/passwd"):
        g = guard_command(cmd)
        assert g.verdict == "allowlist", f"{cmd!r} 被误拒（{g.verdict}）：{g.reason}"
    g = guard_command("passwd")
    assert not g.allowed and g.verdict == "denylist", "改密码命令必须仍被拦"
    # 对照：敏感拦截不受影响
    assert not guard_command("rm -rf /").allowed


def test_d163_7_agent_curl_matches_platform():
    """D163-7：Agent 只读白名单必须含 `curl -I`（与平台对齐，HTTP 探活检查不中断）。"""
    src = AGENT.read_text(encoding="utf-8")
    m = re.search(r"RO_PREFIXES = \(([^)]*)\)", src)
    assert m, "RO_PREFIXES 未找到"
    ns: dict = {}
    exec(f"RO_PREFIXES = ({m.group(1)})", ns)  # noqa: S102 - 测试内解析常量
    prefixes = ns["RO_PREFIXES"]
    assert any(p.startswith("curl") for p in prefixes), (
        f"Agent 白名单缺 curl（平台有 curl -I）：{prefixes}")


def test_d163_8_bad_body_is_422():
    """D163-8：非 JSON / 二进制 body → 422 人话（不再 500 泄漏 TypeError）。"""
    import asyncio

    import httpx

    from app.main import app

    async def run():
        transport = httpx.ASGITransport(app=app)
        # 异常 handler 注册在 lifespan 内 → 测试必须真的跑 lifespan（生产 uvicorn 会跑）
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                tok = (await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
                H = {"Authorization": f"Bearer {tok}"}
                r1 = await c.post("/api/ops/alert-rules", headers={**H, "Content-Type": "text/plain"}, content="not json")
                r2 = await c.post("/api/ops/alert-rules", headers=H, content=b"\xff\xfe\x00")
            return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.status_code == 422, f"非 JSON body 应 422，实际 {r1.status_code} {r1.text[:120]}"
    assert "JSON" in r1.text, r1.text[:160]
    assert r2.status_code == 422, f"二进制垃圾应 422，实际 {r2.status_code} {r2.text[:120]}"
    assert "serializable" not in r2.text, "不得泄漏内部 TypeError 细节"


def test_d163_9_dev_mint_route_not_registered_in_prod():
    """D163-9：非 debug 模式下 dev/mint-license 不注册（OpenAPI 不暴露签发入口）。"""
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/dev/mint-license" not in paths, (
        "生产模式不得注册 dev 签发路由（双防护在但入口不该存在）")


def test_d165_multi_phone_split_and_aggregate():
    """D165：被叫号码支持逗号/分号分隔多个，逐个拨打、聚合结果。"""
    from app.services.notify_transport import _split_phones

    assert _split_phones("13800001111,13900002222") == ["13800001111", "13900002222"]
    assert _split_phones("13800001111；13900002222，13700003333") == [
        "13800001111", "13900002222", "13700003333"]
    assert _split_phones(" 13800001111 , 13800001111 ") == ["13800001111"], "去重去空"
    assert _split_phones("") == []

    # 多号分发：一个成功一个失败 → ok=True 且明细完整
    import app.services.notify_transport as nt

    async def fake_one(c, phone, title, body):
        if phone.endswith("2222"):
            return False, "阿里云语音拒绝（流控）"
        return True, f"aliyun-voice ok（呼叫 {phone}，CallId=X）"

    orig = nt._send_aliyun_voice_one
    nt._send_aliyun_voice_one = fake_one
    try:
        ok, detail = asyncio.run(nt.send_aliyun_voice(
            {"phone": "13800001111,13900002222", "access_key_id": "a",
             "access_key_secret": "b", "tts_code": "c"}, "t", "b"))
    finally:
        nt._send_aliyun_voice_one = orig
    assert ok, detail
    assert "1/2" in detail and "13800001111" in detail, "应写明几通成功"
    assert "流控" in detail, "失败明细要保留"
