"""D159：缺陷 D158-1 修复守卫——带维度序列必须真落库 `metric_series`。

D154 建了表、接了读回落（metrics 路由 / rca_tools），但全库没有写入方
（D158 文档轮「写方 ↔ 读方」机械对账发现）。本套件锁死修复后的行为与接线：
  ① `flush_series` 把 `_series` 的新点写进表（tags_key / tags 原样往返）；
  ② 二次 flush 不产生重复行（水位语义）；
  ③ 比水位旧的点不再补写；
  ④ sched 的 60s 聚合节拍里调用了 `flush_series`（漏接线即红）。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from sqlalchemy import text

# 每个用例用独立 ident + 独立维度键：ring._series 与水位是进程级全局，隔离靠键唯一
IDENT_A = "d159-flush-a"
IDENT_B = "d159-flush-b"
IDENT_C = "d159-flush-c"


async def _cleanup(db, ident: str) -> None:
    await db.execute(text("DELETE FROM metric_series WHERE ident = :i"), {"i": ident})


def test_flush_series_writes_rows_with_tags_roundtrip():
    """① 落库：点位入库、tags_key 规范序、tags JSON 原样往返、值/时间不漂。"""
    from app.db.session import SessionLocal
    from app.store import ring

    ts0 = datetime.utcnow().replace(microsecond=0) - timedelta(minutes=10)
    tags = {"state": "active", "datname": "orders"}
    ring.append_point(IDENT_A, "pg.connections.current", ts0, 5, tags=tags)
    ring.append_point(IDENT_A, "pg.connections.current", ts0 + timedelta(seconds=15), 7, tags=tags)

    async def m():
        async with SessionLocal() as db:
            await _cleanup(db, IDENT_A)
            await db.commit()
            n = await ring.flush_series(db)
            assert n >= 1, "flush_series 未写入任何行"
            rows = (await db.execute(text(
                "SELECT tags_key, tags, value, ts FROM metric_series "
                "WHERE ident = :i AND metric = 'pg.connections.current' ORDER BY ts"),
                {"i": IDENT_A})).fetchall()
            assert len(rows) >= 2, f"应有 ≥2 行维度序列，实际 {len(rows)}"
            assert rows[0][0] == "datname=orders;state=active", "tags_key 必须是键名排序的规范串"
            assert json.loads(rows[0][1]) == {"datname": "orders", "state": "active"}
            assert float(rows[0][2]) == 5.0
            await _cleanup(db, IDENT_A)
            await db.commit()
    asyncio.run(m())


def test_second_flush_is_idempotent():
    """② 水位语义：同一序列再 flush 不产生重复行。"""
    from app.db.session import SessionLocal
    from app.store import ring

    ts0 = datetime.utcnow().replace(microsecond=0) - timedelta(minutes=8)
    ring.append_point(IDENT_B, "disk.percent", ts0, 81.5, tags={"device": "/dev/sda1"})

    async def m():
        async with SessionLocal() as db:
            await _cleanup(db, IDENT_B)
            await db.commit()
            await ring.flush_series(db)
            cnt1 = (await db.execute(text(
                "SELECT COUNT(*) FROM metric_series WHERE ident = :i"), {"i": IDENT_B})).scalar()
            n2 = await ring.flush_series(db)
            cnt2 = (await db.execute(text(
                "SELECT COUNT(*) FROM metric_series WHERE ident = :i"), {"i": IDENT_B})).scalar()
            assert n2 == 0, f"二次 flush 应为 0 行，实际写入 {n2}"
            assert cnt1 == cnt2 == 1, f"行数应恒为 1，实际 {cnt1} → {cnt2}"
            await _cleanup(db, IDENT_B)
            await db.commit()
    asyncio.run(m())


def test_watermark_skips_older_points():
    """③ 比水位旧的迟到点不再补写（与 flush_aggregates 同款取舍）。"""
    from app.db.session import SessionLocal
    from app.store import ring

    ts0 = datetime.utcnow().replace(microsecond=0) - timedelta(minutes=6)
    ring.append_point(IDENT_C, "redis.hits", ts0, 100, tags={"db": "cache"})

    async def m():
        async with SessionLocal() as db:
            await _cleanup(db, IDENT_C)
            await db.commit()
            await ring.flush_series(db)
            # 迟到旧点：ts 早于刚推进的水位 → 不写
            ring.append_point(IDENT_C, "redis.hits", ts0 - timedelta(minutes=5), 90, tags={"db": "cache"})
            n = await ring.flush_series(db)
            assert n == 0, f"水位之后不应再写旧点，实际写入 {n}"
            vals = [float(r[0]) for r in (await db.execute(text(
                "SELECT value FROM metric_series WHERE ident = :i"), {"i": IDENT_C})).fetchall()]
            assert vals == [100.0], f"表内应只有首个点 100，实际 {vals}"
            await _cleanup(db, IDENT_C)
            await db.commit()
    asyncio.run(m())


def test_sched_aggregate_job_calls_flush_series():
    """④ 接线：60s 聚合节拍必须同时刷维度序列（D158-1 的根因就是没人调）。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "sched.py"
    text_src = src.read_text(encoding="utf-8")
    assert "ring.flush_series(db)" in text_src, "sched.py 必须调用 ring.flush_series(db)（job_aggregate 节拍）"
    assert "job_aggregate" in text_src
