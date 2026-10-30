# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""Skills 注册表（SKILL.md；03 §1.20 + 调研笔记 §1 的 Aurora/Holmes 双验证路线）。

- 每技能一个目录 / 一个 SKILL.md（frontmatter + 正文四段），目录扫描自动发现
- description 必填强校验（缺失即失配——Aurora 原话"质量决定匹配精度"）
- 交互聊天只注入 index_text；Agent 用 load_skill 按需拉全文
- 内置 **3 条核心方法论（恒加载）+ 5 条领域技能（按需加载，带连接检查）**（FR §6.8 要求 ≥5）
- **连接检查 fail-closed（抄 Aurora）**：领域技能声明 `requires`；检查不过的技能不进索引、
  不可加载——避免"技能指向不存在的数据源"成为幻觉来源。connection_check 为空 = 未检查，默认放行
  （不能让"没跑过检查"把技能全禁掉）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models.ops import Skill

log = logging.getLogger("skills")

SKILL_DIRS = [
    Path(__file__).resolve().parents[2] / "skills",          # 仓库内置（backend/skills）
    Path.home() / ".aiops" / "skills",                        # 用户自定义
]

CORE_SKILLS = {
    "core-investigation": {
        "alerts": ["*"],   # 核心方法论：任何诊断都是起点
        "description": "通用故障调查方法论：假设驱动、证伪优先、症状不等于根因。任何诊断都应先加载。",
        "body": """## 何时用
所有根因诊断任务的起点。

## 步骤
1. 用已有上下文（告警、指标窗口、探活、主机信息、历史案例）先列 2-3 个候选假设。
2. 每个假设设计一个**证伪型查询**：如果假设成立，应该能看到什么；如果看不到，就排除它。
3. 症状不是根因：把"CPU 高"追到"谁在耗 CPU、从什么时候开始、什么变更引入的"。
4. 查日志确认，不要只看进程活着（running ≠ healthy）。
5. 时间上的吻合不是因果；"常见原因"不是证据。

## 产出
四个二级标题齐全的 findings；每条结论带证据引用；affected 必须带证据 token。
""",
    },
    "core-safety": {
        "alerts": ["*"],   # 核心纪律：任何涉及处置建议的场景
        "description": "只读优先与变更纪律：没有验证过的修复不建议，危险命令一律不执行。",
        "body": """## 何时用
任何会给出处置建议或涉及执行的场景。

## 规则
1. 诊断阶段只做只读操作；不要建议未经证据支持的变更。
2. 每条修复建议必须给出：验证依据、影响面、风险等级、回滚方式。
3. 禁止类命令（rm -rf /、mkfs、dd 写盘、shutdown/reboot、curl|sh、drop database 等）永不执行、永不建议。
4. 非只读命令必须走人工审批（HITL）；平台不代运维在目标机执行命令。
""",
    },
    "core-reporting": {
        "alerts": ["*"],   # 报告写作：所有产出报告的场景
        "description": "报告写作规范：结论在前、证据在后、诚实标注不确定性与未检查项。",
        "body": """## 何时用
输出 findings / 巡检报告 / 复盘报告时。

## 规则
1. Summary 先给结论（或明确说"无法定论"），再给依据。
2. confidence 三档必须诚实：CONFIRMED / LIKELY / INCONCLUSIVE；错误的置信度比没有结论更有害。
3. not_checked 要如实列出（不要用推断扩大或缩小爆炸半径）。
4. 引用必须指向真实 step 或数据读数；不得编造。
""",
    },
# ---------------- 领域技能（FR §6.8：内置 ≥5 个）----------------
    # 只写"用现有只读工具就能做到"的动作；这个版本没有的能力（远端日志/shell）明确写边界。
    "host-health-diagnose": {
        "priority": 6,
        "alerts": ["cpu.*", "mem.*", "disk.*", "load.*", "*CPU*", "*内存*", "*磁盘*", "*负载*", "*主机*"],
        "description": "主机健康度排查：CPU/内存/磁盘/负载分层看，再区分磁盘满/内存打满/CPU 竞争三类常见病。",
        "check": {"requires": ["hosts"]},
        "body": """## 何时用
主机级告警（CPU 高、内存高、磁盘高、主机离线、负载高）或"某台机器不对劲"。

## 动作（全部只读）
1. `host_info` 先确认这台机器是否纳管，拿静态信息 + 活跃告警数 + 探活汇总。
2. `query_metrics` 分别看 `cpu.percent`、`mem.percent`、`disk.percent`、`load.1` 的
   min/max/avg/latest 与尾部走势（默认 60 分钟，可放大到 1440）。
3. 判类：
   - **磁盘**：`disk.percent` 单调上升 → 多半是日志/临时文件累积，看增速而不是绝对值。
   - **内存**：`mem.percent` 阶梯上升且不回落 → 泄漏；伴随 CPU 也高 → 可能是 swap/GC 抖动。
   - **CPU/负载**：`load.1` 与 CPU 同步冲高且周期性出现（如整点）→ 定时任务；否则查是谁在耗。
4. 平台自身主机才可 `get_process_list` 看 Top 进程；远程主机 V1 不报进程，写进 not_checked。

## 边界
远程主机的进程/日志不可得——不要假装看过，`not_checked` 里如实写。
""",
    },
    "metric-analysis": {
        "priority": 7,
        "description": "指标窗口分析：事故前基线 vs 事故中做对比，区分阶跃/尖峰/单调增长，拒绝用 2 个点下结论。",
        "alerts": ['metric', '指标', 'cpu.*', 'mem.*', 'load.*'],
        "check": {"requires": ["metrics"]},
        "body": """## 何时用
判断"是否真的异常""从什么时候开始""幅度多大"。

## 动作
1. `query_metrics` 取两个窗口：**事故前基线**与**事故中**（用告警 trigger_time 定位），
   读回 min/max/avg/latest 做对比，不要只报单点。
2. 形态判定（决定后续方向）：
   - 阶跃 step：某时刻抬升后维持 → 找该时刻的变更/发布。
   - 尖峰 spike：短时冲高即回落 → 定时任务、批量作业、重试风暴。
   - 单调增长 drift：持续上升 → 泄漏/累积（磁盘、内存、连接数）。
3. 窗口内点数 < 5 时明确说"采样稀疏、不足以定论"。
4. 指标只保留有限窗口（内存环形 + 聚合）；超窗时段写进 not_checked。

## 边界
只用实际能取到的指标名，不编造。
""",
    },
    "log-search": {
        "priority": 8,
        "description": "日志取证：先用告警时间框定窗口再读；远程主机用 Agent 取证，平台自身主机可直读。",
        "alerts": ['日志', 'journal', 'error', 'log.*', 'oom'],
        # D173-2：远程主机的取证能力是 fetch_remote_log（Agent ≥0.3.0），
        # 不再要求"平台自身主机已纳管"（公网容器化部署下这条永远不成立）。
        "check": {"requires": ["hosts"]},
        "body": """## 何时用
需要"错误证据"时（指标不够，必须看到具体报错/超时/重启记录）。

## 动作
1. 用告警/事件的 `trigger_time` 框定时间窗，再决定读哪个文件、找什么关键字。
2. **远程主机**用 `fetch_remote_log`（Agent ≥0.3.0，按服务自动发现日志路径，按级别/错误签名筛选）；
   **平台自身主机**可用 `read_log_tail` 读尾部。报告里必须引用**具体行内容**作为证据。
3. 取不到日志时**不要**推断内容，改为给出可复制的取证命令
   （如 `journalctl -u <svc> --since "<time>" | tail -200`）交给运维。
4. 常见模式：连接数/超时 → 看 accept 错误；OOM → 看内核或运行时；重启循环 → 看启动失败原因。

## 边界
读不到就说读不到；**禁止编造日志行**。
""",
    },
    "case-recall": {
        "priority": 9,
        "description": "历史案例检索：先查案例库再动手，把「上次同类故障怎么定位」当假设来源（不是结论）。",
        "alerts": ['案例', '复盘', '历史'],
        "check": {"requires": ["cases"]},
        "body": """## 何时用
诊断早期（找假设）与结论阶段（交叉验证）。

## 动作
1. `search_cases` 用"现象 + 关键实体"检索（例："磁盘使用率持续增长 日志未轮转"）。
2. 命中案例只当**假设来源**，必须用当前证据重新验证——调研笔记原话：
   "a prior conclusion is a hypothesis to disprove with fresh evidence, never a verdict"。
3. 报告可引用案例（写清 case id / 距离分数），但不把历史结论当本次结论。
4. 无命中就明说"案例库无相似记录"，不要编造。

## 边界
向量不可用时召回会降级为文本匹配，如实说明召回模式。
""",
    },
    "disk-troubleshoot": {
        "priority": 5,
        "description": "磁盘问题专项：区分空间不足与 inode 不足，按增速估算打满时间，给可验证的清理建议。",
        "alerts": ['disk.*', '磁盘', 'inode'],
        # D173-2：原声明 platform_host → 公网容器化部署下永不成立，最该用的磁盘专项技能
        # 被 fail-closed 挡在自动路由之外（实测 disk.percent 告警只命中 host-health-diagnose）。
        # 该技能核心是 query_metrics（任何上报主机都可用），本地日志/进程步骤正文里本就写明
        # "远程主机不可得，写 not_checked"——故依赖改为 metrics。
        "check": {"requires": ["metrics"]},
        # D173/P2：技能授权的只读命令（**会话作用域**白名单扩展）。
        # 平台与 Agent 双侧精确匹配放行；denylist（rm/shutdown/管道进 shell…）仍是硬边界。
        "commands": [
            "du -sh /var/log/*",
            "du -xhd1 /var",
        ],
        "body": """## 何时用
`disk.percent` 告警、写文件失败、服务因空间报错。

## 动作
1. `query_metrics` 看 `disk.percent` 历史：**阶跃**（一次性灌入）还是**单调增长**（持续写入）。
2. 用窗口首尾值算日增速，估算"还剩多久打满"——比"当前 87%"有用得多。
3. 平台自身主机可 `read_log_tail` 抽查线索、`get_process_list` 看是否进程狂写；远程主机不可得，写 not_checked。
4. 建议必须**可验证**：先给只读确认命令（`df -h` / `du -xhd1 <path> | sort -h | tail`），
   再给清理动作与回滚方式；不建议未经确认的删除。
5. 属日志累积时，同时建议轮转策略（logrotate）与阈值前置（如 85% 预警）。

## 边界
不建议 `rm -rf` 通配大范围路径；没看到目录明细前不猜"哪个目录大"。
""",
    },
}


