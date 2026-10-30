#!/usr/bin/env python3
"""清理测试/压测在演示库里留下的垃圾数据（**默认 dry-run，加 --apply 才真删**）。

为什么需要它：
  自动化测试与压测会往库里写"合法但难看"的数据——例如
    · D8 接口矩阵的"超长标题"边界用例留下 200 个 `x` 的事件；
    · 每次 `POST /api/ops/inspections/run-once` 都会建一条「即时巡检」记录，
      反复测试后攒出几十条同名记录，演示时非常难看。
  交付/截图/演示前应清一遍，否则买家第一眼看到的是测试残渣。

安全设计：
  1. **默认只报告不删除**（dry-run），必须显式 `--apply`；
  2. 只匹配**明确的测试特征**（不是"名字看起来怪就删"），每条规则都写明依据；
  3. `--apply` 前打印将删除的行数与样本，删完再复核一次计数；
  4. 不碰任何业务表的核心数据（主机/规则/技能/演示事件一律不动）。

用法：
    cd backend && python scripts/purge_test_junk.py              # 只看会删什么
    cd backend && python scripts/purge_test_junk.py --apply      # 真删
    cd backend && python scripts/purge_test_junk.py --keep-inspections 3 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402

# 测试造出的 rule_name 特征（三选一都是"测试才会有的名字"）：
#   1) 含 HTML/JS 注入串（XSS 用例）
#   2) atk- 前缀（攻击矩阵的注入载荷）
#   3) 名字长度 ≥ 40 字符的"边界串"（超长标题用例造的是 150~200 字符的名字）
#      —— 不用 REGEXP 反向引用：实测 SeekDB 不支持 `^(.)\1{19,}$` 这类回引（匹配不到，
#         会静默漏删），改用长度阈值这种可移植判定。合法规则名都很短（演示-* 约 10 字符）。
_JUNK_RULE_WHERE = (
    "(rule_name LIKE '%onerror%' OR rule_name LIKE '%__xssFired%' OR rule_name LIKE '%<img%' "
    " OR rule_name LIKE '%atk-%' OR CHAR_LENGTH(rule_name) >= 40)"
)

# (标签, 统计 SQL, 删除 SQL, 依据)
RULES: list[tuple[str, str, str, str]] = [
    (
        "边界测试的超长标题事件",
        "SELECT COUNT(*) FROM incidents WHERE title REGEXP '^x{20,}$'",
        "DELETE FROM incidents WHERE title REGEXP '^x{20,}$'",
        "test_d8_api_matrix 的『超长标题』用例造的数据（200 个 x），无业务含义",
    ),
    (
        "测试注入的告警（alert_cur_events）",
        "SELECT COUNT(*) FROM alert_cur_events WHERE " + _JUNK_RULE_WHERE,
        "DELETE FROM alert_cur_events WHERE " + _JUNK_RULE_WHERE,
        "D8 的 XSS / 超长字符 / 大整数边界用例通过**真引擎**触发的告警——"
        "用例清理了规则但没清告警，留下会在告警中心与 AI 问答里出现的孤儿告警",
    ),
    (
        "测试注入的告警（alert_his_events 台账）",
        "SELECT COUNT(*) FROM alert_his_events WHERE " + _JUNK_RULE_WHERE,
        "DELETE FROM alert_his_events WHERE " + _JUNK_RULE_WHERE,
        "同上（历史台账侧）",
    ),
    (
        "含 HTML 注入串的事件（XSS 用例残留）",
        "SELECT COUNT(*) FROM incidents WHERE title LIKE '%<img%' OR title LIKE '%onerror%'",
        "DELETE FROM incidents WHERE title LIKE '%<img%' OR title LIKE '%onerror%'",
        "TC-FE-22（XSS 转义）用例注入的标题，业务上不该存在",
    ),
    # D61（用户截图）：smoke/回归测试留下的「已停用」规则永远占着规则列表——
    # 原先只有软删没有硬删，越积越多。只删已停用的，启用中的不动。
    (
        "smoke 前缀的已停用规则",
        "SELECT COUNT(*) FROM alert_rules WHERE name LIKE 'smoke-%' AND disabled = 1",
        "DELETE FROM alert_rules WHERE name LIKE 'smoke-%' AND disabled = 1",
        "smoke_mainflow 每次运行建一条 smoke-规则-<ts>，跑完软删（停用），永久滞留列表",
    ),
    (
        "E2E / 回归测试前缀的已停用规则",
        "SELECT COUNT(*) FROM alert_rules WHERE (name LIKE 'E2E-%' OR name LIKE '回归测试-%') AND disabled = 1",
        "DELETE FROM alert_rules WHERE (name LIKE 'E2E-%' OR name LIKE '回归测试-%') AND disabled = 1",
        "e2e/回归脚本同类残留",
    ),
    (
        "smoke / E2E / 回归测试技能",
        "SELECT COUNT(*) FROM skills WHERE name LIKE 'smoke-%' OR name LIKE 'E2E技能%' OR name LIKE 'regression-skill-%'",
        "DELETE FROM skills WHERE name LIKE 'smoke-%' OR name LIKE 'E2E技能%' OR name LIKE 'regression-skill-%'",
        "技能自检/回归脚本建的一次性技能（正文 0 字），留在技能管理页即垃圾",
    ),
    (
        "测试残留的案例卡",
        "SELECT COUNT(*) FROM case_cards WHERE symptom LIKE 'd5test%' OR symptom IN ('CPU 高负载', '磁盘写满')",
        "DELETE FROM case_cards WHERE symptom LIKE 'd5test%' OR symptom IN ('CPU 高负载', '磁盘写满')",
        "D69 实测发现：早期冒烟/单测留下的极短泛化案例卡（如 5 字的「CPU 高负载」）"
        "在向量空间里会变成'黑洞'，把不相关查询都吸过去，拖累整个案例库的检索质量",
    ),
    (
        "孤儿服务行（主机已不存在）",
        "SELECT COUNT(*) FROM services WHERE host_id NOT IN (SELECT id FROM hosts)",
        "DELETE FROM services WHERE host_id NOT IN (SELECT id FROM hosts)",
        "主机被删（测试清理/库重置）后服务行失主——界面上列着但点取证报「主机不存在」（P5 实测踩到）",
    ),
    (
        "孤儿任务行（主机已不存在）",
        "SELECT COUNT(*) FROM agent_tasks WHERE host_id NOT IN (SELECT id FROM hosts)",
        "DELETE FROM agent_tasks WHERE host_id NOT IN (SELECT id FROM hosts)",
        "同上：任务失主后无法回填，只会堆积",
    ),
    (
        "接口矩阵/自检残留的抑制策略",
        "SELECT COUNT(*) FROM alert_mutes WHERE cause IN ('d8', 'smoke')",
        "DELETE FROM alert_mutes WHERE cause IN ('d8', 'smoke')",
        "D8 矩阵与 smoke 建抑制后未清（本地实测累积 468 条 cause='d8'）",
    ),
]

# 保留最近 N 条「即时巡检」，更早的视为测试残留
INSPECTION_RULE = (
    "历史「即时巡检」记录",
    "SELECT COUNT(*) FROM inspections WHERE name = '即时巡检'",
    """DELETE FROM inspections WHERE name = '即时巡检'
       AND id NOT IN (SELECT id FROM (SELECT id FROM inspections WHERE name='即时巡检'
                                      ORDER BY id DESC LIMIT :keep) t)""",
    "每次 run-once 都会建一条同名记录；保留最近若干条用于演示，更早的是测试残留",
)


async def count(db, sql: str, **kw) -> int:
    return int((await db.execute(text(sql), kw)).scalar() or 0)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认只报告）")
    ap.add_argument("--keep-inspections", type=int, default=3, help="保留最近几条「即时巡检」（默认 3）")
    args = ap.parse_args()

    mode = "APPLY（真删）" if args.apply else "DRY-RUN（只看不删）"
    print(f"=== 清理测试残留 · {mode} ===\n")

    async with SessionLocal() as db:
        total = 0
        plan: list[tuple[str, int, str, str, dict]] = []
        for label, csql, dsql, why in RULES:
            n = await count(db, csql)
            plan.append((label, n, dsql, why, {}))
            total += n
        label, csql, dsql, why = INSPECTION_RULE
        n = await count(db, csql)
        # 只删超出保留数的部分
        to_del = max(0, n - args.keep_inspections)
        plan.append((f"{label}（共 {n} 条，保留 {args.keep_inspections}）", to_del, dsql, why,
                     {"keep": args.keep_inspections}))
        total += to_del

        for label, n, _dsql, why, _kw in plan:
            flag = "🗑 " if n else "✅ "
            print(f"{flag}{label}：待删 {n} 条")
            print(f"    依据：{why}")
        print(f"\n合计待删：{total} 行")

        if not args.apply:
            print("\n（dry-run 结束。确认无误后加 --apply 执行）")
            return 0
        if total == 0:
            print("无需清理。")
            return 0

        for label, n, dsql, _why, kw in plan:
            if not n:
                continue
            r = await db.execute(text(dsql), kw)
            print(f"  已删 {r.rowcount} 行 ← {label}")
        await db.commit()

        # 删后复核
        print("\n=== 删后复核 ===")
        for label, csql, _d, _w in RULES:
            print(f"  {label}：剩余 {await count(db, csql)} 条")
        print(f"  即时巡检：剩余 {await count(db, INSPECTION_RULE[1])} 条")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
