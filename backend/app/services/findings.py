"""write_findings 强 schema：解析与校验（FR-D2；03 §1.14）。

findings.md 结构：
  ---  YAML frontmatter 七字段（agent_id/purpose/status/tools_used/citations/self_assessed_strength/confidence + impact 四分类）
  ---
  ## 结论 / ## 证据 / ## 推理 / ## 已排除的假设   四个 H2 必填

校验失败 → 把错误清单回喂 LLM 重试（≤2 次）→ 仍失败 → 落 status=failed stub（保证进程不挂，报告标注可重试）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

REQUIRED_H2 = ["结论", "证据", "推理", "已排除的假设"]
CONFIDENCE = {"CONFIRMED", "LIKELY", "INCONCLUSIVE"}
STRENGTH = {"strong", "moderate", "weak", "inconclusive"}
IMPACT_KEYS = ["affected", "checked_healthy", "changed_other_reason", "not_checked"]
# P1-11：调查覆盖清单（Aurora 角色拆分的单 Agent 版）。每项写结论或“not_checked: 原因”，
# 逼模型明确“哪些维度没查”，避免一条道查到黑。
COVERAGE_KEYS = ["error_signal", "recent_change", "runtime_state", "history", "blast_radius"]


@dataclass
class Findings:
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    raw: str = ""
    errors: list[str] = field(default_factory=list)
    normalize_notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def confidence(self) -> str | None:
        return self.frontmatter.get("confidence")

    @property
    def strength(self) -> str | None:
        return self.frontmatter.get("self_assessed_strength")

    @property
    def status(self) -> str:
        return self.frontmatter.get("status", "succeeded")

    def summary_section(self) -> str:
        return _section(self.body, "结论")


def _section(body: str, title: str) -> str:
    m = re.search(rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", body or "", re.S | re.M)
    return (m.group(1).strip() if m else "")


def parse_findings(text: str) -> Findings:
    """解析 frontmatter + body。frontmatter 用宽松 YAML 子集解析（避免额外依赖）。"""
    f = Findings(raw=text or "")
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text or "", re.S)
    if not m:
        f.errors.append("缺少 YAML frontmatter（文件须以 --- 开头的元数据块开始）")
        f.body = text or ""
        return f
    fm_raw, f.body = m.group(1), m.group(2)
    f.frontmatter = _mini_yaml(fm_raw)
    return f


def _mini_yaml(raw: str) -> dict:
    """极简 YAML 子集（**缩进感知**）。支持：

    顶层 `key: value` / `key: [行内列表]` / `key: {行内对象}` / `key:` 起一个块，块内：
      - `sub: value`、`sub: [行内]`
      - `sub:` 后跟 `- item`（标量序列）
      - `sub:` 后跟 `- k: v` + 更深缩进的 `k2: v2`（对象序列）

    D20 修复：原实现用「整行是否有 ≥2 空格缩进 + 首字符是 \\w」来判子键，
    于是**块序列形式**（`  affected:` 换行后 `    - entity: x`）会：
      · `affected` 被赋成空字符串（而非列表）
      · `- entity: x` 因首字符是 `-` 不匹配 → 被当成**顶层键**塞进 frontmatter
      · `cur_block` 被重置 → 紧随其后的 `  checked_healthy: []` 也**泄漏到顶层**
    结果是：模型明明写对了 `checked_healthy`，校验器却报"impact 缺分类：checked_healthy"
    → 报告判废（实测 MiniMax-M3 就会写成块序列形式，提示词给的是行内形式）。
    """
    rows: list[tuple[int, str]] = []
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        rows.append((len(line) - len(line.lstrip(" ")), line.strip()))

    def parse(idx: int, indent: int):
        # 序列：本层第一行以 `- ` 开头
        if idx < len(rows) and rows[idx][0] >= indent and rows[idx][1].startswith("-"):
            seq: list = []
            while idx < len(rows) and rows[idx][0] >= indent and rows[idx][1].startswith("-"):
                ind, text = rows[idx]
                body = text[1:].strip()
                if ":" in body and not body.startswith(("[", "{")):
                    k, _, v = body.partition(":")
                    obj = {k.strip(): _coerce(v.strip())}
                    idx += 1
                    while idx < len(rows) and rows[idx][0] > ind:
                        k2, _, v2 = rows[idx][1].partition(":")
                        obj[k2.strip()] = _coerce(v2.strip())
                        idx += 1
                    seq.append(obj)
                else:
                    seq.append(_coerce(body))
                    idx += 1
            return seq, idx

        obj: dict = {}
        while idx < len(rows) and rows[idx][0] >= indent:
            ind, text = rows[idx]
            if text.startswith("- "):
                break
            k, _, v = text.partition(":")
            k, v = k.strip(), v.strip()
            idx += 1
            if v:
                obj[k] = _coerce(v)
            elif idx < len(rows) and rows[idx][0] > ind:
                child, idx = parse(idx, rows[idx][0])
                obj[k] = child
            else:
                obj[k] = ""
        return obj, idx

    if not rows:
        return {}
    value, _ = parse(0, rows[0][0])
    return value if isinstance(value, dict) else {}


def _coerce(v: str):
    if v.startswith("[") and v.endswith("]"):
        return [_coerce_item(x) for x in _split_top(v[1:-1])]
    if v.startswith("{") and v.endswith("}"):
        return _parse_inline_obj(v)
    return _strip_quotes(v)


def _split_top(inner: str) -> list[str]:
    """按顶层逗号切分（忽略 [] 与 {} 内的逗号）——支撑 affected: [{a: 1}, {b: 2}]。"""
    out, buf, depth = [], [], 0
    for ch in inner:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def _parse_inline_obj(s: str) -> dict:
    """解析行内对象 {k: v, k2: v2}（值不做嵌套解析）。"""
    body = s[1:-1] if s.startswith("{") else s
    out: dict = {}
    for part in _split_top(body):
        if ":" not in part:
            continue
        k, _, val = part.partition(":")
        out[_strip_quotes(k.strip())] = _strip_quotes(val.strip())
    return out


def _coerce_item(x: str):
    x = x.strip()
    if x.startswith("{") and x.endswith("}"):
        return _parse_inline_obj(x)
    return _strip_quotes(x)


def _strip_quotes(s: str):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


_STRENGTH_ZH = {"强": "strong", "中": "moderate", "较强": "strong", "中等": "moderate",
                "弱": "weak", "无法判断": "inconclusive", "不确定": "inconclusive"}


def normalize_findings(f: Findings) -> list[str]:
    """对**机械性错位**做规范化（不改变结论内容，只修字段用法），返回规范化说明。

    实测（MiniMax-M3）模型常犯：把 confidence 的值写进 status、把中文/自造词写进
    self_assessed_strength、citations 漏填但正文里其实引用了 step。这些属于"字段用法
    错误"而非"证据不足"，规范化即可，避免无谓地退化成 stub。
    """
    notes: list[str] = []
    fm = f.frontmatter

    # 1) status 写成了置信度 → 移回 confidence，status 置 succeeded
    if fm.get("status") in CONFIDENCE:
        fm.setdefault("confidence", fm["status"])
        notes.append(f"status={fm['status']} 实为置信度，已移至 confidence，status 置 succeeded")
        fm["status"] = "succeeded"

    # 2) self_assessed_strength 非枚举 → 中文映射 / 归一 / 兜底 inconclusive
    s = str(fm.get("self_assessed_strength") or "").strip()
    if s and s.lower() not in STRENGTH:
        mapped = None
        for zh, en in _STRENGTH_ZH.items():
            if s.startswith(zh):
                mapped = en
                break
        low = s.lower()
        if not mapped:
            for cand in ("strong", "moderate", "weak", "inconclusive"):
                if cand in low:
                    mapped = cand
                    break
        fm["self_assessed_strength"] = mapped or "inconclusive"
        notes.append(f"self_assessed_strength={s[:24]!r} 非枚举，归一为 {fm['self_assessed_strength']}")

    # 3) citations 为空但正文/元数据里其实引用了 step → 提取模型自己的引用（非编造）
    cites = fm.get("citations")
    if not cites:
        found = sorted(set(re.findall(r"step[_-]?(\d+)", f.body or "")))
        if found:
            fm["citations"] = [f"step_{n}" for n in found]
            notes.append(f"citations 为空，已从正文提取实际引用 {fm['citations']}")

    # 4) impact 四分类缺**空列表**子键 → 补 []（D20 修复）
    #
    # 实测（MiniMax-M3，2026-10-14）：模型给出了完整且高质量的报告，但 `impact:` 里只写了
    # affected，漏掉 checked_healthy / changed_other_reason / not_checked 三个**空列表**键
    # → 校验失败 → 重试 2 次仍漏 → 退化成 stub，用户看到"诊断报告生成失败"，
    # 而这次诊断已烧掉 84,319 tokens 且 thoughts 里的推理链是正确可用的。
    #
    # 语义上"键缺失"与"该分类无内容"完全等价（stub 自己写的也是 `[]`），
    # 属于**字段用法**问题而非"证据不足"，故在此规范化补齐，不丢整份报告。
    impact = fm.get("impact")
    if isinstance(impact, dict):
        filled = [k for k in IMPACT_KEYS if k not in impact]
        if filled:
            for k in filled:
                impact[k] = []
            fm["impact"] = impact
            notes.append(f"impact 缺空分类 {filled} → 已补 []（键缺失与「无内容」等价）")

    return notes


def validate_findings(f: Findings, known_step_ids: set[str] | None = None) -> list[str]:
    """返回错误清单（空=通过）。逐条对齐 03 §1.14 的校验规则。错误信息必须枚举合法取值。"""
    errs: list[str] = []
    fm = f.frontmatter

    status_val = fm.get("status")
    for key in ("agent_id", "purpose", "status", "tools_used", "citations", "self_assessed_strength",
                "confidence", "coverage"):
        if key == "tools_used" and status_val in ("failed", "timeout"):
            # failed/timeout 是失败态，未必执行过任何工具（例如 LLM 一进来就崩），
            # 若仍硬性要求 tools_used 非空，真实失败原因会被 failed_stub 的
            # 「tools_used: []」盖成一条误导性的「findings 校验未通过」。失败态放行。
            continue
        if key not in fm or fm[key] in ("", None, []):
            errs.append(f"frontmatter 缺必填字段或为空：{key}")

    if fm.get("status") and fm["status"] not in ("succeeded", "failed", "timeout"):
        errs.append(f"status 取值非法：{fm['status']!r}（只允许 succeeded / failed / timeout）")
    if fm.get("confidence") and fm["confidence"] not in CONFIDENCE:
        errs.append(f"confidence 取值非法：{fm['confidence']!r}（只允许 CONFIRMED / LIKELY / INCONCLUSIVE）")
    if fm.get("self_assessed_strength") and fm["self_assessed_strength"] not in STRENGTH:
        errs.append(f"self_assessed_strength 取值非法：{str(fm['self_assessed_strength'])[:20]!r}"
                    "（只允许 strong / moderate / weak / inconclusive）")

    cites = fm.get("citations") or []
    if not isinstance(cites, list) or not cites:
        errs.append("citations 不得为空：请填写你实际调用过的 step 编号，形如 citations: [step_1, step_3]")
    elif known_step_ids:
        unresolved = [c for c in cites if isinstance(c, str) and re.fullmatch(r"step[_-]?\d+", c or "") and c not in known_step_ids]
        if unresolved:
            errs.append(f"citations 指向不存在的 step：{unresolved}（本次实际执行到 {sorted(known_step_ids)[-1] if known_step_ids else 0} 步）")

    for title in REQUIRED_H2:
        if not _section(f.body, title):
            errs.append(f"缺少必需章节或内容为空：## {title}")

    impact = fm.get("impact")
    if not isinstance(impact, dict):
        errs.append("frontmatter 缺 impact 影响面四分类（必须四个子键齐全：affected / checked_healthy / "
                    "changed_other_reason / not_checked，无内容写 []）")
    else:
        for k in IMPACT_KEYS:
            if k not in impact:
                errs.append(f"impact 缺分类：{k}（无内容也要写 {k}: []）")
            elif not isinstance(impact[k], list):
                # D20 修复：原来是 `impact.get(k) or []` 把非列表值悄悄当空列表，
                # 于是"affected 每项必须带 evidence_token"这条规则**永远不触发**
                # （模型写 `affected: demo-db-01` 这种标量也能过）。必须显式报错。
                errs.append(f"impact.{k} 必须是列表（形如 {k}: [{{entity: ..., evidence_token: ...}}] 或 {k}: []），"
                            f"实际是 {type(impact[k]).__name__}: {str(impact[k])[:40]!r}")
        affected = impact.get("affected") or []
        if affected and not isinstance(affected, list):
            errs.append("impact.affected 必须是列表（每项 {entity, evidence_token}）")
        elif isinstance(affected, list):
            for item in affected:
                if isinstance(item, dict):
                    if not item.get("entity"):
                        errs.append("impact.affected 项缺 entity")
                    if not (item.get("evidence_token") or item.get("reason")):
                        errs.append(f"impact.affected 项缺 evidence_token（实体 {item.get('entity')} 必须带证据 token）")
                elif isinstance(item, str) and "(" not in item:
                    errs.append(f"impact.affected 文本项缺少括号证据 token：{item}")

    # P1-11：调查覆盖清单。每项写查到的结论，或写 "not_checked: 原因"。
    coverage = fm.get("coverage")
    if coverage is not None and not isinstance(coverage, dict):
        errs.append("coverage 必须是对象（error_signal/recent_change/runtime_state/history/blast_radius 五项）")
    elif isinstance(coverage, dict):
        for k in COVERAGE_KEYS:
            if not coverage.get(k):
                errs.append(f"coverage 缺 {k}（查了就写结论，没查写 not_checked: 原因）")

    # P1-9：多步/多服务处置的结构化计划（可选；有则必须每步带证据与回滚）。
    # 不强制所有报告都写，避免把“只读结论”逼出无意义计划；但写了就要能落地。
    plan = fm.get("action_plan")
    if plan not in (None, "", []):
        if not isinstance(plan, list):
            errs.append("action_plan 必须是列表（每项含 step/action/evidence/rollback）")
        else:
            for i, item in enumerate(plan[:20], 1):
                if not isinstance(item, dict):
                    errs.append(f"action_plan 第 {i} 项必须是对象（含 action/evidence/rollback）")
                    continue
                for k in ("action", "evidence", "rollback"):
                    if not item.get(k):
                        errs.append(f"action_plan 第 {i} 项缺 {k}（动作必须有证据与回滚方式）")
    return errs


def validate_text(text: str, known_step_ids: set[str] | None = None) -> tuple[Findings, list[str]]:
    f = parse_findings(text)
    f.normalize_notes = normalize_findings(f)
    errs = validate_findings(f, known_step_ids)
    f.errors = errs
    return f, errs


def failed_stub(reason: str, tools_used: list[str] | None = None) -> str:
    """校验重试后仍失败 → 写 stub（报告标注可重试，服务不挂）。"""
    return f"""---
agent_id: rca-1
purpose: 自动根因诊断（生成失败）
status: failed
tools_used: [{", ".join(tools_used or [])}]
citations: [（无：生成失败）]
self_assessed_strength: inconclusive
confidence: INCONCLUSIVE
impact:
  affected: []
  checked_healthy: []
  changed_other_reason: []
  not_checked: [（全部：诊断未产出有效结论）]
coverage:
  error_signal: not_checked: 诊断失败
  recent_change: not_checked: 诊断失败
  runtime_state: not_checked: 诊断失败
  history: not_checked: 诊断失败
  blast_radius: not_checked: 诊断失败
---

## 结论

诊断报告生成失败，可重新发起诊断。

失败原因：{reason}

## 证据

（无：未产出可引用证据）

## 推理

（无）

## 已排除的假设

（无）
"""