def _unquote(v: str) -> str:
    """去掉**一层**成对引号。

    不能用 str.strip("\"'")——那会把命令里的结尾双引号也吃掉：
    `'mysql -e "SHOW FULL PROCESSLIST"'` 会被截成 `mysql -e "SHOW FULL PROCESSLIST`（命令被改坏）。
    """
    s = (v or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def parse_skill_md(text: str) -> dict:
    """解析 SKILL.md：frontmatter（name/description/alerts/commands/rca_priority）+ 正文。

    YAML 子集（够用且不引三方依赖）：
      · 标量  key: value
      · 行内数组 key: [a, b]
      · 块数组 key: 换行后 "  - item"（**commands 用这个**——命令里常含逗号，
        行内数组会把 `mysql -e "select a,b"` 拆坏）
    """
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text or "", re.S)
    if not m:
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开头")
    fm_raw, body = m.group(1), m.group(2)
    fm: dict = {}
    block_key: str | None = None
    for raw in fm_raw.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and block_key:      # 块数组的元素
            fm.setdefault(block_key, []).append(_unquote(stripped[2:]))
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if not v:                                        # 块数组开始
            block_key = k
            fm.setdefault(k, [])
            continue
        block_key = None
        if v.startswith("[") and v.endswith("]"):
            fm[k] = [_unquote(x) for x in v[1:-1].split(",") if x.strip()]
        else:
            fm[k] = _unquote(v)
    if not fm.get("name"):
        raise ValueError("SKILL.md 缺 name")
    if not fm.get("description"):
        raise ValueError("SKILL.md 缺 description（必填强校验：缺失即失配）")
    try:
        priority = int(fm.get("rca_priority") or 10)
    except (TypeError, ValueError):
        priority = 10
    return {"name": fm["name"], "description": fm["description"],
            "alerts_scope": fm.get("alerts") or [], "commands": fm.get("commands") or [],
            "rca_priority": priority, "body_md": body.strip()}


