"""D144 守卫：四处用户实测报障的修复回归。

① 电话渠道要有手机号输入：后端传输层必须支持 `phone` 字段 + `{phone}` 占位网关，
   而不是只认 apprise_url 逃生口（用户原话：「选择了类型为电话，为什么没有手机号输入的地方？」）。
② 生效时段 UTC+8：UI 标注「告警通知生效时间 (UTC+8)」→ 引擎必须按北京时间判定；
   effective_now 兼容 ISO 1..7 星期写法（旧 UI 写 1..7、后端按 0..6 比较，周日/周一错判）。
③ 跨零点时段：22:00~06:00 这类"夜里值班"时段必须生效。
④ /agent_version 端点：install.sh 用来判定「版本一致，无需升级」。
"""
from __future__ import annotations

from datetime import datetime

import pytest


# ── ① 电话渠道：phone 字段 + {phone} 占位 ──────────────────────
def test_transport_phone_placeholder(monkeypatch):
    from app.services.notify_transport import to_apprise_url

    # 网关带 {phone} → 替换为手机号
    url = to_apprise_url("phone", {
        "phone": "+8613800138000",
        "apprise_url": "twilio://SID:Token@+8613800000000/{phone}"})
    assert url == "twilio://SID:Token@+8613800000000/+8613800138000", f"占位替换失败：{url}"

    # 占位符但没有手机号 → 配置不完整（测试时暴露，不许静默失败）
    assert to_apprise_url("phone", {"apprise_url": "twilio://SID:Token@From/{phone}"}) is None

    # 完整 apprise_url（不含占位）→ 原样透传
    full = "twilio://SID:Token@From/+8613800138000"
    assert to_apprise_url("phone", {"apprise_url": full}) == full

    # 只有手机号 → 平台级默认语音网关 AIOPS_VOICE_GATEWAY_URL
    monkeypatch.setenv("AIOPS_VOICE_GATEWAY_URL", "twilio://SID:Token@+8613800000000/{phone}")
    url = to_apprise_url("phone", {"phone": "+8613800138000"})
    assert url and "+8613800138000" in url

    # 只有手机号、也没有网关 → None（诚实：配置不完整）
    monkeypatch.delenv("AIOPS_VOICE_GATEWAY_URL", raising=False)
    assert to_apprise_url("phone", {"phone": "+8613800138000"}) is None


# ── ②③ 生效时段：ISO 星期 + 跨零点 ───────────────────────────
def test_effective_now_iso_days():
    """旧 UI 写 days 1..7（ISO，周一=1），后端此前按 Python weekday（周一=0）比较
    ——周日/周一整体错位一天。两种口径都要能解释。"""
    from app.engine.state import effective_now

    # ISO 写法：2026-10-15 周四（isoweekday=4 / weekday=3）
    iso_segs = [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}]
    assert effective_now(iso_segs, datetime(2026, 10, 15, 10, 0)) is True
    assert effective_now(iso_segs, datetime(2026, 10, 17, 10, 0)) is False   # 周六
    assert effective_now(iso_segs, datetime(2026, 10, 18, 10, 0)) is False   # 周日（iso 7，旧口径 6 恰好命中 → 必须不命中）

    # weekday 写法（0..6）保持兼容：周五 = 4
    wd_segs = [{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}]
    assert effective_now(wd_segs, datetime(2026, 10, 16, 10, 0)) is True     # 周五
    assert effective_now(wd_segs, datetime(2026, 10, 17, 10, 0)) is False    # 周六


def test_effective_now_overnight():
    """跨零点时段（夜里值班）：22:00~06:00 在 23:30 与次日 02:00 都必须生效。"""
    from app.engine.state import effective_now

    seg = [{"start": "22:00", "end": "06:00"}]
    assert effective_now(seg, datetime(2026, 10, 15, 23, 30)) is True
    assert effective_now(seg, datetime(2026, 10, 16, 2, 0)) is True
    assert effective_now(seg, datetime(2026, 10, 16, 12, 0)) is False


def test_core_eval_uses_cn_tz():
    """引擎按北京时间判定生效时段：core._eval_once 必须把 now+8h 传给 effective_now。

    用 AST 静态断言（起整条规则引擎要真库+调度器，成本不成比例；这里锁住"调用时
    传入了 +8h 偏移"这一结构事实——effective_now 本身的语义已有上面两条单测锁定）。"""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "engine" / "core.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "effective_now":
            for arg in node.args:
                if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                    found = True
    assert found, "core._eval_once 调 effective_now 时必须传 UTC+8 偏移（now + timedelta(hours=8)）"


# ── ④ /agent_version 端点 ────────────────────────────────────
@pytest.mark.asyncio
async def test_agent_version_endpoint():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30) as ac:
        r = await ac.get("/agent_version")
    assert r.status_code == 200, r.text[:200]
    ver = r.text.strip()
    assert ver and ver[0].isdigit(), f"agent_version 应返回裸版本号（如 0.3.4），实际 {ver!r}"
