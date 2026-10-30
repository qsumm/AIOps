"""D120 回归锁：重注册必须刷新 Host.ip。

历史缺陷：ip 只在首次注册建行时写入（ingest.register_agent 的 Host(...) 构造），
已注册主机的重注册不更新——老主机（Agent 升级到带 _detect_ip 的版本）重启后
IP 依旧是空串，界面永远显示 "-"。修复后：重注册携带非空 ip 即刷新。
"""
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.host import Host
from app.services import ingest

FP = "test-fp-d120"


async def _rm(db, ident):
    h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if h is not None:
        await db.delete(h)
        await db.commit()


@pytest.mark.asyncio
async def test_reregister_refreshes_ip():
    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d120", "install_token": tok})
        ident = data["ident"]
        h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        assert h.ip == ""                      # 旧版 Agent 不带 ip → 空（历史现场）

        # 升级后的 Agent 重启重注册：携带真实 IP → 必须补上
        await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d120", "ip": "1.2.3.21"})
        await db.refresh(h)
        assert h.ip == "1.2.3.21", "重注册必须刷新 Host.ip（D120）"

        # IP 变化（DHCP 场景）→ 跟随更新
        await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d120", "ip": "1.2.3.22"})
        await db.refresh(h)
        assert h.ip == "1.2.3.22"

        # 空 ip 上报不得把已有值清掉
        await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d120", "ip": ""})
        await db.refresh(h)
        assert h.ip == "1.2.3.22"
        await _rm(db, ident)


@pytest.mark.asyncio
async def test_peer_ip_fills_empty_host():
    """NAT/容器网段内的 agent 本机探测拿不到公网 IP → 平台用上报对端 IP 补（metrics/心跳/注册三路）。"""
    async with SessionLocal() as db:
        # 1) 注册无 ip + 对端补
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(db, {"fingerprint": "test-fp-d120b", "hostname": "d120b",
                                                "install_token": tok},
                                           peer_ip="1.2.3.4")
        h = (await db.execute(select(Host).where(Host.ident == data["ident"]))).scalar_one()
        assert h.ip == "1.2.3.4"
        await _rm(db, h.ident)
        # 2) 老主机 ip 空 → ingest_metrics 对端补
        tok2 = ingest.mint_install_token()
        data2 = await ingest.register_agent(db, {"fingerprint": "test-fp-d120c", "hostname": "d120c", "install_token": tok2})
        h2 = (await db.execute(select(Host).where(Host.ident == data2["ident"]))).scalar_one()
        assert h2.ip == ""
        await ingest.ingest_metrics(db, h2.ident, data2["token"],
                                    {"ts": "2026-10-19T12:00:00+00:00",
                                     "metrics": [{"metric": "cpu.percent", "value": 5.0}]},
                                    peer_ip="5.6.7.8")
        await db.refresh(h2)
        assert h2.ip == "5.6.7.8", "metrics 上报周期即补对端 IP（用户无需重启 agent）"
        await _rm(db, h2.ident)


@pytest.mark.asyncio
async def test_public_ip_wins_over_private_selfreport():
    """NAT 机自报私网地址（172.17.0.13）+ 平台看到公网对端（1.2.3.10）→ 公网赢。"""
    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(
            db, {"fingerprint": "test-fp-d120d", "hostname": "d120d", "install_token": tok,
                 "ip": "172.17.0.13"}, peer_ip="1.2.3.10")
        h = (await db.execute(select(Host).where(Host.ident == data["ident"]))).scalar_one()
        assert h.ip == "1.2.3.10", "公网对端必须覆盖 agent 的私网自报"
        # metrics 周期同样校正
        await ingest.ingest_metrics(db, h.ident, data["token"],
                                    {"ts": "2026-10-19T12:30:00+00:00",
                                     "metrics": [{"metric": "cpu.percent", "value": 3.0}]},
                                    peer_ip="1.2.3.10")
        await db.refresh(h)
        assert h.ip == "1.2.3.10"
        # 纯内网部署：peer 也是私网 → 不覆盖 agent 自报（同级按自报优先）
        await ingest.ingest_metrics(db, h.ident, data["token"],
                                    {"ts": "2026-10-19T12:31:00+00:00",
                                     "metrics": [{"metric": "cpu.percent", "value": 3.0}]},
                                    peer_ip="10.0.0.9")
        await db.refresh(h)
        assert h.ip == "1.2.3.10", "私网 peer 不得覆盖已有公网 IP"
        await _rm(db, h.ident)


def test_pick_ip_unit():
    from app.services.ingest import _pick_ip
    assert _pick_ip("172.17.0.13", "172.17.0.13", "1.2.3.10") == "1.2.3.10"
    assert _pick_ip("", "", "1.2.3.10") == "1.2.3.10"
    assert _pick_ip("1.2.3.10", "", "10.0.0.9") == "1.2.3.10"      # 公网旧值不被私网 peer 覆盖
    assert _pick_ip("", "203.0.113.7", "198.51.100.9") == "203.0.113.7"  # 同公网：自报优先
    assert _pick_ip("", "", "") == ""