async def sync_builtin_skills() -> int:
    """写入/更新内置核心技能到 DB（幂等）。返回写入条数。"""
    n = 0
    async with SessionLocal() as db:
        for name, spec in CORE_SKILLS.items():
            row = (await db.execute(select(Skill).where(Skill.name == name))).scalar_one_or_none()
            idx_text = f"- {name}: {spec['description']}"[:512]
            if row is None:
                db.add(Skill(name=name, description=spec["description"], body_md=spec["body"],
                             index_text=idx_text, source="builtin", enabled=True,
                             rca_priority=1 if name.startswith("core-") else int(spec.get("priority") or 5),
                             alerts_scope=list(spec.get("alerts") or []),
                             commands=list(spec.get("commands") or []),
                             connection_check={"spec": spec.get("check") or {}}))
                n += 1
            else:
                row.description, row.body_md = spec["description"], spec["body"]
                row.index_text = idx_text
                # 存量数据修复（D173/P1）：alerts 元数据此前只在代码里、没进库——
                # 不补这一步，自动路由上线即空转（见 14-技能体系升级 §2.3）
                row.alerts_scope = list(spec.get("alerts") or [])
                if spec.get("commands"):
                    row.commands = list(spec["commands"])
                # 优先级：领域技能按"越具体越靠前"排序（磁盘专项 < 主机通用），
                # 自动路由取前 N 时才不会被通用技能挤掉
                if not name.startswith("core-"):
                    row.rca_priority = int(spec.get("priority") or 5)
                cc = dict(row.connection_check or {})
                # 依赖声明变更时**上次的检查结论作废**：旧 verdict 描述的是旧依赖集——
                # 实测教训（D173-2）：依赖从 platform_host 放宽为 metrics 后，DB 里仍留着
                # "platform_host 不成立 → ok=false"，fail-closed 会继续把技能挡在
                # 自动路由外，直到有人手动点"连接检查"——用户完全无感。
                if (cc.get("spec") or {}).get("requires") != (spec.get("check") or {}).get("requires"):
                    cc.pop("ok", None)
                    cc.pop("note", None)
                    cc.pop("checked_at", None)
                cc["spec"] = spec.get("check") or {}
                row.connection_check = cc

        await db.commit()
    return n


