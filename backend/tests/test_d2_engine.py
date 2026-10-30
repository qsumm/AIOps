"""D2 单测：指纹状态机六态 + 通知节奏四例 + mute 链（06 §2 强制模块）。"""
from datetime import datetime, timedelta

import pytest

from app.engine.state import StateMachine, effective_now, fingerprint
from app.services.mute import is_muted
from app.services.notify import FireState, should_notify
from app.engine.state import Ev


def cfg(**kw):
    base = {"id": 1, "name": "r", "severity": 2, "for_duration_s": 0, "recover_duration_s": 0,
            "query": "q", "append_tags": {}}
    base.update(kw)
    return base


T0 = datetime(2026, 10, 15, 10, 0, 0)
L = {"host": "h1", "metric": "cpu.percent"}


def test_new_trigger():
    sm = StateMachine(cfg())
    out = sm.process([(L, 95.0)], now=T0)
    assert len(out) == 1 and not out[0].is_recovered and out[0].trigger_value == 95.0


def test_update_emits_update_event():
    """活跃事件的更新**要入队**（标记 is_update），否则重复通知与 updated 事件永远不会发生。

    D9 实测修正：原先 `process` 对已触发指纹只改状态、不入队，导致消费端的
    `should_notify`（repeat_step / max_number 节奏）永远不会被再次调用——
    D2 当年只单测了 should_notify 这个纯函数，没测它在链路上是否真的被调到。
    """
    sm = StateMachine(cfg())
    sm.process([(L, 95.0)], now=T0)
    out = sm.process([(L, 97.0)], now=T0 + timedelta(seconds=15))
    assert len(out) == 1, out
    assert out[0].is_update and not out[0].is_recovered
    assert out[0].trigger_value == 97.0
    # 同一指纹在 fires 里仍只有一条（更新不产生新活跃事件）
    assert len(sm.fires) == 1
    assert sm.fires[fingerprint(1, L, 2, "q")].ev.trigger_value == 97.0


def test_same_fingerprint_points_collapse_in_one_round():
    """一轮内的同指纹多采样点必须收敛成**一条**事件（否则就是告警风暴）。

    D9 实测：threshold 会逐点返回窗口内所有越限点，5 分钟窗口配 30s 采集 ⇒ 一轮 10 个点，
    消费端/SSE 一次收到 10 条一模一样的 updated。取值保留最后一点（＝最新）。
    """
    sm = StateMachine(cfg())
    out = sm.process([(L, 80.0), (L, 88.0), (L, 95.0)], now=T0)
    assert len(out) == 1, out
    assert out[0].trigger_value == 95.0 and not out[0].is_update
    # 持续越限：下一轮再带 4 个点，同样只出一条 updated
    out = sm.process([(L, 96.0), (L, 97.0), (L, 98.0), (L, 99.0)], now=T0 + timedelta(seconds=15))
    assert len(out) == 1 and out[0].is_update and out[0].trigger_value == 99.0
    assert len(sm.fires) == 1


def test_recover_after_quiet():
    sm = StateMachine(cfg(recover_duration_s=60))
    h = fingerprint(1, L, 2, "q")
    sm.process([(L, 95.0)], now=T0)
    out = sm.process([], now=T0 + timedelta(seconds=30))
    assert out == []  # 留观期内不恢复
    out = sm.process([], now=T0 + timedelta(seconds=61))
    assert len(out) == 1 and out[0].is_recovered and h not in sm.fires


def test_restart_replay():
    sm = StateMachine(cfg())
    h = fingerprint(1, L, 2, "q")
    sm.replay([{"hash": h, "rule_id": 1, "rule_name": "r", "host_ident": "h1", "metric": "cpu.percent",
                "severity": 2, "trigger_value": 91.0, "tags": {"host": "h1"},
                "first_trigger_time": T0, "trigger_time": T0, "notify_cur_number": 2}])
    assert h in sm.fires and sm.fires[h].notify_count == 2  # 通知计数续算
    out = sm.process([(L, 92.0)], now=T0 + timedelta(seconds=15))
    # 回放后不重复"触发"（不新增活跃事件），但会产生一次**更新**事件（通知节奏与 updated 事件需要）
    assert len(out) == 1 and out[0].is_update and not out[0].is_recovered
    assert len(sm.fires) == 1 and h in sm.fires


