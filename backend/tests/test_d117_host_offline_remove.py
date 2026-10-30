"""主机管理「下线 / 移出」回归锁（D117）。

链路：ingest.auth_agent 拦截 offline/removed（403）→ heartbeat/metrics 停止；
register_agent 复位标记（重新接入=复原）；removed 主机重注册要求重新签发安装令牌。
下线/移出走 status=down → 探活/告警/自愈按 status=="up" 处理即自动跳过。
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.host import Host
from app.services import ingest

FP = "test-fp-d117"


async def _get(db, ident):
    return (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()


@pytest.mark.asyncio
async def test_offline_blocks_report_and_register_restores():
    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d117", "install_token": tok})
        ident = data["ident"]
        # 正常上报不受影响
        await ingest.ingest_metrics(db, ident, data["token"], {"ts": "2026-10-19T10:00:00+00:00",
            "metrics": [{"metric": "cpu.percent", "value": 12.3}]})
        h = await _get(db, ident)
        assert h.offline is False and h.removed is False and h.status == "up"

        # 下线 → metrics / heartbeat 一律 403
        h.offline = True
        h.status = "down"
        await db.commit()
        with pytest.raises(HTTPException) as ei:
            await ingest.ingest_metrics(db, ident, data["token"], {"ts": "2026-10-19T10:01:00+00:00",
                "metrics": [{"metric": "cpu.percent", "value": 99.0}]})
        assert ei.value.status_code == 403
        with pytest.raises(HTTPException) as ei2:
            await ingest.heartbeat(db, ident, data["token"], {})
        assert ei2.value.status_code == 403
        await db.refresh(h)
        assert h.status == "down" and h.offline is True   # 未被拉回 up

        # 重新接入（register 对非 removed 免令牌）→ 复位 offline / status
        await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d117"})
        await db.refresh(h)
        assert h.offline is False and h.status == "up"

        await db.delete(h)
        await db.commit()


@pytest.mark.asyncio
async def test_removed_requires_new_install_token():
    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        data = await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d117r", "install_token": tok})
        ident = data["ident"]
        h = await _get(db, ident)
        h.removed = True
        h.offline = True
        h.status = "down"
        await db.commit()

        with pytest.raises(HTTPException) as ei:
            await ingest.ingest_metrics(db, ident, data["token"], {"ts": "2026-10-19T10:02:00+00:00",
                "metrics": [{"metric": "cpu.percent", "value": 1.0}]})
        assert ei.value.status_code == 403

        # removed 主机重注册必须重新签发一次性安装令牌（旧 token 无效）
        with pytest.raises(HTTPException):
            await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d117r"})

        tok2 = ingest.mint_install_token()
        await ingest.register_agent(db, {"fingerprint": FP, "hostname": "d117r", "install_token": tok2})
        await db.refresh(h)
        assert h.removed is False and h.offline is False and h.status == "up"

        await db.delete(h)
        await db.commit()