def scan_skill_files() -> list[dict]:
    """扫描 skills 目录（max_depth=2）解析 SKILL.md；坏文件按 NAMED 分类跳过并记日志。"""
    found = []
    for d in SKILL_DIRS:
        if not d.exists():
            continue
        for f in sorted(d.glob("*/SKILL.md")) + sorted(d.glob("SKILL.md")):
            try:
                spec = parse_skill_md(f.read_text(encoding="utf-8"))
                spec["path"] = str(f)
                found.append(spec)
            except Exception as e:  # noqa: BLE001 —— 具体文件坏=NAMED（可展示）
                log.warning("技能文件解析失败（%s）：%s", f, e)
    return found


async def import_skill_files() -> dict:
    """把磁盘上的 SKILL.md 导入 DB（同名覆盖为用户版）。"""
    files = scan_skill_files()
    imported, failed = 0, []
    async with SessionLocal() as db:
        for spec in files:
            try:
                row = (await db.execute(select(Skill).where(Skill.name == spec["name"]))).scalar_one_or_none()
                if row is None:
                    db.add(Skill(name=spec["name"], description=spec["description"], body_md=spec["body_md"],
                                 alerts_scope=spec["alerts_scope"], rca_priority=spec["rca_priority"],
                                 commands=spec.get("commands") or [],
                                 index_text=f"- {spec['name']}: {spec['description']}"[:512], source="user"))
                else:
                    row.description, row.body_md = spec["description"], spec["body_md"]
                    row.alerts_scope, row.rca_priority = spec["alerts_scope"], spec["rca_priority"]
                    if spec.get("commands"):
                        row.commands = list(spec["commands"])
                imported += 1
            except Exception as e:  # noqa: BLE001
                failed.append({"skill": spec.get("name"), "error": str(e)[:120]})
        await db.commit()
    return {"imported": imported, "failed": failed, "scanned_dirs": [str(d) for d in SKILL_DIRS]}