def test_algo_change_new_fingerprint():
    sm1 = StateMachine(cfg(query="q1"))
    sm2 = StateMachine(cfg(query="q2"))  # 换算法 → query 变 → 新指纹
    h1 = fingerprint(1, L, 2, "q1")
    h2 = fingerprint(1, L, 2, "q2")
    assert h1 != h2  # 换算法不误恢复旧事件（FR-C3 注记）


def test_for_duration_gate():
    sm = StateMachine(cfg(for_duration_s=60))
    out = sm.process([(L, 95.0)], now=T0)
    assert out == []  # 持续时长未到：进 pendings
    out = sm.process([(L, 95.0)], now=T0 + timedelta(seconds=59))
    assert out == []
    out = sm.process([(L, 95.0)], now=T0 + timedelta(seconds=61))
    assert len(out) == 1


def test_effective_time():
    segs = [{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}]
    assert effective_now(segs, datetime(2026, 10, 15, 10, 0)) is True   # 周四上午
    assert effective_now(segs, datetime(2026, 10, 15, 20, 0)) is False  # 晚间
    assert effective_now(segs, datetime(2026, 10, 17, 10, 0)) is False  # 周六
    assert effective_now([], datetime(2026, 10, 17, 10, 0)) is True     # 空=全天


# ---- 通知节奏四例 ----
def _st(sent_at=None, count=1):
    ev = Ev(hash="x", rule_id=1, rule_name="r", host_ident="h", metric="m", severity=2,
            trigger_value=1.0, labels={}, tags={})
    return FireState(ev=ev, last_sent=sent_at, notify_count=count)


def test_notify_once_only():
    st = _st(sent_at=T0, count=2)
    assert should_notify(st, repeat_step_min=0, max_number=0, notify_recovered=True,
                         ev_is_recovered=False, now=T0 + timedelta(hours=1)) is False


def test_notify_repeat_by_step():
    st = _st(sent_at=T0, count=2)
    assert should_notify(st, repeat_step_min=30, max_number=0, notify_recovered=True,
                         ev_is_recovered=False, now=T0 + timedelta(minutes=10)) is False
    assert should_notify(st, repeat_step_min=30, max_number=0, notify_recovered=True,
                         ev_is_recovered=False, now=T0 + timedelta(minutes=31)) is True


def test_notify_max_number_cap():
    # 语义：notify_count = 1 + 已发次数；封顶 max_number 表示"最多发 max_number 条"
    # count=5 → 可发第 5 条(5<=5)；count=6 → 已发满被拦
    st = _st(sent_at=T0, count=5)
    assert should_notify(st, repeat_step_min=30, max_number=5, notify_recovered=True,
                         ev_is_recovered=False, now=T0 + timedelta(hours=1)) is True
    st6 = _st(sent_at=T0, count=6)
    assert should_notify(st6, repeat_step_min=30, max_number=5, notify_recovered=True,
                         ev_is_recovered=False, now=T0 + timedelta(hours=1)) is False


def test_notify_recovered_switch():
    st = _st()
    assert should_notify(st, 0, 0, notify_recovered=True, ev_is_recovered=True) is True
    assert should_notify(st, 0, 0, notify_recovered=False, ev_is_recovered=True) is False


# ---- mute 链 ----
def test_mute_chain():
    tags = {"host": "db-01", "metric": "disk"}
    # 全屏蔽
    assert is_muted([{"tag_filters": [{"k": "host", "op": "==", "v": "db-01"}], "severities": [],
                      "btime": T0, "etime": T0 + timedelta(hours=1), "mute_type": 0}], tags, 2, now=T0) == 0
    # 只屏蔽通知
    assert is_muted([{"tag_filters": [], "severities": [1, 2],
                      "btime": T0, "etime": T0 + timedelta(hours=1), "mute_type": 1}], tags, 2, now=T0) == 1
    # 级别不符 → 不屏蔽
    assert is_muted([{"tag_filters": [], "severities": [1],
                      "btime": T0, "etime": T0 + timedelta(hours=1), "mute_type": 0}], tags, 2, now=T0) is None
    # 周期时段（每日 02:00-03:00）
    per = [{"tag_filters": [], "severities": [], "periodic_mutes": [{"start": "02:00", "end": "03:00"}], "mute_type": 1}]
    assert is_muted(per, tags, 2, now=datetime(2026, 10, 15, 2, 30)) == 1
    assert is_muted(per, tags, 2, now=datetime(2026, 10, 15, 12, 0)) is None
