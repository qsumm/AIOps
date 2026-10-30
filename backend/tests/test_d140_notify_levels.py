"""D140 守卫：告警分级通知渠道（用户原话：「S1 紧急的一定要通过打电话的。
S2 S3 可以接微信群、企微、钉钉、飞书等……严重的一定是电话通知，其他的无所谓」）。

锁四件**会静默退化**的事：
  ① S1 电话兜底：分级配置里 S1 没有电话渠道 → 后端必须 400 拒收（保存时暴露，
     而不是告警响的时候才发现只有 IM）——不许静默降级；
  ② dispatch 分级路由：channels_by_level 优先于 channels_json，S1/S2/S3 各发各的；
  ③ S1 运行时兜底：电话渠道被删/停用后仍来的 S1 → 升级为全渠道广播 + 台账留痕
     （宁可打扰所有人，也不能让紧急告警只躺在 IM 里）；
  ④ phone/wecom 渠道类型在传输层可用（apprise URL 映射不返回 None）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
TRANSPORT = ROOT / "static"  / ".." / "backend" / "app" / "services" / "notify_transport.py"


# ── ① 传输层：phone / wecom 映射 ─────────────────────────────
def test_transport_maps_phone_and_wecom():
    from app.services.notify_transport import to_apprise_url

    # phone：必须有 apprise_url（语音网关），没有 → None（前端/测试时暴露）
    assert to_apprise_url("phone", {}) is None
    assert to_apprise_url("phone", {"apprise_url": "twilio://SID:Token@From/To"}) is not None
    # wecom：企业微信群机器人 webhook（与 dingtalk 同形）
    url = to_apprise_url("wecom", {"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"})
    assert url and url.startswith("wecom://"), f"wecom 映射失败：{url}"
    # 友好类型清单必须含这两个（渠道列表 UI 的类型枚举来源）
    from app.services.notify_transport import FRIENDLY_TYPES
    assert "phone" in FRIENDLY_TYPES and "wecom" in FRIENDLY_TYPES


# ── ②③ dispatch 分级路由 + S1 运行时兜底（用真实 ASGI 应用 + 真库）────────
pytestmark = pytest.mark.asyncio

@pytest.fixture
async def notify_env():
    """建两条渠道（电话+IM）、一条分级规则，测完清场。

    注意：只把名字/配置传给测试体，不跨事件循环传 Session（每个用例自己的
    asyncio loop 里开新 Session——跨 loop 复用 Session 会炸 "attached to a different loop"）。
    """
    import uuid
    from app.db.session import SessionLocal
    from app.models.alert import AlertRule, NotificationChannel
    tag = uuid.uuid4().hex[:8]
    names = {"phone": f"d140-phone-{tag}", "im": f"d140-im-{tag}"}
    async with SessionLocal() as db:
        ph = NotificationChannel(name=names["phone"], type="phone", enabled=True,
                                 config_json={"apprise_url": "twilio://SID:Token@From/To"})
        im = NotificationChannel(name=names["im"], type="feishu", enabled=True,
                                 config_json={"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x?token=t"})
        db.add_all([ph, im])
        rule = AlertRule(
            name=f"d140-rule-{tag}", scope="metric", metric="cpu.percent", expr=">99",
            severity=1, eval_interval_s=15, window_s=60,
            channels_json=[names["im"]],
            channels_by_level={"1": [names["phone"]], "2": [names["im"]], "3": [names["im"]]},
        )
        db.add(rule)
        await db.commit()
        yield {"names": names, "rule_id": rule.id, "tag": tag}
        rows = (await db.execute(
            select(AlertRule).where(AlertRule.name == f"d140-rule-{tag}"))).scalars().all()
        for r in rows:
            await db.delete(r)
        for c in (await db.execute(select(NotificationChannel).where(
                NotificationChannel.name.in_([names["phone"], names["im"]])))).scalars().all():
            await db.delete(c)
        await db.commit()


def _make_event(rule_id: int, severity: int, host_ident: str = "d140-host", value=95.0):
    """构造一个最小可 dispatch 的事件对象（真 Ev dataclass，字段与引擎产出一致）。"""
    from app.engine.state import Ev
    return Ev(hash=f"d140{'0' * 28}", rule_id=rule_id, rule_name=f"d140-rule-{rule_id}",
              host_ident=host_ident, metric="cpu.percent", severity=severity,
              trigger_value=value, labels={}, tags={})


async def test_s1_dispatch_routes_to_phone(notify_env):
    """S1 事件：分级配置说「只走电话」→ 实际发送只投电话渠道（IM 不收）。"""
    from app.engine.state import FireState
    from app.services import notify as notify_svc
    from app.engine.state import Ev
    from app.db.session import SessionLocal

    names, rid = notify_env["names"], notify_env["rule_id"]
    ev = _make_event(rid, severity=1)
    rule_cfg = {"name": "d140", "channels_json": [names["im"]],
                "channels_by_level": {"1": [names["phone"]], "2": [names["im"]], "3": [names["im"]]},
                "expr": ">99", "severity": 1}
    async with SessionLocal() as db:
        results = await notify_svc.dispatch(db, ev, FireState(ev=ev), rule_cfg)
    sent = {r["channel"] for r in results if r["channel"]}
    assert sent == {names["phone"]}, f"S1 应只投电话渠道，实际：{sent}"
    assert names["im"] not in sent, "S2/S3 的 IM 渠道不该收到 S1 事件"


async def test_s2_dispatch_routes_to_im(notify_env):
    """S2 事件：分级配置说「只走 IM」→ 电话渠道不收。"""
    from app.engine.state import FireState
    from app.services import notify as notify_svc
    from app.db.session import SessionLocal

    names, rid = notify_env["names"], notify_env["rule_id"]
    ev = _make_event(rid, severity=2)
    rule_cfg = {"name": "d140", "channels_json": [],
                "channels_by_level": {"1": [names["phone"]], "2": [names["im"]], "3": [names["im"]]},
                "expr": ">99", "severity": 2}
    async with SessionLocal() as db:
        results = await notify_svc.dispatch(db, ev, FireState(ev=ev), rule_cfg)
    sent = {r["channel"] for r in results if r["channel"]}
    assert sent == {names["im"]}, f"S2 应只投 IM 渠道，实际：{sent}"
    assert names["phone"] not in sent, "S1 的电话渠道不该被 S2 事件吵醒"


async def test_s1_fallback_broadcasts_everything_when_phone_missing(notify_env):
    """S1 运行时兜底：电话渠道被停用后仍来的 S1 → 升级全渠道广播 + degraded 台账留痕。

    铁律：紧急告警不能因为电话渠道挂了就静默降级成 IM。
    """
    from app.engine.state import FireState
    from app.services import notify as notify_svc
    from app.models.alert import NotificationChannel
    from app.db.session import SessionLocal

    names, rid = notify_env["names"], notify_env["rule_id"]
    # 电话渠道停用 → enabled 查询里它就消失了
    async with SessionLocal() as db:
        ph = (await db.execute(select(NotificationChannel).where(
            NotificationChannel.name == names["phone"]))).scalar_one()
        ph.enabled = False
        await db.commit()
    ev = _make_event(rid, severity=1)
    rule_cfg = {"name": "d140", "channels_json": [],
                "channels_by_level": {"1": [names["phone"]], "2": [names["im"]], "3": [names["im"]]},
                "expr": ">99", "severity": 1}
    async with SessionLocal() as db:
        results = await notify_svc.dispatch(db, ev, FireState(ev=ev), rule_cfg)
    sent = {r["channel"] for r in results if r["channel"]}
    assert names["im"] in sent, "兜底应把 S1 升级为全渠道广播（IM 也要收到）"
    degraded = [r for r in results if str(r.get("detail", "")).startswith("S1 兜底生效")]
    assert degraded, "S1 兜底必须显式留痕（degraded 说明），不能静默"
    # 还原
    async with SessionLocal() as db:
        ph = (await db.execute(select(NotificationChannel).where(
            NotificationChannel.name == names["phone"]))).scalar_one()
        ph.enabled = True
        await db.commit()


async def test_no_levels_falls_back_to_channels_json(notify_env):
    """向后兼容：没配 channels_by_level 的规则仍按 channels_json 走（老规则零影响）。"""
    from app.engine.state import FireState
    from app.services import notify as notify_svc
    from app.db.session import SessionLocal

    names, rid = notify_env["names"], notify_env["rule_id"]
    ev = _make_event(rid, severity=1)
    rule_cfg = {"name": "d140", "channels_json": [names["im"]],
                "channels_by_level": None, "expr": ">99", "severity": 1}
    async with SessionLocal() as db:
        results = await notify_svc.dispatch(db, ev, FireState(ev=ev), rule_cfg)
    sent = {r["channel"] for r in results if r["channel"]}
    assert sent == {names["im"]}, "未配分级时回退 channels_json（老行为）"


# ── ④ 规则 API：S1 无电话 → 400 拒收 ─────────────────────────
async def test_rule_api_rejects_s1_without_phone_channel():
    """写入口径的 S1 兜底：分级配置 S1 无电话渠道 → 400，人话指路。"""
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
        tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        body = {"name": f"d140-s1-guard-{uuid_hex()}", "scope": "metric", "metric": "cpu.percent",
                "expr": ">99", "severity": 1,
                "channels_by_level": {"1": ["不存在的电话渠道"]}}
        r = await ac.post("/api/ops/alert-rules", json=body, headers=h)
        assert r.status_code == 400, f"S1 无电话渠道必须 400，实际 {r.status_code}: {r.text[:200]}"
        assert "电话" in r.json()["msg"], f"报错必须指路电话渠道：{r.json()['msg']}"


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:8]