# ---------------- 连接检查（fail-closed；Aurora connection_check 的落地）----------------
async def _check_requirements(reqs: list[str]) -> list[dict]:
    """逐项检查技能依赖的资源是否到位。返回 [{key, ok, detail}]。"""
    from app.models.collect import Probe
    from app.models.host import Host
    from sqlalchemy import text as _text

    out: list[dict] = []
    async with SessionLocal() as db:
        for key in reqs:
            if key == "hosts":
                n = (await db.execute(select(func.count()).select_from(Host))).scalar() or 0
                out.append({"key": key, "ok": bool(n), "detail": f"已纳管主机 {n} 台"})
            elif key == "metrics":
                from app.store import ring

                hosts = (await db.execute(select(Host.ident, Host.last_beat))).all()
                recent = [h for h in hosts if h[1] and (datetime.utcnow() - h[1]).total_seconds() < 300]
                out.append({"key": key, "ok": bool(recent),
                            "detail": f"近 5 分钟有上报的主机 {len(recent)}/{len(hosts)} 台"})
            elif key == "probes":
                n = (await db.execute(select(func.count()).select_from(Probe))).scalar() or 0
                out.append({"key": key, "ok": bool(n), "detail": f"探活配置 {n} 条"})
            elif key == "cases":
                try:
                    n = int((await db.execute(_text("SELECT COUNT(*) FROM case_cards"))).scalar() or 0)
                except Exception:  # noqa: BLE001
                    n = 0
                out.append({"key": key, "ok": bool(n), "detail": f"案例卡 {n} 张"})
            elif key == "platform_host":
                import platform as _pf

                hosts = (await db.execute(select(Host.name, Host.ip))).all()
                ok = any((n and n.lower() == _pf.node().lower()) or i in ("127.0.0.1", "::1") for n, i in hosts)
                out.append({"key": key, "ok": bool(ok),
                            "detail": "存在平台自身主机（可读日志/进程）" if ok else "未纳管平台自身主机 → 日志/进程取证不可用"})
            else:
                out.append({"key": key, "ok": False, "detail": f"未知依赖项 {key}"})
    return out


async def check_skill(name: str, persist: bool = True) -> dict:
    """执行技能连接检查；persist=True 时把结果写回 skills.connection_check。"""
    async with SessionLocal() as db:
        row = (await db.execute(select(Skill).where(Skill.name == name))).scalar_one_or_none()
        if row is None:
            return {"ok": False, "checks": [], "note": f"技能 {name} 不存在"}
        spec = (row.connection_check or {}).get("spec") or {}
        reqs = list(spec.get("requires") or [])
        if not reqs:
            result = {"ok": True, "checks": [], "note": "无外部依赖（核心方法论技能，恒可用）"}
        else:
            checks = await _check_requirements(reqs)
            failed = [c for c in checks if not c["ok"]]
            result = {"ok": not failed, "checks": checks,
                      "note": "连接正常" if not failed else "依赖未就绪：" + "；".join(f"{c['key']}({c['detail']})" for c in failed)}
        if persist:
            row.connection_check = {"spec": spec, "ok": result["ok"],
                                    "checked_at": datetime.utcnow().isoformat() + "Z", "note": result["note"]}
            await db.commit()
    return result


def _healthy(row: Skill) -> bool:
    """fail-closed：明确检查失败才排除；未检查（无 ok 字段）默认放行。"""
    cc = row.connection_check or {}
    return cc.get("ok") is not False


