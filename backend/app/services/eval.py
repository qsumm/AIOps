# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""AI 诊断评测闭环（FR-428；15 号设计方案；ADR-005）。

三个组成部分：
  ① FAULT_EXPECTATIONS：故障注入 → 期望根因 的映射（deploy/faults 5 脚本为标注机）；
  ② score_case：确定性计分器（纯 Python，无 LLM）——根因一致 / 硬冲突 / 证据完整 / 校准；
  ③ run_eval：评测 runner——取 case 池 → rca.diagnose 回放重跑（temperature=0）→ 计分 →
     落 eval_runs；fingerprint() 生成版本指纹（提示词+技能+模型+代码）。

纪律（ADR-005）：
  - case 双池硬约束：runner 只按传入池取 case；回归池永不参与调优（守卫 test_d182 锁死）；
  - 评测数字只进内部页面与文档（红线 2），本模块不产生任何对外文案；
  - 回放不实发任何工具调用（rca.diagnose eval_mode.replay 分支保证）。
"""
import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.eval import EvalCase, EvalRun
from app.models.llm import LLMConfig

import logging

log = logging.getLogger("eval")

# ── ① 注入 → 期望根因（与 deploy/faults/*.sh 一一对应；15 号 §2.2）─────────────
FAULT_EXPECTATIONS: dict[str, dict] = {
    "cpu_spike":   {"label": "CPU 资源争用/飙升",   "must_mention": ["cpu"]},
    "disk_full":   {"label": "磁盘使用率超阈值",     "must_mention": ["磁盘", "disk", "分区"]},
    "mem_leak":    {"label": "内存异常/泄漏",        "must_mention": ["内存", "mem"]},
    "process_hang": {"label": "业务进程假死",        "must_mention": ["进程", "process", "假死", "hang"]},
    "probe_fail":  {"label": "服务不可用/探活失败",  "must_mention": ["探活", "端口", "不可用", "down", "失败"]},
}


def expectation_for(fault: str) -> dict | None:
    return FAULT_EXPECTATIONS.get(fault)


# ── 版本指纹（FR-428d）：提示词 + 技能集 + 模型配置 + 代码 ─────────────────────
async def fingerprint() -> str:
    """评测结果的对比键。任一层变化（改提示词/技能/模型/代码）指纹即变。"""
    from app.services import skills as _sk

    async with SessionLocal() as db:
        llm = (await db.execute(select(LLMConfig).where(LLMConfig.enabled == True))).scalars().all()  # noqa: E712
        model_part = sorted(f"{c.provider}:{c.model}:{c.fast_model}" for c in llm)
    # 注：scan_skill_files 是同步函数且无 DB——放在 SessionLocal 块内曾触发
    # aiomysql "Invalid argument"（连接事件循环被干扰），实测移出后稳定。
    names = _sk.scan_skill_files()
    skill_part = sorted(n.get("name", "") for n in names)
    from app.services import prompts as _pr
    prompt_part = hashlib.md5(_pr.RCA_SYSTEM_PROMPT.encode()).hexdigest()[:10] if getattr(
        _pr, "RCA_SYSTEM_PROMPT", "") else ""
    raw = json.dumps({"model": model_part, "skills": skill_part, "prompt": prompt_part},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── ② 确定性计分器 ────────────────────────────────────────────────────────────
_HARD_CONFLICTS: list[tuple[str, re.Pattern]] = [
    # 处置建议与已知事实矛盾的模式（命中即硬冲突；随 badcase 增补）。
    # 正则务必加括号分组——`a|b.*c` 会被解析成 `a` 或 `b.*c`（实测踩过：disk 一词即误判）。
    ("探活失败却建议轮转日志", re.compile(r"探活.*(失败|down).*轮转|轮转日志.*探活", re.I)),
    ("磁盘水位告警建议重启服务",
     re.compile(r"(disk|磁盘|disk\.percent).{0,40}(使用率|水位).{0,60}(重启服务|restart[_ ]service)", re.I | re.S)),
]


def _conclusion_text(result: dict) -> str:
    md = result.get("findings_md", "") or ""
    m = re.search(r"## 结论\s*(.+?)(?=\n## |\Z)", md, re.S)
    return (m.group(1) if m else md)[:4000]


def score_case(case: EvalCase, result: dict) -> dict:
    """单个 case 的确定性计分。result = rca.diagnose 的返回值。

    指标：
      match       根因一致（结论命中期望根因的关键词之一）；
      hard_conflict 处置建议与已知事实矛盾（_HARD_CONFLICTS 模式）；
      evidence    证据完整（结论引用了期望证据域，如 disk case 应引用 disk 证据；
                  以 must_mention 在证据/结论文中出现为准——宽松但确定）；
      calibration 置信度校准（证据不足时 INCONCLUSIVE 视为正确；这里用简化判据：
                  快照内无 SUCCESS 工具响应的 case，若结论 INCONCLUSIVE 则正确）。
    """
    exp = FAULT_EXPECTATIONS.get(case.expected) or {"must_mention": [case.expected]}
    conclusion = _conclusion_text(result)
    full_text = conclusion + "\n" + (result.get("findings_md", "") or "")

    match = any(kw.lower() in full_text.lower() for kw in exp["must_mention"])
    # 注意：这里必须是 any(...) 而不是 bool(生成器)——bool(<generator>) 恒真（写测试时逮住的）。
    hard_conflict = any(pat.search(full_text) for _, pat in _HARD_CONFLICTS)
    evidence = match   # P0 简化：关键词命中即视为证据链覆盖（P1 拆分为引用 step 检查）
    snap = (case.snapshot or {}).get("steps") or []
    no_evidence = not any((s.get("result") or {}).get("status") == "SUCCESS" for s in snap)
    conf = result.get("confidence", "")
    calibration = (conf == "INCONCLUSIVE") if no_evidence else (conf in ("CONFIRMED", "LIKELY", "INCONCLUSIVE"))

    return {
        "case_id": case.id, "title": case.title, "expected": case.expected,
        "expected_label": exp.get("label", case.expected),
        "confidence": conf, "match": match, "hard_conflict": hard_conflict,
        "evidence": evidence, "calibration": calibration,
        "tokens": result.get("total_tokens", 0),
    }


# ── ③ runner ─────────────────────────────────────────────────────────────────
async def run_eval(pool: str = "regress", case_ids: list[int] | None = None,
                   max_steps: int = 30) -> dict:
    """跑一轮评测：取池内 case → 回放重跑 → 计分 → 落 eval_runs → 返回汇总。

    只允许 tune|regress 二值；case_ids 指定时也强制按池过滤（双池是硬约束）。
    """
    from app.services import rca as _rca

    if pool not in ("tune", "regress"):
        raise ValueError(f"pool 必须是 tune|regress，收到 {pool!r}")
    fp = await fingerprint()
    async with SessionLocal() as db:
        q = select(EvalCase).where(EvalCase.pool == pool, EvalCase.label_status == "confirmed")
        if case_ids:
            q = q.where(EvalCase.id.in_(case_ids))
        cases = (await db.execute(q.order_by(EvalCase.id))).scalars().all()

    log.info("评测开始：pool=%s case=%d fingerprint=%s", pool, len(cases), fp)
    details, tokens_sum = [], 0
    for case in cases:
        snap = case.snapshot or {}
        try:
            result = await _rca.diagnose(
                incident_id=case.incident_id,
                eval_mode={"replay": {"steps": snap.get("steps") or []},
                           "fingerprint": fp, "case_title": case.title},
                max_steps=max_steps, quota_role="",
            )
        except Exception as e:  # noqa: BLE001 —— 单 case 失败不拖垮整轮
            log.warning("case %s 回放失败：%s", case.id, e)
            result = {"ok": False, "confidence": "", "findings_md": f"回放失败：{e}", "total_tokens": 0}
        sc = score_case(case, result)
        sc["error"] = not result.get("ok", True)
        tokens_sum += sc["tokens"]
        details.append(sc)

    total = len(details)
    def _rate(k: str) -> float:
        return round(sum(1 for d in details if d.get(k)) / total, 4) if total else 0.0

    summary = {"fingerprint": fp, "pool": pool, "total": total,
               "match_rate": _rate("match"),
               "hard_conflict_rate": round(sum(1 for d in details if d["hard_conflict"]) / total, 4) if total else 0.0,
               "evidence_rate": _rate("evidence"), "calibration_rate": _rate("calibration"),
               "avg_tokens": round(tokens_sum / total, 1) if total else 0.0}

    async with SessionLocal() as db:
        db.add(EvalRun(fingerprint=fp, pool=pool, total=total,
                       match_rate=summary["match_rate"],
                       hard_conflict_rate=summary["hard_conflict_rate"],
                       evidence_rate=summary["evidence_rate"],
                       calibration_rate=summary["calibration_rate"],
                       avg_tokens=summary["avg_tokens"], details=details))
        await db.commit()
    log.info("评测完成：%s", summary)
    return {"summary": summary, "details": details}


def _conf_from_md(md: str) -> str:
    import re as _re
    m = _re.search(r"^confidence:\s*(CONFIRMED|LIKELY|INCONCLUSIVE)", md, _re.M)
    return m.group(1) if m else ""


async def persist_case_from_incident(incident_id: int, expected: str, *, origin: str = "inject",
                                     pool: str = "regress", title: str = "", host_ident: str = "",
                                     expected_note: str = "") -> int | None:
    """把一次**刚完成的真实诊断**固化为 case：从 rca_findings + execution_steps 取材料。

    用法（注入流程）：注入故障 → 告警 → diagnose（正常模式）→ 调本函数固化。
    """
    from app.models.event import ExecutionStep, RCAFinding

    fp = await fingerprint()
    async with SessionLocal() as db:
        finding = (await db.execute(select(RCAFinding)
                                    .where(RCAFinding.incident_id == incident_id)
                                    .order_by(RCAFinding.id.desc()))).scalars().first()
        if finding is None:
            return None
        steps = (await db.execute(select(ExecutionStep)
                                  .where(ExecutionStep.finding_id == finding.id)
                                  .order_by(ExecutionStep.step_index))).scalars().all()
        step_rows = [{"step": s.step_index, "tool": s.tool_name,
                      "params": s.tool_input or {},
                      "result": json.loads(s.tool_output or "{}")} for s in steps]
        from app.services.rca import collect_context
        ctx = await collect_context(incident_id)
        case = EvalCase(title=(title or f"事件#{incident_id}·{expected}")[:190], origin=origin, pool=pool,
                        incident_id=incident_id, host_ident=host_ident or (ctx.get("incident") or {}).get("host_ident", ""),
                        expected=expected, expected_note=expected_note[:255], fingerprint=fp,
                        snapshot={"context": ctx, "steps": step_rows,
                                  "findings_md": finding.findings_md or "",
                                  # RCAFinding 无 confidence 列（置信度在 frontmatter/事件上）——从报告文本提取
                                  "confidence": _conf_from_md(finding.findings_md or "")},
                        label_status="confirmed" if origin == "inject" else "pending")
        db.add(case)
        await db.commit()
        await db.refresh(case)
        return case.id
