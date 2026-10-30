"""P0：Agent 认证自愈与自监控回归锁。

背景（实测定根因）：
  · Agent 运行中拿到 401 后原实现静默重试，平台只看到 last_beat 变旧，
    无法区分“机器关机”和“凭证失效”；
  · 主机详情没有认证状态/最近成功上报/缓存积压/最近错误，排障只能翻 agent.log。

锁三件事：
  ① Agent 源码：401 置 auth 失败、主循环重注册、403 进入 needs_reinstall、上报带 agent_meta；
  ② 平台：错误 token 把 host.auth_state 置 auth_failed；正确 token 恢复 ok 并吸收自监控字段；
  ③ 主机列表/详情接口把健康字段带回。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.main import create_app
from app.models.host import Host

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent" / "agent_main.py"


def test_agent_has_auth_self_healing_and_meta():
    s = AGENT.read_text(encoding="utf-8")
    # D125：版本锁不再硬编码具体版本（每次 bump 必漏三处）——改为断言**与平台一致**：
    # agent 文件里的 AGENT_VERSION 必须等于平台侧 AGENT_LATEST_VERSION。
    from app.services import ingest as _ing
    m = re.search(r'AGENT_VERSION\s*=\s*"([^"]+)"', s)
    assert m, "agent 必须声明 AGENT_VERSION"
    assert m.group(1) == _ing.AGENT_LATEST_VERSION, (
        f"agent 版本 {m.group(1)} 与平台 AGENT_LATEST_VERSION {_ing.AGENT_LATEST_VERSION} 不一致——"
        "发布新 agent 时两个版本号必须一起改")
    assert "status_code == 401" in s, "必须显式处理 401，不能静默重试"
    assert "_mark_auth_failure" in s and "AuthExpired" in s, "401 需要进入认证失效状态"
    assert "needs_reinstall" in s, "403 需要明确进入重新接入状态"
    assert "_agent_meta" in s and "cache_depth" in s, "上报需携带自监控字段"
    assert "重新注册成功" in s, "401 后应尝试重新注册"


@pytest.mark.asyncio
async def test_bad_token_marks_auth_failed_then_good_token_recovers():
    fp = "p0-agent-health-test"
    ident = hashlib.sha256(fp.encode()).hexdigest()[:32]
    app = create_app()
    try:
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
            db.add(Host(ident=ident, name="P0-健康测试", status="up", agent_token="good-token",
                        last_beat=datetime.utcnow(), auth_state="unknown"))
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            # ① 错误 token：401，且主机行记录 auth_failed（不再只是“心跳变旧”）
            r = await ac.post("/api/ops/agents/metrics", json={"metrics": []},
                              headers={"X-Ident": ident, "X-Token": "bad-token"})
            assert r.status_code == 401
            async with SessionLocal() as db:
                h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
                assert h.auth_state == "auth_failed"
                assert "凭证失效" in (h.last_agent_error or "")

            # ② 正确 token + agent_meta：认证恢复，缓存/上报时间落库
            # D155 语义变更：`last_agent_error` 从「**最后一次**错误（历史）」改成
            # 「**当前仍存在**的错误」。依据：Agent 的 agent_meta 是在**发请求之前**
            # 构造的——它带着上一轮的错误来，而这一轮成功了，就证明上一轮的错误已经过去
            # （Agent 自己也是成功后才清自己的副本）。所以「平台收到了成功上报」本身就是
            # 最可靠的恢复证据，平台据此清错。
            # 为什么必须这样：否则同一屏上会同时出现「状态 在线 / 认证正常 /
            # 最近成功上报 xx:xx / 最近错误 403」——用户无从判断此刻到底有没有问题
            # （真实场景：主机被下线 10 小时，恢复后界面仍红）。
            # 历史的 Agent 错误不丢：Agent 自己的日志里有，平台的审计日志记平台侧动作。
            payload = {
                "metrics": [{"metric": "cpu.percent", "value": 12.5}],
                "ts": datetime.utcnow().isoformat() + "Z",
                "agent_meta": {"version": "0.3.2", "auth_state": "ok",
                               "cache_depth": 7, "last_error": "上一次网络抖动"},
            }
            r = await ac.post("/api/ops/agents/metrics", json=payload,
                              headers={"X-Ident": ident, "X-Token": "good-token"})
            assert r.status_code == 204, r.text[:200]
            async with SessionLocal() as db:
                h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
                assert h.auth_state == "ok"
                assert h.cache_depth == 7
                assert h.agent_version == "0.3.2"
                # 成功上报 → 上一轮那个错误已过去 → 清空（见上面的语义说明）
                assert h.last_agent_error == "", "成功上报后应清掉上一轮的错误"
                assert h.last_report_ok_at is not None
                assert h.last_auth_ok_at is not None

            # ③ 列表/详情接口必须把这些字段带回前端
            lg = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            tok = lg.json()["data"]["access_token"]
            H = {"Authorization": f"Bearer {tok}"}
            r = await ac.get("/api/ops/hosts", headers=H)
            item = next(x for x in r.json()["data"]["items"] if x["ident"] == ident)
            assert item["auth_state"] == "ok"
            assert item["cache_depth"] == 7
            assert item["last_report_ok_at"]
            r = await ac.get(f"/api/ops/hosts/{ident}", headers=H)
            d = r.json()["data"]
            assert d["auth_state"] == "ok"
            # D155：成功上报后错误已清空（语义见上面 ② 的说明）→ 出参也应为空
            assert d["last_agent_error"] == ""
    finally:
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
            await db.commit()
        app.dependency_overrides.clear()


def test_compose_passes_root_password_to_seekdb():
    """D114（S1 部署事故回归锁）：seekdb 1.4 start.sh 读 ROOT_PASSWORD，不是 OB_ROOT_PASSWORD。

    只传 OB_* 时，重建已有数据目录会拿空密码校验 → 无限重启。compose 必须两个都传。
    """
    from pathlib import Path

    s = (Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "ROOT_PASSWORD=${DB_PASSWORD" in s
    assert "OB_ROOT_PASSWORD=${DB_PASSWORD" in s
