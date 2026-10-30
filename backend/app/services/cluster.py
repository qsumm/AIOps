# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""事件收敛聚类（FR-D1；03 §1.12）。

两级：
  一级 规则聚类（免 LLM）：key = (host_ident, 规则族)，30 分钟窗口内找 open incident
  二级 LLM 因果判定（可降级）：YES 并入 / NO 新建；LLM 不可用 → 规则兜底直接并入（标记 rule）
source_key = 首告警指纹（唯一约束保证幂等：同一告警重复处理不会建两个 incident）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.event import Incident
from app.services import prompts
from app.services.llm_gateway import call_llm

log = logging.getLogger("cluster")

WINDOW_MIN = 30


def rule_family(rule_name: str, metric: str) -> str:
    """规则族：显式 cluster_group（此处取规则名前缀）优先，否则按指标名前缀。"""
    base = (rule_name or "").split("-")[0].strip() or metric
    return base


def _parse_verdict(text: str) -> tuple[bool, str]:
    t = (text or "").strip()
    yes = t.upper().startswith("YES") or "YES" in t.upper()[:12]
    reason = t.split("-", 1)[1].strip() if "-" in t else t[:60]
    return yes, reason


def _tombstone(source_key: str, incident_id: int) -> str:
    """已关闭事件的锚点墓碑：释放 source_key 供后续同告警重新触发时新建活跃事件。

    source_key 为 CHAR(32) 且唯一——刻意保留唯一约束，但语义收紧为"**活跃事件的锚点**"。
    否则一条告警恢复后再触发，会被幂等判定错误并入已解决事件（D4 实测发现）。
    """
    keep = (source_key or "")[:20]
    return f"cl:{incident_id}:{keep}"[:32]


async def _release_anchor(db, inc: Incident) -> None:
    if inc.source_key and not inc.source_key.startswith("cl:"):
        inc.source_key = _tombstone(inc.source_key, inc.id)


async def cluster_alert(alert: dict, *, use_llm: bool | None = None) -> dict:
    """把一条告警收敛进/新建 incident。返回 {incident_id, merged, by, reason}。

    use_llm 缺省取配置 cluster_llm_enabled；单测/CI 关闭以避免真实 LLM 调用与耗时。
    """
    if use_llm is None:
        use_llm = get_settings().cluster_llm_enabled
    async with SessionLocal() as db:
        # 幂等：source_key 已存在且事件仍活跃 → 直接返回既有 incident
        existed = (await db.execute(
            select(Incident).where(Incident.source_key == alert["hash"])
        )).scalar_one_or_none()
        if existed and existed.status != "resolved":
            return {"incident_id": existed.id, "merged": False, "by": "idempotent", "reason": "该告警已是事件锚点"}
        if existed:  # 锚点被已关闭事件占着：释放后按新事件处理（恢复后再触发 = 新事件）
            await _release_anchor(db, existed)
            await db.commit()

        family = rule_family(alert.get("rule_name", ""), alert.get("metric", ""))
        since = datetime.utcnow() - timedelta(minutes=WINDOW_MIN)
        cands = (await db.execute(
            select(Incident).where(
                Incident.status != "resolved",
                Incident.created_at >= since,
                Incident.host_ident == alert.get("host_ident", ""),
            ).order_by(Incident.created_at.desc())
        )).scalars().all()

        if not cands:
            inc = await _create(db, alert, reason=f"规则聚类：窗口内无同类事件（族={family}）")
            return {"incident_id": inc.id, "merged": False, "by": "rule", "reason": inc.cluster_reason}

        target = cands[0]
        if not use_llm:
            await _merge(db, target, alert, by="rule")
            return {"incident_id": target.id, "merged": True, "by": "rule", "reason": "LLM 关闭，规则兜底并入"}

        try:
            verdict = await call_llm(
                db, "summary",
                prompts.cluster_prompt(
                    {"title": target.title, "severity": target.severity, "created_at": str(target.created_at),
                     "members_desc": f"{len(target.alert_hashes or [])} 条，含 {target.title}"},
                    alert,
                ),
                model_role="fast",
            )
            yes, reason = _parse_verdict(verdict.content)
        except Exception as e:  # noqa: BLE001 —— LLM 不可用 → 规则兜底
            log.warning("收敛 LLM 判定不可用，规则兜底：%s", e)
            await _merge(db, target, alert, by="rule")
            return {"incident_id": target.id, "merged": True, "by": "rule", "reason": f"LLM 不可用兜底并入（{type(e).__name__}）"}

        if yes:
            await _merge(db, target, alert, by="llm", reason=reason)
            return {"incident_id": target.id, "merged": True, "by": "llm", "reason": reason}
        inc = await _create(db, alert, reason=f"LLM 判定非同源：{reason}")
        return {"incident_id": inc.id, "merged": False, "by": "llm", "reason": reason}


