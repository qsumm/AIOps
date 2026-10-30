"""D150 守卫：指标规则支持通配符（用户需求：一台虚机挂 10 块数据盘，不想配 10 条规则）。

语义（必须锁死，否则会退化成"一条规则只报第一块盘"或"所有盘共用一个告警"）：
  ① 一个模式匹配多个**实际指标名**；
  ② **每个实际指标单独成事件**（anomaly.metric 用真实名字 → 指纹不同 → 分盘告警/分别恢复）；
  ③ 普通指标名（无 * ? []）走原快路径，行为零变化；
  ④ 过宽模式有上限（200），不允许 `*` 把一次评估拖成上千次查询。
"""
from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_wildcard_helpers():
    from app.engine import core
    assert core._has_wildcard("disk.*.percent")
    assert core._has_wildcard("disk.data?.percent")
    assert not core._has_wildcard("disk.percent")
    assert core._metric_match("disk.*.percent", "disk.data.percent")
    assert core._metric_match("disk.*.percent", "disk.var_lib_nfs_rpc_pipefs.percent")
    assert not core._metric_match("disk.*.percent", "cpu.percent")
    assert not core._metric_match("disk.data*.percent", "disk.root.percent")
    # `*` 不跨段：不会把 inode 使用率一起命中；想含 inode 用 `**`
    assert not core._metric_match("disk.*.percent", "disk.inode.root.percent")
    assert core._metric_match("disk.**.percent", "disk.inode.root.percent")
    assert core._metric_match("disk.data*.percent", "disk.data1.percent")


def test_wildcard_metrics_uses_real_names():
    """枚举结果必须是真实指标名（且只命中模式匹配的）。"""
    from app.engine import core
    from app.store import ring

    ident = "d150-listing-host"
    now = datetime.utcnow()
    for name in ("disk.alpha.percent", "disk.beta.percent", "cpu.percent", "disk.alpha_iops"):
        ring.append_point(ident, name, now, 10.0)
    names = __import__("asyncio").run(core._wildcard_metrics(
        ident, "disk.*.percent", now - timedelta(minutes=5), now + timedelta(minutes=1)))
    assert "disk.alpha.percent" in names and "disk.beta.percent" in names
    assert "cpu.percent" not in names and "disk.alpha_iops" not in names


@pytest.mark.asyncio
async def test_rule_test_endpoint_reports_each_matched_metric():
    """「测试运行」对通配规则也要逐指标返回（不能只返回模式本身）。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.main import app
    from app.models.alert import AlertRule
    from app.models.host import Host
    from app.store import ring

    async with SessionLocal() as db:
        host = (await db.execute(select(Host).limit(1))).scalars().first()
        assert host is not None, "测试库应至少有一台主机"
        ident = host.ident
        tag = uuid.uuid4().hex[:6]
        name = f"d150-wild-{tag}"
        rule = AlertRule(name=name, scope="metric", metric=f"disk.d150{tag}*.percent",
                         expr=">50", severity=2, window_s=600, eval_interval_s=30)
        db.add(rule)
        await db.commit()
        rid = rule.id
    now = datetime.utcnow()
    for m in (f"disk.d150{tag}a.percent", f"disk.d150{tag}b.percent"):
        ring.append_point(ident, m, now, 90.0)
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as ac:
            tok = (await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["data"]["access_token"]
            d = (await ac.post(f"/api/ops/alert-rules/{rid}/test",
                               headers={"Authorization": f"Bearer {tok}"})).json()["data"]
        metrics = {x["metric"] for x in d["matched"]}
        assert f"disk.d150{tag}a.percent" in metrics, f"未逐指标返回：{metrics}"
        assert f"disk.d150{tag}b.percent" in metrics, f"未逐指标返回：{metrics}"
    finally:
        async with SessionLocal() as db:
            row = (await db.execute(select(AlertRule).where(AlertRule.id == rid))).scalar_one_or_none()
            if row is not None:
                await db.delete(row)
                await db.commit()


def test_engine_emits_per_metric_event_and_ui_hint():
    src = (ROOT / "app" / "engine" / "core.py").read_text(encoding="utf-8")
    assert "_wildcard_metrics" in src and "_has_wildcard" in src
    assert '{"host": h.ident, "metric": metric_name}' in src, \
        "通配规则必须用真实指标名构造 anomaly（否则多块盘共用一个指纹）"
    js = (ROOT.parent / "static" / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
    assert "disk.*.percent" in js, "规则编辑器必须提示通配符用法"
