"""D125 静默失败可观测性回归锁（三条来自夜莺 Categraf 学习）。

  ① install.sh：并发原子锁 + 覆盖前配置备份（批量装机防互踩/防丢用户探针配置）
  ② install.sh：注册确认失败给**可执行的三步排查**，并检查服务是否真的在跑
     （夜莺范本："绿色的服务却从不出现在 UI 里，是最坏的结果"）
  ③ 主机列表暴露 has_metrics —— status=up 但无任何指标时给出「无指标」黄徽章
  ④ agent 打印「首个上报已确认」——注册成功 ≠ 在采集上报，要有独立端到端信号
"""
import os

import pytest
from sqlalchemy import select

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INSTALL_SH = os.path.join(ROOT, "agent", "install.sh")
AGENT_PY = os.path.join(ROOT, "agent", "agent_main.py")


def _read(p: str) -> str:
    return open(p, encoding="utf-8").read()


# ---------- ① 锁与备份 ----------
def test_install_has_atomic_lock():
    src = _read(INSTALL_SH)
    assert "mkdir \"$LOCK\"" in src, "必须用 mkdir 原子锁（批量装机防互踩）"
    assert ".install.lock" in src
    assert "rmdir \"$LOCK\"" in src, "锁必须被 trap 清理（异常退出不留死锁）"


def test_install_backs_up_user_config_before_overwrite():
    src = _read(INSTALL_SH)
    assert "conf-backup-" in src, "覆盖前必须备份用户配置（探针声明是用户反复调过的东西）"
    assert "probes.json" in src and "cp -p" in src


# ---------- ② 排查指引与服务状态 ----------
def test_install_verify_has_actionable_triage_and_service_check():
    src = _read(INSTALL_SH)
    assert "service_running()" in src, "verify 必须检查服务托管是否真的在跑"
    assert "系统未运行" not in src  # 文案断言过严，改为下面的具体项
    assert "服务未运行" in src or "服务没有在运行" in src
    for step in ("systemctl status", "curl -I", "接入新主机"):
        assert step in src, f"排查指引缺少步骤线索：{step}"


# ---------- ④ 端到端上报确认信号 ----------
def test_agent_logs_first_report_confirmation():
    src = _read(AGENT_PY)
    assert "首个上报已确认" in src, "必须有独立的『首个上报被平台接受』信号（安装校验认它）"


# ---------- ③ has_metrics 序列化 ----------
@pytest.mark.asyncio
async def test_host_list_exposes_has_metrics():
    from httpx import ASGITransport, AsyncClient

    from app.db.session import SessionLocal
    from app.main import app
    from app.services import ingest

    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(db, {"fingerprint": "test-fp-d125", "hostname": "d125",
                                                "install_token": tok, "ip": "9.9.9.9"})
        ident = data["ident"]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            r = await ac.get("/api/ops/hosts", headers=hdr)
            row = next((x for x in r.json()["data"]["items"] if x["ident"] == ident), None)
            assert row is not None
            assert "has_metrics" in row
            assert row["has_metrics"] is False, "新注册主机尚无指标点 → has_metrics=False（列表显示「无指标」）"
            # 上报一个指标后应变为 True
            async with SessionLocal() as db:
                await ingest.ingest_metrics(db, ident, data["token"],
                                             {"ts": "2026-10-19T12:00:00+00:00",
                                              "metrics": [{"metric": "cpu.percent", "value": 5.0}]},
                                             peer_ip="9.9.9.9")
            r = await ac.get("/api/ops/hosts", headers=hdr)
            row = next(x for x in r.json()["data"]["items"] if x["ident"] == ident)
            assert row["has_metrics"] is True
    finally:
        from app.models.host import Host
        async with SessionLocal() as db:
            h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
            if h is not None:
                await db.delete(h)
                await db.commit()