async def skill_effects(days: int = 90) -> dict:
    """P4（FR-427d）：技能效果——只算可从真实流水复算的计数/比率，不做评分。

    数据源（零新增埋点，三张既有表）：
      execution_steps（tool_name='load_skill'）→ 技能×诊断的使用记录
      finding_triage（每个事件最新处置）        → 人工处置结果
      rca_findings.total_tokens                → 诊断成本

    口径：
      加载次数 = 该技能被 load 的**去重事件数**；
      跟进率   = 用了该技能的事件中，最新处置=followup 的占比；
      基线     = 全平台有处置的事件的 followup 占比（对照，不是因果）；
      Token 中位数 = 用了该技能的诊断的 total_tokens 中位数。
    样本 <5 → insufficient=True（前端显示"样本不足"，不硬算比率）。

    诚实边界：跟进率是**相关**指标——技能常被用在更难的诊断上，天然偏高；非因果。
    返回 {skill_name: {...}}（含全部技能，未使用的为 loaded=0）。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import text as _text

    since = datetime.utcnow() - timedelta(days=days)
    async with SessionLocal() as db:
        names = [r[0] for r in (await db.execute(
            _text("SELECT name FROM skills"))).all()]
        # 技能 → 用过它的事件（去重）
        rows = (await db.execute(_text(
            "SELECT JSON_UNQUOTE(JSON_EXTRACT(es.tool_input, '$.name')) AS sname, "
            "       es.incident_id FROM execution_steps es "
            "WHERE es.tool_name='load_skill' AND es.created_at >= :since"),
            {"since": since})).all()
        used_by: dict[str, set] = {}
        for sname, inc in rows:
            if not sname or inc is None:
                continue
            used_by.setdefault(str(sname), set()).add(int(inc))
        # 每个事件的最新处置
        tri = (await db.execute(_text(
            "SELECT t.incident_id, t.action FROM finding_triage t "
            "JOIN (SELECT incident_id, MAX(id) AS mid FROM finding_triage "
            "      GROUP BY incident_id) last ON t.id = last.mid"))).all()
        latest = {int(i): str(a) for i, a in tri}
        # 事件 → token
        tok = (await db.execute(_text(
            "SELECT incident_id, total_tokens FROM rca_findings "
            "WHERE total_tokens > 0"))).all()
        tokens = {int(i): int(v) for i, v in tok}

    n_all = len(latest)
    fu_all = sum(1 for a in latest.values() if a == "followup")
    baseline = round(fu_all / n_all, 3) if n_all else None

    out: dict[str, dict] = {}
    for name in names:
        used = used_by.get(name, set())
        n_used = len(used)
        item = {"skill": name, "loaded": n_used, "days": days,
                "baseline_rate": baseline, "insufficient": n_used < 5}
        if n_used >= 5:
            fu = sum(1 for i in used if latest.get(i) == "followup")
            item["followup_rate"] = round(fu / n_used, 3)
            vals = sorted(tokens[i] for i in used if i in tokens)
            item["token_median"] = vals[len(vals) // 2] if vals else None
            item["token_samples"] = len(vals)
        out[name] = item
    return out


async def skill_effect(name: str, days: int = 90) -> dict:
    """单技能效果（P4）：见 skill_effects 的口径说明。"""
    data = await skill_effects(days=days)
    return data.get(name) or {"skill": name, "loaded": 0, "insufficient": True, "days": days}


async def skill_index() -> list[dict]:
    """给 prompt 注入的索引行（只注入 index_text；Agent 用 load_skill 拉全文）。

    连接检查明确失败的技能不进索引 —— 不让模型去加载一个用不了的技能。
    """
    async with SessionLocal() as db:
        rows = (await db.execute(select(Skill).where(Skill.enabled.is_(True))
                                 .order_by(Skill.rca_priority))).scalars().all()
    return [{"name": r.name, "description": r.description, "index": r.index_text}
            for r in rows if _healthy(r)]


def _scope_hit(patterns: list, metrics: list[str], rule_names: list[str]) -> bool:
    """D173（FR-427a）：alerts_scope 命中判定。

    模式语义（对事件成员告警的 metric 或 rule_name 任一命中即算）：
      · metric      → fnmatch 通配（cpu.*、mem.percent）
      · rule_name   → fnmatch 通配 **或** 去掉通配符后的子串包含
        （规则名常带序号前缀「1020-磁盘使用率」，纯 fnmatch 匹配不到「磁盘」；
          带通配的「*日志*」用子串也匹配不到——两种都支持才符合直觉）
    例：["mem.*", "disk.*"]、["1020-磁盘使用率"]、["*日志*"]。
    空 patterns = 不参与自动路由（只靠 LLM 自觉 load_skill）。
    """
    import fnmatch as _fm

    for p in patterns or []:
        p = str(p).strip()
        if not p:
            continue
        for m in metrics:
            if _fm.fnmatch(m, p):
                return True
        core = p.strip("*").strip()
        for rn in rule_names:
            if _fm.fnmatch(rn, p):
                return True
            if core and core in rn:
                return True
    return False


async def match_skills(metrics: list[str], rule_names: list[str], limit: int = 2) -> list[dict]:
    """P1 自动路由：按事件指标/规则名匹配 alerts_scope，返回 ≤limit 个健康技能全文。

    设计要点（14-技能体系升级 §2.3 P1）：
      · 确定性触发——不再依赖 LLM 自觉点 load_skill；
      · 上限 limit 防 prompt 爆炸（rca_priority 升序优先）；
      · 连接检查失败（fail-closed）与空 alerts_scope 一律跳过。
    """
    async with SessionLocal() as db:
        rows = (await db.execute(select(Skill).where(Skill.enabled.is_(True))
                                 .order_by(Skill.rca_priority))).scalars().all()
    hits = []
    for r in rows:
        if not _healthy(r) or not r.alerts_scope:
            continue
        # core-* 三条核心方法论（alerts=["*"]）靠索引常驻，不占 auto-loaded 名额——
        # 否则 limit 会被它们占满，领域技能永远轮不到（设计修正）
        if r.name.startswith("core-"):
            continue
        if _scope_hit(r.alerts_scope, metrics or [], rule_names or []):
            hits.append({"name": r.name, "description": r.description, "body_md": r.body_md,
                         "wrapped": f"<skill name=\"{r.name}\" auto-loaded=\"true\">\n{r.body_md}\n</skill>"})
        if len(hits) >= limit:
            break
    return hits


async def load_skill(name: str) -> dict | None:
    """按名取技能全文（含 <skill> 包裹提示，调研笔记 §1 的 fetch_skill 语义）。

    fail-closed：连接检查失败的技能返回 None（等价"技能不存在"），
    并提示可用 load_skill 前先看索引里有什么。
    """
    async with SessionLocal() as db:
        r = (await db.execute(select(Skill).where(Skill.name == name, Skill.enabled.is_(True)))).scalar_one_or_none()
    if r is None or not _healthy(r):
        if r is not None:
            log.info("技能 %s 连接检查未通过，拒绝加载", name)
        return None
    return {"name": r.name, "description": r.description, "body_md": r.body_md,
            "commands": list(r.commands or []),
            "wrapped": f"<skill name=\"{r.name}\">\n{r.body_md}\n</skill>\n\n"
                       "以上是做法指引，不是结论。执行后请以「逐步清单（每步注明结果）+ 根因 + 修复」格式汇报，不要用表情符号。"}


async def skill_commands(name: str) -> list[str]:
    """取技能的授权只读命令（会话内白名单扩展用；D173/P2）。

    失败连接检查的技能返回空——fail-closed 与 load_skill 保持同一口径。
    """
    async with SessionLocal() as db:
        r = (await db.execute(select(Skill).where(Skill.name == name, Skill.enabled.is_(True)))).scalar_one_or_none()
    if r is None or not _healthy(r):
        return []
    return [str(c).strip() for c in (r.commands or []) if str(c).strip()]


# ---------------- P3：GitHub 导入（FR-427c）----------------
GITHUB_HOSTS = {"github.com", "www.github.com", "raw.githubusercontent.com"}
MAX_SKILL_BYTES = 1024 * 1024     # 1MB 上限：SKILL.md 是文本方法论，1MB 已远超正常体量
GITHUB_TIMEOUT_S = 10


def _github_raw_url(url: str) -> str:
    """把 GitHub 技能 URL 归一为 raw 地址。

    支持三种形态（用户最常贴的就是第一种）：
      https://github.com/{owner}/{repo}/blob/{branch}/{path}/SKILL.md
      https://github.com/{owner}/{repo}/tree/{branch}/{dir}          → {dir}/SKILL.md
      https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}

    非 GitHub 域名一律拒绝（SSRF 边界：只允许这两个域）。
    """
    from urllib.parse import urlparse

    u = (url or "").strip()
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 链接")
    host = (p.hostname or "").lower()
    if host not in GITHUB_HOSTS:
        raise ValueError(f"只允许 github.com / raw.githubusercontent.com 域名（当前：{host or '空'}）")
    path = p.path.strip("/")
    parts = [x for x in path.split("/") if x]
    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise ValueError("raw 链接缺少 owner/repo/branch/path")
        return f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{parts[2]}/" + "/".join(parts[3:])
    if len(parts) < 3:
        raise ValueError("GitHub 链接缺少 owner/repo")
    owner, repo = parts[0], parts[1]
    if parts[2] == "blob":
        if len(parts) < 5:
            raise ValueError("blob 链接缺少分支与文件路径")
        branch, rest = parts[3], "/".join(parts[4:])
    elif parts[2] == "tree":
        if len(parts) < 4:
            raise ValueError("tree 链接缺少分支与目录")
        branch, rest = parts[3], "/".join(parts[4:] + ["SKILL.md"])
    else:
        raise ValueError("只支持 blob/tree 形态的 GitHub 链接")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{rest}"


def _github_ssl_context():
    """给 GitHub 抓取准备 SSL 上下文。

    实测（2026-10-23）：部分 Python 环境（本地开发机 / 精简镜像）**没有可用的系统 CA**，
    urllib 会抛 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`——
    表现成"GitHub 导入失败"，但其实是本机证书链问题，不是 GitHub 的问题。
    故优先用 certifi 的 CA 包（requests 依赖里已带），没有再退回系统默认。
    """
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 —— 没有 certifi 就用系统默认
        return ssl.create_default_context()


def _fetch_text(url: str, limit: int = MAX_SKILL_BYTES, timeout: int = GITHUB_TIMEOUT_S,
                retries: int = 2) -> str:
    """抓取远程文本（限大小、限超时、限 UA、失败重试）。

    实测（2026-10-23 公网）：从阿里云到 raw.githubusercontent.com **连接会被重置**
    （curl 时好时坏、urllib 直接 Connection reset）——公网到 GitHub 的链路本身不稳。
    故加**短退避重试**，并在最终失败时把"网络不通"说清楚（而不是把异常栈甩给用户）。
    """
    import time
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "aiops-skill-importer/1.0"})
    ctx = _github_ssl_context()
    last_err: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 —— 域名已白名单
                data = resp.read(limit + 1)
            break
        except urllib.error.HTTPError as e:      # 4xx/5xx：重试无意义
            raise ValueError(f"GitHub 返回 {e.code}：{url}（文件可能不存在或仓库是私有的）")
        except Exception as e:  # noqa: BLE001 —— 网络类错误值得重试
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    else:
        raise ValueError(f"GitHub 连接失败（{type(last_err).__name__}：{str(last_err)[:80]}）"
                         f"——本机到 GitHub 的网络不通或被限流，请稍后重试")
    if len(data) > limit:
        raise ValueError(f"文件超过 {limit // 1024}KB 上限，已拒绝")
    return data.decode("utf-8", errors="replace")


async def import_from_github(url: str, user: str = "") -> dict:
    """从 GitHub 导入 SKILL.md（FR-427c）。

    边界（诚实声明）：
      · 只收 **文本方法论 + commands**——scripts/ 附属文件列出但不入库
        （我们没有脚本执行环境，不假装支持 Anthropic 的 bash 语义）；
      · 连接检查不通过时入库但标记 unhealthy（fail-closed 下不会被自动路由/加载）。
    """
    raw_url = _github_raw_url(url)
    text = _fetch_text(raw_url)
    spec = parse_skill_md(text)          # frontmatter 校验（name/description 必填）
    async with SessionLocal() as db:
        row = (await db.execute(select(Skill).where(Skill.name == spec["name"]))).scalar_one_or_none()
        if row is not None:
            raise ValueError(f"技能「{spec['name']}」已存在，请先改名或删除后再导入")
        s = Skill(name=spec["name"], description=spec["description"], body_md=spec["body_md"],
                  alerts_scope=list(spec["alerts_scope"]), commands=list(spec["commands"]),
                  rca_priority=spec["rca_priority"], source="imported",
                  index_text=f"- {spec['name']}: {spec['description']}"[:512],
                  connection_check={"spec": {}, "imported_from": raw_url,
                                    "imported_by": user or "", "ok": None})
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return {"id": s.id, "name": s.name, "source_url": raw_url,
                "alerts_scope": s.alerts_scope, "commands": s.commands,
                "has_scripts": "scripts/" in (spec["body_md"] or "")}
