"""演示库数据修复（idempotent）：让 incident 的 host_ident 指向**真实存在**的主机。

为什么需要：早期种子/历史数据里 incident.host_ident 可能是主机名（`demo-db-01`）
或指向一台早已删除的主机哈希。后果是**旗舰演示路径翻车**——对这类事件跑 AI 诊断，
所有取证工具都返回 NO_DATA，报告只能写"对象主机未纳管"，看起来像平台坏了。

本脚本做三件事（默认 dry-run，加 --apply 才落库）：
  1. host_ident 为空/不存在 → 若标题里出现某台已纳管主机的名字，重指过去；
  2. 仍无法对应（纯合成数据）→ 打 `cluster_reason` 标记 `orphan_host`，
     便于前端/报告提示"这是历史合成数据、无对应主机"；
  3. 同名事件组只做统计提示（**不改数据**：同一指纹在旧事件关闭后可合法再触发）；
  4. `--purge-orphans`：清理**孤儿事件**——活跃/历史事件指向的主机或规则已不存在
     （手工改库、压测残留、早期种子都会造成）。这类事件会一直挂在大盘与告警中心，必须能一键清。

用法：
    cd backend && python scripts/repair_demo_data.py            # 只报告
    cd backend && python scripts/repair_demo_data.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# 让 `python scripts/xxx.py` 在**容器内**也能 import app（文档给的正是这个用法）：
# 直接跑脚本时 sys.path[0] 是 scripts/ 而不是项目根，`from app...` 会 ModuleNotFoundError。
# seed_demo.py 没这个问题是因为它只走 HTTP、不 import app（每个导出脚本都得自己修）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text  # noqa: E402

from app.db.session import SessionLocal, dispose_all_engines  # noqa: E402
from app.models.event import Incident  # noqa: E402
from app.models.host import Host  # noqa: E402


async def purge_orphans(apply: bool) -> None:
    """删除主机或规则已不存在的活跃/历史事件（孤儿事件）。"""
    async with SessionLocal() as db:
        hosts = (await db.execute(text("SELECT ident FROM hosts"))).all()
        rules = (await db.execute(text("SELECT id FROM alert_rules"))).all()
    idents = {h[0] for h in hosts}
    rids = {r[0] for r in rules}
    async with SessionLocal() as db:
        cur = (await db.execute(text("SELECT hash, host_ident, rule_id, rule_name FROM alert_cur_events"))).all()
        his = (await db.execute(text("SELECT id, host_ident, rule_id FROM alert_his_events"))).all()
        n_cur = [c for c in cur if c[1] not in idents or c[2] not in rids]
        # 历史账本按主机/规则维度粗判（history 保留价值高，只清明显孤儿）
        n_his = [h for h in his if h[1] not in idents and h[2] not in rids]
        print(f"孤儿活跃事件 {len(n_cur)} 条 / 孤儿历史事件 {len(n_his)} 条")
        for c in n_cur[:10]:
            print(f"  [活跃孤儿] {c[0][:12]} host={c[1][:12] or '(空)'} rule={c[3] or c[2]}")
        if apply and n_cur:
            for c in n_cur:
                await db.execute(text("DELETE FROM alert_cur_events WHERE hash = :h"), {"h": c[0]})
            for h in n_his:
                await db.execute(text("DELETE FROM alert_his_events WHERE id = :i"), {"i": h[0]})
            await db.commit()
            print(f"已清理：活跃 {len(n_cur)} / 历史 {len(n_his)}"
                  + ("" if apply else ""))
        elif n_cur:
            print("　（dry-run：加 --apply 才落库）")


async def run(apply: bool) -> None:
    async with SessionLocal() as db:
        hosts = (await db.execute(select(Host))).scalars().all()
        by_ident = {h.ident for h in hosts}
        name_to_ident = {(h.name or "").lower(): h.ident for h in hosts if h.name}
        incidents = (await db.execute(select(Incident).order_by(Incident.id))).scalars().all()

        repointed, orphaned, dupes = 0, 0, 0
        seen_titles: dict[str, int] = {}

        for inc in incidents:
            # 同名事件只统计、**不动数据**：同一指纹在旧事件关闭后可以合法地再次建事件，
            # "标题相同"不等于"重复"（早期版本按标题去重会误伤真实事件）。
            key = (inc.title or "").strip()
            seen_titles[key] = seen_titles.get(key, 0) + 1

            if inc.host_ident in by_ident:
                continue   # 已正确指向

            # 1) 按名字/标题重指
            cand = None
            for nm, ident in name_to_ident.items():
                if nm and (nm in (inc.title or "").lower() or nm == (inc.host_ident or "").lower()):
                    cand = ident
                    break
            if cand:
                repointed += 1
                print(f"  [重指] #{inc.id} {inc.host_ident or '(空)'} → {cand}  「{inc.title[:32]}」")
                if apply:
                    inc.host_ident = cand
                    if (inc.cluster_reason or "").startswith("orphan_host"):
                        inc.cluster_reason = "demo_seed"
                continue

            # 2) 无法对应：标记孤儿（不删数据，保留审计）
            orphaned += 1
            print(f"  [孤儿] #{inc.id} {inc.host_ident or '(空)'} 无对应主机  「{inc.title[:32]}」")
            if apply and not (inc.cluster_reason or "").startswith("duplicate_of"):
                inc.cluster_reason = "orphan_host"

        if apply:
            await db.commit()
        dup_groups = [k for k, n in seen_titles.items() if n > 1]
        print(f"\n主机 {len(hosts)} 台 / 事件 {len(incidents)} 条 → "
              f"重指 {repointed} / 孤儿标记 {orphaned}"
              + (f" / 同名事件组 {len(dup_groups)}（仅提示，未改动）" if dup_groups else "")
              + ("" if apply else "　（dry-run：加 --apply 才落库）"))
    await dispose_all_engines()


def main() -> int:
    ap = argparse.ArgumentParser(description="演示库数据修复（incident.host_ident 对齐真实主机）")
    ap.add_argument("--apply", action="store_true", help="真正落库（默认只报告）")
    ap.add_argument("--purge-orphans", action="store_true", help="清理主机/规则已不存在的孤儿事件")
    args = ap.parse_args()
    asyncio.run(run(args.apply))
    if args.purge_orphans:
        print()
        asyncio.run(purge_orphans(args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
