"""P0：告警新鲜度（僵尸 active 行的统一判定与清理）。

问题不是“告警不会恢复”，而是**评估停了但行还在**：
  · 规则停用/删除后 worker 停止，alert_cur_events 里的行没人清；
  · 平台重启后回放旧行，规则在 effective_time 外或数据源不可达时不再评估；
  · 界面持续显示 firing，事件收敛/诊断把它当当前证据（L3b/L4a 误诊实测）。

约定：
  · last_eval_at 只在**规则成功评估**后刷新；评估停了就不再刷新；
  · stale=True 的行不参与通知/聚类/诊断，界面显示“数据陈旧”；
  · 规则恢复评估后 touch_rule_evaluated 会把 stale 清掉。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.models.alert import AlertCurEvent, AlertRule

DEFAULT_STALE_AFTER_S = 300   # 无规则可查（已删除）时的兜底
MIN_STALE_AFTER_S = 60
EVAL_MULTIPLIER = 4           # 4 个评估周期没刷新即陈旧（覆盖短暂抖动）


def stale_after_s(rule: AlertRule | None) -> int:
    if rule is None:
        return DEFAULT_STALE_AFTER_S
    try:
        interval = int(rule.eval_interval_s or 15)
    except (TypeError, ValueError):
        interval = 15
    return max(MIN_STALE_AFTER_S, interval * EVAL_MULTIPLIER, DEFAULT_STALE_AFTER_S)


def alert_age_ref(row: AlertCurEvent) -> datetime | None:
    """判断新鲜度的参照时间：优先 last_eval_at，老数据回落 trigger_time。"""
    return row.last_eval_at or row.trigger_time


def is_fresh(row: AlertCurEvent, now: datetime | None = None,
             rules: dict[int, AlertRule] | None = None) -> bool:
    if getattr(row, "stale", False):
        return False
    now = now or datetime.utcnow()
    ref = alert_age_ref(row)
    if ref is None:
        return False
    limit = stale_after_s((rules or {}).get(row.rule_id))
    return ref >= now - timedelta(seconds=limit)


async def touch_rule_evaluated(db, rule_id: int, now: datetime | None = None) -> int:
    """规则本轮成功评估：刷新该规则所有活跃行的 last_eval_at，并解除 stale。"""
    now = now or datetime.utcnow()
    r = await db.execute(
        update(AlertCurEvent)
        .where(AlertCurEvent.rule_id == rule_id)
        .values(last_eval_at=now, stale=False, stale_reason="")
    )
    await db.commit()
    return int(r.rowcount or 0)


async def mark_rule_stale(db, rule_id: int, reason: str) -> int:
    """规则停用/删除/不在生效时段：把该规则所有活跃行标记陈旧（幂等）。"""
    r = await db.execute(
        update(AlertCurEvent)
        .where(AlertCurEvent.rule_id == rule_id, AlertCurEvent.stale.is_(False))
        .values(stale=True, stale_reason=str(reason)[:64])
    )
    await db.commit()
    return int(r.rowcount or 0)


async def mark_stale_alerts(db, now: datetime | None = None) -> int:
    """全局扫描：last_eval_at 超过“4 个评估周期（至少 300s）”未刷新的行 → stale。

    由调度器每 30s 调用；只处理 stale=False 的行，幂等。
    """
    now = now or datetime.utcnow()
    rules = {r.id: r for r in (await db.execute(select(AlertRule))).scalars().all()}
    rows = (await db.execute(
        select(AlertCurEvent).where(AlertCurEvent.stale.is_(False))
    )).scalars().all()
    n = 0
    for row in rows:
        if is_fresh(row, now, rules):
            continue
        row.stale = True
        row.stale_reason = "no_evaluation"
        n += 1
    if n:
        await db.commit()
    return n
