"""D162：外部指标源的「每源间隔真正生效」与「白名单业务指标可带维度下钻」。

**D162-1**（公网部署后实测）：D161-4 只修好了"注册"，**执行体**仍是同步壳——
    with SessionLocal() as db:      # AsyncSession 不支持同步上下文管理器
→ 每次执行抛 TypeError；且 APScheduler 对非协程 job 丢线程池，线程里取不到 loop。
于是"每源按 scrape_interval_s 精确拉取"**从未真正发生**，只有 30s 全局 tick 在拉，
60s 被拉成 60~90s 抖动。修：执行体改成协程 + 全局 tick 给已注册独立 job 让位。

**D162-2**（公网实测）：用户白名单了业务指标（metric_keep=["biz_"]），
靶机暴露 biz_order_queue_backlog{queue=pay|trade,region=cn-east}，
但平台**查不到、也无法按维度下钻**——白名单路径走的是扁平提取（同名多序列互相覆盖 +
维度全丢），恰好废掉了「外部指标源」最核心的用途。
修：新增 extract_declared_series，白名单指标也逐序列保留 labels 入环。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.prom_scrape import (_parse_exposition, extract_declared_metrics,
                                     extract_declared_series)

BIZ_TEXT = """# HELP biz_order_queue_backlog queue backlog
# TYPE biz_order_queue_backlog gauge
biz_order_queue_backlog{agent_hostname="VM-0-13-centos",queue="pay",region="cn-east"} 3
biz_order_queue_backlog{agent_hostname="VM-0-13-centos",queue="trade",region="cn-east"} 4
# HELP node_cpu_seconds_total cpu
# TYPE node_cpu_seconds_total counter
node_cpu_seconds_total{cpu="0",mode="idle"} 100
node_cpu_seconds_total{cpu="1",mode="idle"} 100
"""


def test_declared_series_keeps_each_dimension():
    """D162-2：白名单指标的每条序列都要保留，不能被同名覆盖、不能丢维度。"""
    parsed = _parse_exposition(BIZ_TEXT)
    series = extract_declared_series(parsed, ["biz_"])
    assert len(series) == 2, f"biz_ 白名单应得到 2 条序列（pay/trade），实际 {len(series)}"
    queues = sorted(t.get("queue") for _, t, _ in series)
    assert queues == ["pay", "trade"], f"维度 queue 必须保留，实际 {queues}"
    assert all(t.get("region") == "cn-east" for _, t, _ in series), "维度 region 必须保留"
    # 数值必须与各自序列对应（pay=3 / trade=4），不能张冠李戴
    by_queue = {t["queue"]: v for _, t, v in series}
    assert by_queue == {"pay": 3.0, "trade": 4.0}, by_queue


def test_declared_series_respects_tag_keep_whitelist():
    """tag_keep 是维度白名单：只保留列出的标签（业务维度优先，去掉噪声标签）。"""
    parsed = _parse_exposition(BIZ_TEXT)
    series = extract_declared_series(parsed, ["biz_"], tag_keep=["queue"])
    assert series, "白名单命中应产出序列"
    for _, tags, _v in series:
        assert set(tags) <= {"queue"}, f"tag_keep 之外不应保留标签，实际 {tags}"
        assert "queue" in tags, "白名单内的标签必须保留"


def test_flat_view_still_works_for_preview():
    """扁平视图（测试按钮预览用）保留原行为：同名多序列压成一条，不破坏既有契约。"""
    flat = extract_declared_metrics(BIZ_TEXT, ["biz_"])
    assert flat == {"biz_order_queue_backlog": 4.0}, flat


def test_declared_series_empty_when_no_pattern():
    assert extract_declared_series(_parse_exposition(BIZ_TEXT), []) == []
    assert extract_declared_series(_parse_exposition(BIZ_TEXT), ["nomatch_"]) == []


@pytest.mark.asyncio
async def test_scrape_job_body_is_coroutine_not_broken_sync_shell():
    """D162-1：scrape job 执行体必须是协程，旧同步壳必须已删除。"""
    import inspect

    import app.sched as sched_mod

    assert inspect.iscoroutinefunction(sched_mod._run_scrape_one_async), (
        "scrape job 执行体必须是协程（调度器是 AsyncIOScheduler）；"
        "非协程 job 被丢进线程池，无法 await 异步 DB 会话")
    assert not hasattr(sched_mod, "_run_scrape_one_sync"), (
        "旧的同步执行体 _run_scrape_one_sync 必须删除（它打异步引擎必抛 TypeError）")


@pytest.mark.asyncio
async def test_db_series_fallback_parses_tags_key_not_json():
    """D162-3：metric_series 的 tags_key 是 `k=v;k=v` 格式，必须按该格式解析维度。

    现场（公网实测）：`_query_series_db` 把 tags_key 当 **JSON** 解析 → 解析失败
    → except 吞成 {} → **DB 回落路径上所有带维度序列都退化成空维度**，
    同一指标出现多条 tags:{}，用户看到的"下钻维度"是空的。
    """
    from app.routers.metrics import _query_series_db

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeDB:
        def __init__(self, rows):
            self._rows = rows

        async def execute(self, *_a, **_kw):
            return _FakeResult(self._rows)

    ident = "scrape:1"
    tkey = "agent_hostname=VM-0-13-centos;queue=pay;region=cn-east"
    rows = [
        (ident, "biz_order_queue_backlog", tkey, {}, 3.0, datetime(2026, 10, 21, 17, 0, 0)),
        (ident, "biz_order_queue_backlog",
         "agent_hostname=VM-0-13-centos;queue=trade;region=cn-east", {}, 4.0,
         datetime(2026, 10, 21, 17, 0, 0)),
    ]
    db = _FakeDB(rows)
    out = await _query_series_db(db, "biz_order_queue_backlog", ident, {},
                                  datetime(2026, 10, 21, 16, 0, 0),
                                  datetime(2026, 10, 21, 18, 0, 0), 50)
    assert len(out) == 2, f"两条不同维度序列应各自成一条，实际 {len(out)}"
    for r in out:
        assert r["tags"], "维度不得退化成空（D162-3）"
        assert r["tags"].get("region") == "cn-east", r["tags"]
        assert r["tags"].get("agent_hostname") == "VM-0-13-centos", r["tags"]
    queues = sorted(r["tags"]["queue"] for r in out)
    assert queues == ["pay", "trade"], queues
    # 带 tag_filter 时只返回匹配的那条
    out2 = await _query_series_db(db, "biz_order_queue_backlog", ident, {"queue": "pay"},
                                   datetime(2026, 10, 21, 16, 0, 0),
                                   datetime(2026, 10, 21, 18, 0, 0), 50)
    assert len(out2) == 1 and out2[0]["tags"]["queue"] == "pay", out2


def test_pipe_to_shell_is_denied_not_hitl():
    """D162-4：管道进 shell 必须**直接拒绝**（denylist），不能只判「需审批」。

    现场（公网实测）：`df -h | sh` 的 verdict 是 `hitl`——人工审批一次就能在生产机上
    执行远端下发的任意脚本，与 denylist「不可越过」的语义不一致。
    D161-6 只把 pipe-to-shell 判成「非只读」（→ hitl），denylist 只覆盖 curl|wget 管道。

    口径：denylist = AI 与人都不能越过；管道进 shell 属此类。
    """
    from app.services.runbook import guard_command

    for cmd in ["df -h | sh", "free -m | bash", "ls -l | sh", "cat x |sh"]:
        g = guard_command(cmd)
        assert not g.allowed, f"{cmd!r} 必须被拒绝"
        assert g.verdict == "denylist", (
            f"{cmd!r} 的 verdict 应为 denylist（不可越过），实际 {g.verdict!r}"
            " —— 判成 hitl 等于审批一次即可在生产机执行任意脚本")
        assert not g.need_hitl, f"{cmd!r} 不该走人工审批"


def test_readonly_commands_still_allowlisted():
    """收紧管道后，正常只读命令不得被误伤（回归护栏）。"""
    from app.services.runbook import guard_command

    for cmd in ["df -h", "free -m", "cat /etc/nginx/nginx.conf", "ls -l /etc",
                "ps aux", "stat /etc/hosts", "grep -i nginx /etc/nginx/nginx.conf"]:
        g = guard_command(cmd)
        assert g.allowed and g.verdict == "allowlist", f"{cmd!r} 应判只读放行，实际 {g.verdict}"
