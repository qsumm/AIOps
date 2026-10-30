"""D30：manual_split 换锚的唯一约束冲突回归。

缺陷（用户实测：事件拆分失败）：被拆出的告警恰是源事件的 source_key 锚点时，
源事件需换锚到剩余成员；原实现直接取 hashes[0]，但该成员可能**已被另一个事件
占为 source_key**（实测：多个事件的成员 hash 与别的 source_key 重叠），
直接改锚撞唯一约束 → IntegrityError → HTTP 409「同名资源已存在」。
"""
import hashlib

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models.event import Incident
from app.services import cluster as cluster_svc

PRE = "d30-"


def _h(n: str) -> str:
    return hashlib.sha256(f"{PRE}{n}".encode()).hexdigest()[:32]


@pytest.mark.asyncio
async def test_manual_split_reanchor_no_conflict():
    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.title.like(f"{PRE}%")))
        await db.commit()
        a = Incident(title=f"{PRE}A", severity=2, status="open",
                     alert_hashes=[_h("1"), _h("2")], source_key=_h("1"), host_ident="d30-host")
        b = Incident(title=f"{PRE}B", severity=2, status="open",
                     alert_hashes=[_h("2")], source_key=_h("2"), host_ident="d30-host")
        db.add_all([a, b])
        await db.commit()
        await db.refresh(a)
        await db.refresh(b)
        aid, bid = a.id, b.id
    try:
        # 拆 A 的锚点 h1；A 剩余唯一成员 h2 正被 B 占用 → 旧实现必 409
        r = await cluster_svc.manual_split(aid, _h("1"))
        assert r.get("ok"), f"拆分应成功（此前撞唯一约束 409）：{r}"
        async with SessionLocal() as db:
            a2 = await db.get(Incident, aid)
            b2 = await db.get(Incident, bid)
            new = await db.get(Incident, r["new"])
            assert new is not None and new.source_key == _h("1"), "拆出的事件应以被拆告警为锚"
            assert a2.source_key == _h("2"), "源事件应换锚到剩余成员 h2"
            assert b2.source_key.startswith("cl:"), "占用者 B 应被 tombstone 释放锚点"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Incident).where(Incident.title.like(f"{PRE}%")))
            await db.commit()


@pytest.mark.asyncio
async def test_manual_split_picks_free_anchor_when_available():
    """剩余成员里若有未被占用的，直接选它，不动别的事件。"""
    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.title.like(f"{PRE}%")))
        await db.commit()
        a = Incident(title=f"{PRE}C", severity=2, status="open",
                     alert_hashes=[_h("x"), _h("y"), _h("z")], source_key=_h("x"), host_ident="d30-host")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        aid = a.id
    try:
        r = await cluster_svc.manual_split(aid, _h("x"))
        assert r.get("ok")
        async with SessionLocal() as db:
            a2 = await db.get(Incident, aid)
            assert a2.source_key in (_h("y"), _h("z")), "应换锚到未被占用的剩余成员"
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Incident).where(Incident.title.like(f"{PRE}%")))
            await db.commit()
