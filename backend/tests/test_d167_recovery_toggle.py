"""D167-1：「恢复通知」开关此前不生效（or 短路绕过），修复后全路径尊重。

用户需求（2026-10-22）：告警需要一个通用规则——恢复是否通知。
前端与模型早有 notify_recovered 字段（默认 True），但引擎
`do_notify = ev.is_recovered or should_notify(...)` 短路——恢复事件
**无条件发**，开关形同虚设。修：恢复事件是否发由 notify_recovered 决定，
且在 dispatch 入口统一拦（分级路径与通知路由路径都覆盖）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engine.state import FireState
from app.services import notify as notify_svc


class _Ch:
    id = 1
    name = "飞书"
    type = "feishu"
    config_json = {"webhook_url": "https://x"}
    fail_streak = 0
    disabled_until = None
    last_test_status = ""


def _ev(recovered: bool):
    return SimpleNamespace(is_recovered=recovered, hash="h" * 32, rule_id=1,
                           rule_name="r", host_ident="x", metric="mem.percent",
                           severity=2, trigger_value=1.0, tags={}, annotations={},
                           notify_cur_number=1, stale=False, status="firing",
                           trigger_time=None, first_trigger_time=None)


def test_should_notify_recovered_branch():
    """should_notify 的恢复分支本身语义正确（关=不发）。"""
    st = FireState(ev=_ev(False))
    assert notify_svc.should_notify(st, 0, 0, True, True) is True
    assert notify_svc.should_notify(st, 0, 0, False, True) is False


@pytest.mark.asyncio
async def test_dispatch_skips_recovery_when_disabled(monkeypatch):
    """notify_recovered=False + 恢复事件 → dispatch 一条不发，且返回可查的说明。"""
    from app.services import notify as ns

    called = []

    async def fake_send(ch_type, cfg, title, body, card=None):
        called.append(title)
        return True, "ok"

    monkeypatch.setattr(ns, "send_channel", fake_send)

    ev = _ev(recovered=True)
    rule_cfg = {"notify_recovered": False, "channels_json": ["飞书"], "expr": ">1", "severity": 2}

    class _DB:
        def __init__(self):
            self.n = 0

        def add(self, *a):
            pass

        async def commit(self):
            pass

        async def execute(self, *a, **kw):
            self.n += 1

            class _R:
                def __init__(self, rows):
                    self._rows = rows

                def scalars(self):
                    return self

                def scalar_one_or_none(self):
                    return None

                def all(self):
                    return self._rows

                def first(self):
                    return None
            # 第 1 次查询 = 启用渠道列表；其余（通知路由/Host）返回空
            return _R([_Ch()]) if self.n == 1 else _R([])
    monkeypatch.setattr(ns, "select", ns.select)  # 不动
    # dispatch 内部会查 Host（host=None 分支安全）
    out = await ns.dispatch(_DB(), ev, None, rule_cfg)
    assert called == [], f"恢复通知已关闭，不应发送（实际发了 {len(called)} 条）"
    assert out and "恢复通知" in out[0].get("detail", ""), out


@pytest.mark.asyncio
async def test_dispatch_sends_recovery_when_enabled(monkeypatch):
    """默认（notify_recovered=True）恢复事件照常发。"""
    from app.services import notify as ns

    called = []

    async def fake_send(ch_type, cfg, title, body, card=None):
        called.append(title)
        return True, "ok"

    monkeypatch.setattr(ns, "send_channel", fake_send)

    ev = _ev(recovered=True)
    rule_cfg = {"notify_recovered": True, "channels_json": ["飞书"], "expr": ">1", "severity": 2}

    class _DB:
        def __init__(self):
            self.n = 0

        def add(self, *a):
            pass

        async def commit(self):
            pass

        async def execute(self, *a, **kw):
            self.n += 1

            class _R:
                def __init__(self, rows):
                    self._rows = rows

                def scalars(self):
                    return self

                def scalar_one_or_none(self):
                    return None

                def all(self):
                    return self._rows

                def first(self):
                    return None
            return _R([_Ch()]) if self.n == 1 else _R([])

    out = await ns.dispatch(_DB(), ev, None, rule_cfg)
    assert len(called) == 1, f"默认应发恢复通知，实际 {len(called)}"
    assert any(o.get("ok") for o in out)
