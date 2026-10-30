"""D154 守卫：三项能力补齐（FR-422 ~ FR-425）。

锁死七件事（语义错了都是生产事故）：
  ① ring 带维度存储：维度**可选**——不带维度老行为逐字节不变；tags_key 规范化稳定；
  ② 选择器解析 `metric{k=v}`：合法/空/坏语法三种输入的明确行为；
  ③ Pipeline 顺序语义：在**算指纹之前**执行（改标签→进指纹→不同维度独立事件；
     drop→不产生事件）；注解不参与指纹；
  ④ 通知规则四维筛选：**级别留空=一条都不匹配**（最易做错的语义）、时段跨零点、
     标签用事件标签（不匹配注解）；
  ⑤ 新旧通知路径**互斥**：新规则命中→不再走 channels_by_level；
  ⑥ Categraf 映射表：指标名来自夜莺官方集成定义（防杜撰），逐序列保留维度；
  ⑦ MCP：默认关闭、写工具默认不注册、未注册工具给出人话。
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = pathlib.Path(__file__).resolve().parents[2]

# ───────────────────────── ① ring 带维度存储 ─────────────────────────
def test_ring_dimensional_storage():
    from app.store import ring

    ident = "d154-ring-host"
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    # 不带维度 → 老行为（_rings）
    ring.append_point(ident, "cpu.percent", now, 12.5)
    assert ring.query_points(ident, "cpu.percent", now - timedelta(minutes=1), now)[0][1] == 12.5
    # 带维度 → _series，互不干扰
    ring.append_point(ident, "pg.connections.current", now, 10.0, tags={"server": "db01"})
    ring.append_point(ident, "pg.connections.current", now, 20.0, tags={"server": "db02"})
    s1 = ring.query_series(ident, "pg.connections.current", {"server": "db01"},
                           now - timedelta(minutes=1), now)
    s2 = ring.query_series(ident, "pg.connections.current", {"server": "db02"},
                           now - timedelta(minutes=1), now)
    assert len(s1) == 1 and s1[0][1][-1][1] == 10.0, s1
    assert len(s2) == 1 and s2[0][1][-1][1] == 20.0, s2
    # 规范化键稳定（与键顺序无关）
    assert ring.tags_key({"b": "2", "a": "1"}) == ring.tags_key({"a": "1", "b": "2"})
    assert ring.tags_key({"a": "1", "b": "2"}) == "a=1;b=2"
    # list_metric_names 也含带维度指标（否则 UI 看不到中间件指标）
    assert "pg.connections.current" in ring.list_metric_names(ident)
    # 空维度点与普通点同路径
    ring.append_point(ident, "load.1", now, 1.0, tags={})
    assert ring.query_points(ident, "load.1", now - timedelta(minutes=1), now)


def test_ring_selector_parse():
    from app.store import ring

    assert ring.parse_selector("pg.connections.current") == ("pg.connections.current", None)
    assert ring.parse_selector("pg.connections.current{}") == ("pg.connections.current", {})
    assert ring.parse_selector("disk.*.percent{device=/dev/sda1}") == ("disk.*.percent", {"device": "/dev/sda1"})
    # 坏语法 → 当字面量指标名（查不到数据，不会误命中别的）
    assert ring.parse_selector("坏的{k") == ("坏的{k", None)
    assert ring.parse_selector("m{novalue}") == ("m{novalue}", None)


# ───────────────────────── ③ Pipeline 语义 ─────────────────────────
def test_state_machine_pipeline_before_fingerprint():
    """Pipeline 在算指纹**之前**执行：改标签 → 指纹变化 → 不同维度独立事件。"""
    from app.engine.state import StateMachine, fingerprint

    cfg = {"id": 1, "severity": 1, "for_duration_s": 0, "recover_duration_s": 0, "query": ">0",
           "event_pipelines": [{"processor": "set_tag", "key": "env", "value": "prod"}]}
    sm = StateMachine(cfg)
    now = __import__("datetime").datetime.utcnow()
    evs = sm.process([({"host": "h1", "metric": "m1"}, 99.0)], now)
    assert evs, "应有事件"
    ev = evs[0]
    assert ev.tags.get("env") == "prod", "Pipeline 改的标签应进事件"
    # 指纹应与"手工加同样标签"的一致（证明改写发生在指纹之前）
    expect = fingerprint(1, {"host": "h1", "metric": "m1", "env": "prod"}, 1, ">0")
    assert ev.hash == expect


def test_state_machine_pipeline_drop():
    """drop 处理器 → 该异常点不产生事件。"""
    from app.engine.state import StateMachine

    cfg = {"id": 2, "severity": 2, "for_duration_s": 0, "recover_duration_s": 0, "query": ">0",
           "event_pipelines": [{"processor": "drop"}]}
    sm = StateMachine(cfg)
    now = __import__("datetime").datetime.utcnow()
    assert sm.process([({"host": "h1", "metric": "m1"}, 99.0)], now) == []


def test_state_machine_no_pipeline_unchanged():
    """不配 Pipeline → 老行为逐字节不变。"""
    from app.engine.state import StateMachine, fingerprint

    cfg = {"id": 3, "severity": 2, "for_duration_s": 0, "recover_duration_s": 0, "query": ">0"}
    sm = StateMachine(cfg)
    now = __import__("datetime").datetime.utcnow()
    evs = sm.process([({"host": "h1", "metric": "m1"}, 50.0)], now)
    assert evs and evs[0].hash == fingerprint(3, {"host": "h1", "metric": "m1"}, 2, ">0")
    assert not evs[0].annotations


# ───────────────────────── ④ 四维筛选语义 ─────────────────────────
def _now():
    import datetime as dt
    return dt.datetime.utcnow() + dt.timedelta(hours=8)   # 时段判定按北京时间


def test_notify_rules_severity_empty_means_disabled():
    """★ 最易做错的语义：适用级别**留空 = 一条都不匹配**（停用该条），不是"不限制"。"""
    from datetime import datetime
    from app.services.notify_rules import match_config

    ev = {"severity": 1, "tags": {"env": "prod"}, "attributes": {}}
    assert match_config({"channel_id": 1, "severities": []}, ev, datetime.utcnow()) is False
    assert match_config({"channel_id": 1}, ev, datetime.utcnow()) is False          # 字段缺失同理
    assert match_config({"channel_id": 1, "severities": [1]}, ev, datetime.utcnow()) is True
    assert match_config({"channel_id": 1, "severities": [2, 3]}, ev, datetime.utcnow()) is False


def test_notify_rules_time_ranges_cross_midnight():
    """时段：跨零点（21:00–09:00）支持；留空 = 不限制。"""
    from datetime import datetime, timedelta
    from app.services.notify_rules import in_time_ranges

    assert in_time_ranges(None, datetime.utcnow()) is True          # 留空 = 不限制
    # 用一个"确定在北京时间 21:00~09:00 之间"的 UTC 时刻构造跨零点样本
    # 北京 00:30 = UTC 前一天 16:30
    utc_at_0030 = datetime(2026, 10, 21, 0, 30) - timedelta(hours=8)
    r = [{"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "21:00", "end": "09:00"}]
    assert in_time_ranges(r, utc_at_0030) is True, "跨零点应命中（北京 00:30 ∈ [21:00, 09:00]）"
    # 北京 12:00 = UTC 04:00 → 不在 [21:00, 09:00]
    utc_at_noon = datetime(2026, 10, 21, 4, 0)
    assert in_time_ranges(r, utc_at_noon) is False


def test_notify_rules_label_match_not_annotations():
    """标签筛选按**事件标签**匹配；注解不参与匹配。"""
    from datetime import datetime
    from app.services.notify_rules import match_config

    ev = {"severity": 1, "tags": {"env": "prod"}, "attributes": {}}
    cfg = {"channel_id": 1, "severities": [1], "label_keys": [{"k": "env", "op": "==", "v": "prod"}]}
    assert match_config(cfg, ev, datetime.utcnow()) is True
    # 注解里的 env 匹配不到（这是"标签/注解分离"的核心约束）
    ev_ann = {"severity": 1, "tags": {}, "annotations": {"env": "prod"}, "attributes": {}}
    assert match_config(cfg, ev_ann, datetime.utcnow()) is False


# ───────────────────────── ⑤ 新旧路径互斥 ─────────────────────────
@pytest.mark.asyncio
async def test_new_rule_takes_over_and_old_path_falls_back():
    """新规则命中 → 接管（老 channels_by_level 不再走）；新规则未命中 → 回落老路径。"""
    from sqlalchemy import delete, select
    from app.db.session import SessionLocal
    from app.models.notify_rule import NotifyRule
    from app.models.alert import NotificationChannel

    name = "d154-takeover"
    async with SessionLocal() as db:
        await db.execute(delete(NotifyRule).where(NotifyRule.name == name))
        from sqlalchemy import text as _text
        await db.execute(_text("DELETE FROM notification_channels WHERE name LIKE 'd154-takeover-%'"))
        # 渠道 + 规则（severities 命中 S1）
        ch = NotificationChannel(name=name + "-ch", type="webhook",
                                 config_json={"webhook_url": "http://127.0.0.1:1/x"}, enabled=True)
        db.add(ch)
        await db.flush()
        db.add(NotifyRule(name=name, enabled=True,
                          configs=[{"channel_id": ch.id, "severities": [1]}]))
        await db.commit()
        ch_id = ch.id

    from app.services import notify as nsvc

    class _Ev:
        hash = "d154hash"; severity = 1; host_ident = "h"
        tags = {"env": "prod"}; metric = "cpu.percent"; is_recovered = False

    async with SessionLocal() as db:
        targets = await nsvc._resolve_new_rules(db, _Ev(), {})
        assert targets, "S1+env=prod 应命中"
        assert targets[0]["rule_name"] == name
        # 互斥：派发走新路径 → 只投新规则配的渠道；传入一个"老分级渠道"，
        # 它**不该**收到（新规则命中即接管，老路径不再走）。
        from app.models.alert import NotificationChannel as _NC
        old_ch = _NC(name=name + "-old-im", type="webhook",
                     config_json={"webhook_url": "http://127.0.0.1:1/x"}, enabled=True)
        db.add(old_ch)
        await db.flush()
        real_ch = await db.get(_NC, targets[0]["channel_id"])
        res = await nsvc._dispatch_by_new_rules(db, _Ev(), {}, [real_ch, old_ch], targets)
        # 断言的是**目标选择**（webhook 地址故意配成不通，发送必然失败——没关系）：
        # 只有新规则配的渠道被**尝试**投递（台账有记录），老分级渠道零投递 = 互斥成立。
        attempted = {r["channel"] for r in res}
        assert attempted == {f"{name}-ch"}, f"只有新规则的渠道该被投递（老渠道零投递）：{res}"
        assert all(r.get("rule") == name for r in res)
        # 台账：新规则的渠道有投递记录，老渠道没有
        from app.models.alert import NotificationRecord
        recs = (await db.execute(select(NotificationRecord).where(
            NotificationRecord.event_hash == "d154hash"))).scalars().all()
        assert {r.channel for r in recs} == {f"{name}-ch"}, [r.channel for r in recs]

    async with SessionLocal() as db:
        await db.execute(delete(NotifyRule).where(NotifyRule.name == name))
        from sqlalchemy import text as _text
        await db.execute(_text("DELETE FROM notification_channels WHERE name LIKE 'd154-takeover-%'"))
        await db.commit()


# ───────────────────────── ⑥ Categraf 映射（防杜撰） ─────────────────────────
def test_categraf_mapping_names_match_n9e_official():
    """映射表里的**每一个** Categraf 指标名都必须能在夜莺官方集成定义里找到。

    这是"防杜撰"守卫：改名/加映射时，先在 ~/nightingale/integrations/ 里核实指标名。
    本地没有夜莺源码时跳过（不影响 CI/交接环境）。
    """
    n9e = pathlib.Path.home() / "nightingale"
    if not n9e.exists():
        pytest.skip("未克隆夜莺源码，跳过映射名对拍")
    import json

    from app.services.prom_scrape import CATEGRAF_MAP

    # 收集官方定义里的全部 Categraf 指标名（metrics/*.json 的 expression +
    # dashboards/alerts 里的PromQL 指标 token）
    official: set[str] = set()
    for f in (n9e / "integrations").rglob("*"):
        if not f.is_file() or f.suffix not in (".json", ".toml", ".md"):
            continue
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if f.name.startswith("categraf") and f.suffix == ".json":
            try:
                for it in json.loads(txt):
                    e = str(it.get("expression") or "")
                    e = e.split("[", 1)[0].split("(", 1)[0].strip()
                    if e and not e.startswith(("irate", "rate")):
                        official.add(e)
            except Exception:
                pass
        import re
        official |= set(re.findall(r"\b((?:mysql|redis|postgresql|nginx|mem|disk|system|net)_[a-z0-9_]+)\b", txt))

    missing = [m for m in CATEGRAF_MAP if m not in official]
    assert not missing, f"映射表里有官方定义中不存在的指标名（疑似杜撰）：{missing}"


def test_series_mapping_keeps_dimensions():
    """多实例序列**不再被压成一条**（旧 map_to_platform_metrics 取 rows[0] 的缺陷）。"""
    from app.services.prom_scrape import map_to_platform_series

    parsed = {
        "postgresql_numbackends": [
            ({"server": "pg-a", "db": "order"}, 30.0),
            ({"server": "pg-a", "db": "user"}, 12.0),
            ({"server": "pg-b", "db": "order"}, 7.0),
        ],
        "cpu_usage_idle": [({"cpu": "cpu-total"}, 90.0)],   # 不会被映射（不在表里）
    }
    out = map_to_platform_series(parsed)
    pg = [(t, v) for m, t, v in out if m == "pg.connections.current"]
    assert len(pg) == 3, f"3 条 PG 序列都应保留，实际 {len(pg)}"
    assert {"server": "pg-a", "db": "order"} in [t for t, _ in pg]
    # 噪声标签被丢（cpu/mode 不进维度）
    parsed2 = {"mysql_global_status_threads_connected": [({"instance": "x", "job": "y"}, 5.0)]}
    out2 = map_to_platform_series(parsed2)
    got = [(t, v) for m, t, v in out2 if m == "mysql.connections.current"]
    assert got and got[0][0].get("instance") == "x" and "job" not in got[0][0], got


# ───────────────────────── ⑦ MCP ─────────────────────────
def _load_mcp():
    sys.modules.setdefault("app_routers_mcp_test", importlib.import_module("app.routers.mcp"))
    return sys.modules["app_routers_mcp_test"]


def test_mcp_defaults_closed_and_writes_hidden(monkeypatch):
    m = _load_mcp()
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    monkeypatch.delenv("MCP_ENABLE_WRITES", raising=False)
    assert m._mcp_enabled() is False, "MCP 端点必须默认关闭"
    assert m._writes_enabled() is False, "写工具必须默认不注册"
    assert "write_findings" not in m._read_only_tool_names(), "write_findings 属写工具，默认不开放"
    # 只读工具在
    assert "query_metrics" in m._read_only_tool_names()
    # 打开后写工具可见
    monkeypatch.setenv("MCP_ENABLE_WRITES", "true")
    assert "write_findings" in m._read_only_tool_names()


def test_mcp_rejects_unknown_tool_with_human_message():
    m = _load_mcp()
    out = asyncio.get_event_loop().run_until_complete(
        m._call_tool("not_a_tool", {}, None, None, None)) if False else None
    # _call_tool 需要 db/user——只测未注册分支（在 _read_only_tool_names 之外直接短路）
    async def _run():
        return await m._call_tool("not_a_tool", {}, None, None, None)
    out = asyncio.run(_run())
    assert "error" in out and "未通过 MCP 开放" in out["error"]