async def _create(db, alert: dict, reason: str) -> Incident:
    inc = Incident(
        title=f"{alert.get('rule_name') or alert.get('metric')} · {alert.get('host_ident', '')[:8]}",
        severity=int(alert.get("severity") or 3),
        status="open",
        alert_hashes=[alert["hash"]],
        source_key=alert["hash"],
        host_ident=alert.get("host_ident", ""),
        cluster_reason=reason,
    )
    db.add(inc)
    try:
        await db.commit()
    except IntegrityError:  # 并发下同一 source_key 已建
        await db.rollback()
        return (await db.execute(select(Incident).where(Incident.source_key == alert["hash"]))).scalar_one()
    await db.refresh(inc)
    return inc


async def _merge(db, inc: Incident, alert: dict, by: str, reason: str = "") -> None:
    hashes = list(inc.alert_hashes or [])
    if alert["hash"] not in hashes:
        hashes.append(alert["hash"])
    inc.alert_hashes = hashes
    inc.severity = min(int(inc.severity or 3), int(alert.get("severity") or 3))  # 取更严重
    inc.cluster_reason = f"merged_by={by}" + (f"：{reason}" if reason else "")
    inc.updated_at = datetime.utcnow()
    await db.commit()


# ---------- 手动合并 / 拆分（05 §4） ----------
async def manual_merge(keep_id: int, merge_id: int) -> dict:
    async with SessionLocal() as db:
        keep = await db.get(Incident, keep_id)
        drop = await db.get(Incident, merge_id)
        if keep is None or drop is None:
            return {"ok": False, "msg": "NOT_FOUND"}
        hashes = list(keep.alert_hashes or [])
        for h in (drop.alert_hashes or []):
            if h not in hashes:
                hashes.append(h)
        keep.alert_hashes = hashes
        keep.cluster_reason = f"manual_merge(合并自 #{drop.id})"
        keep.updated_at = datetime.utcnow()
        # 被并事件不删除：标记 resolved 并留痕（可追溯）；同时释放其锚点
        drop.status = "resolved"
        drop.root_cause = (drop.root_cause or "") + f"\n[手动合并] 已并入事件 #{keep.id}"
        drop.resolved_at = datetime.utcnow()
        await _release_anchor(db, drop)
        await db.commit()
        return {"ok": True, "keep": keep.id, "merged": drop.id, "members": len(hashes)}


async def manual_split(incident_id: int, hash_to_split: str) -> dict:
    async with SessionLocal() as db:
        inc = await db.get(Incident, incident_id)
        if inc is None:
            return {"ok": False, "msg": "NOT_FOUND"}
        hashes = list(inc.alert_hashes or [])
        if hash_to_split not in hashes:
            return {"ok": False, "msg": "ALERT_NOT_IN_INCIDENT"}
        # 该告警指纹可能被别的（已关闭）事件占为锚点 → 先释放，再据其建新事件
        holder = (await db.execute(select(Incident).where(
            Incident.source_key == hash_to_split, Incident.id != incident_id))).scalar_one_or_none()
        if holder is not None:
            await _release_anchor(db, holder)
            await db.flush()
        if len(hashes) <= 1:
            return {"ok": False, "msg": "ONLY_MEMBER_CANNOT_SPLIT"}
        hashes.remove(hash_to_split)
        inc.alert_hashes = hashes
        # source_key 有唯一约束：被拆出的告警要成为新事件的锚点，源事件须换锚到其余成员
        # （先改写再插入，避免同事务内唯一键冲突）
        if inc.source_key == hash_to_split:
            # D30 修复：新锚点不能直接取 hashes[0] —— 它可能已被**另一个**事件占为锚点
            # （实测：多个事件的成员 hash 与别的 source_key 重叠），直接改锚会撞唯一约束 → 409。
            # 与上方 holder 同策：选一个未被占用的成员作新锚；若被占用，先释放占用者（tombstone）。
            occ = {row[1]: row[0] for row in (await db.execute(
                select(Incident.id, Incident.source_key).where(Incident.id != incident_id))).all()}
            new_anchor = None
            for h in hashes:
                oid = occ.get(h)
                if oid is None:
                    new_anchor = h
                    break
                other = await db.get(Incident, oid)
                if other is not None:
                    await _release_anchor(db, other)   # 释放占用者后该成员可作新锚
                    await db.flush()
                    new_anchor = h
                    break
            if new_anchor is None:
                return {"ok": False, "msg": "NO_FREE_ANCHOR"}
            inc.source_key = new_anchor
        inc.cluster_reason = f"manual_split(拆出 {hash_to_split[:8]})"
        inc.updated_at = datetime.utcnow()
        await db.flush()
        new = Incident(title=f"{inc.title} · 拆分", severity=inc.severity, status="open",
                       alert_hashes=[hash_to_split], source_key=hash_to_split,
                       host_ident=inc.host_ident, cluster_reason=f"manual_split(来自 #{inc.id})")
        db.add(new)
        await db.commit()
        await db.refresh(new)
        return {"ok": True, "from": inc.id, "new": new.id}
