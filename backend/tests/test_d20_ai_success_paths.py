"""D20 回归：LLM 恢复可用后真跑 AI 路径才暴露的一串问题。

背景：之前 MiniMax 额度耗尽（429），"AI 成功路径"根本无法验证，我把它列为"真阻塞项"。
额度恢复后立刻真跑，**连着抓到 4 个问题**，且都属于"读代码看不出来"的那类：

  D20-1 【500】`POST /api/ops/incidents/{iid}/diagnose` 是长任务（多步 ReAct，实测 101.5s）
        却没登记进 25s DB 兜底豁免 → 500 @25.0s。
        （根因与 D19-1 同源：只修被点名的实例，不修那一类。已在 main.py 改为
          LONG_TASK_ROUTES 表 + 机械守卫测试。）

  D20-2 【结论被丢】接口返回 200、跑了 15 步、烧掉 84,319 tokens，产物却是
        `status: failed` / `citations: [（无：生成失败）]` —— 只因 `impact:` 里漏了
        checked_healthy / changed_other_reason / not_checked 三个**空列表**键。
        而 thoughts 轨迹显示模型推理链是正确可用的。→ 补 [] 而非判废。

  D20-3 【校验被绕过】`impact.<k>` 非列表时（如 `affected: demo-db-01` 标量），
        原代码 `impact.get(k) or []` 把它当空列表 → "affected 每项必须带
        evidence_token"这条规则**永远不触发**（死规则）。
        → 非列表必须显式报错。

  D20-4 【解析错位】`_mini_yaml` 用"行首是否 \\w"判子键，于是模型自然写出的**块序列**形式
        （`  affected:` 换行 `    - entity: x`）会把 `affected` 变成空字符串、
        把 `- entity: x` 塞成**顶层键**、并把紧随的 `  checked_healthy: []` **泄漏到顶层**
        → 模型明明写对了，校验器却报"impact 缺分类：checked_healthy"。
        → 改为缩进感知解析器（支持标量序列与对象序列）。
"""
import pytest

from app.services import findings as F


def _report(impact_block: str, citations: str = "[step_1, step_2]") -> str:
    """构造一份除 impact 外全部合规的报告（frontmatter 必须有闭合的 ---）。"""
    return f"""---
agent_id: rca-1
purpose: 自动根因诊断
status: succeeded
tools_used: [query_metrics, read_log_tail]
citations: {citations}
self_assessed_strength: moderate
confidence: LIKELY
impact:
{impact_block}
coverage:
  error_signal: checked
  recent_change: not_checked: 无部署数据
  runtime_state: checked
  history: checked
  blast_radius: checked
---

## 结论

磁盘使用率持续上升导致写入失败。

## 证据

- disk.percent 从 70% 涨到 88%（step_1）
- 日志显示 No space left on device（step_2）

## 推理

慢日志未轮转，持续占用磁盘。

## 已排除的假设

- 排除 CPU
"""


REQUIRED_IMPACT = ("affected", "checked_healthy", "changed_other_reason", "not_checked")


# ── D20-2：漏写空的 impact 分类 → 补 []，不判废 ──
def test_missing_empty_impact_keys_are_normalized_not_rejected():
    """漏写空的 impact 分类 → 自动补 []，**不得**判废整份报告（D20-2 核心回归）。"""
    text = _report("  affected: [{entity: demo-db-01, evidence_token: step_1}]")  # 模型实测的漏写法
    f, errs = F.validate_text(text)
    assert not errs, f"补空分类后不应再有校验错误，实际：{errs}"
    for k in REQUIRED_IMPACT:
        assert k in f.frontmatter["impact"], f"{k} 应被补齐"
        assert isinstance(f.frontmatter["impact"][k], list)
    assert any("impact" in n for n in f.normalize_notes), "补键动作应记录进 normalize_notes"


def test_impact_entirely_missing_still_rejected():
    """impact 整块缺失仍判废——只补"块内缺的空列表键"，不替模型凭空造结论。"""
    text = _report("  # 模型完全没写 impact 的分类")
    f, errs = F.validate_text(text)
    assert errs, "impact 不是 dict 时必须仍然报错（不能因为修 D20-2 就把校验放空）"


# ── D20-4：块序列形式必须被正确解析（模型自然会这么写） ──
def test_block_sequence_impact_is_parsed_correctly():
    """`sub:` 换行 `  - k: v` 的对象序列形式必须解析成 [{...}]，且不污染顶层键。"""
    text = _report("""  affected:
    - entity: demo-db-01
      evidence_token: step_1
  checked_healthy: [demo-web-01]
  changed_other_reason: []
  not_checked: [mock-db-01]""")
    f, errs = F.validate_text(text)
    assert "impact" in f.frontmatter
    imp = f.frontmatter["impact"]
    assert imp["affected"] == [{"entity": "demo-db-01", "evidence_token": "step_1"}], \
        f"块序列应解析成对象列表，实际 {imp['affected']!r}"
    assert imp["checked_healthy"] == ["demo-web-01"], "紧随的缩进键不得泄漏到顶层"
    assert imp["not_checked"] == ["mock-db-01"]
    assert not any(k.startswith("- ") or k.startswith("-") for k in f.frontmatter), \
        f"序列项不得被当成顶层键，实际顶层键 {list(f.frontmatter)}"
    assert not errs, f"模型写对了就不该报错，实际：{errs}"


def test_block_sequence_of_scalars_is_parsed():
    """`sub:` 换行 `  - 标量` 的标量序列也要解析成列表。"""
    fm = F._mini_yaml("""agent_id: rca-1
impact:
  affected: []
  checked_healthy:
    - demo-web-01
    - demo-cache-01
  changed_other_reason: []
  not_checked: []""")
    assert fm["impact"]["checked_healthy"] == ["demo-web-01", "demo-cache-01"]


def test_inline_forms_still_work():
    """提示词规定的行内形式不得被新解析器破坏。"""
    fm = F._mini_yaml("""a: 1
impact:
  affected: [{entity: e1, evidence_token: step_1}, {entity: e2, evidence_token: step_2}]
  checked_healthy: []
  changed_other_reason: []
  not_checked: []""")
    assert fm["impact"]["affected"] == [
        {"entity": "e1", "evidence_token": "step_1"},
        {"entity": "e2", "evidence_token": "step_2"},
    ]
    assert fm["impact"]["checked_healthy"] == []


def test_top_level_scalars_and_inline_lists_unaffected():
    """回归：顶层普通键/行内列表/引号仍然照旧。"""
    fm = F._mini_yaml("""agent_id: rca-1
tools_used: [query_metrics, host_info]
confidence: LIKELY
purpose: "带引号的说明"
empty_block:
  a: x""")
    assert fm["agent_id"] == "rca-1"
    assert fm["tools_used"] == ["query_metrics", "host_info"]
    assert fm["confidence"] == "LIKELY"
    assert fm["purpose"] == "带引号的说明"
    # 值一律按字符串处理（解析器不猜类型，与既有行为一致）
    assert fm["empty_block"] == {"a": "x"}


# ── D20-3：非列表的 impact 值必须报错（不得静默当空） ──
@pytest.mark.parametrize("impact_block,expect_key", [
    ("  affected: demo-db-01\n  checked_healthy: []\n  changed_other_reason: []\n  not_checked: []", "affected"),
    ("  affected: []\n  checked_healthy: demo-web-01\n  changed_other_reason: []\n  not_checked: []", "checked_healthy"),
    ("  affected: []\n  checked_healthy: []\n  changed_other_reason: 无\n  not_checked: []", "changed_other_reason"),
    ("  affected: []\n  checked_healthy: []\n  changed_other_reason: []\n  not_checked: 还没查", "not_checked"),
])
def test_non_list_impact_values_are_rejected(impact_block, expect_key):
    """impact 子键写成标量 → 必须显式报错（否则"必须有证据锚点"的规则变成死规则）。"""
    f, errs = F.validate_text(_report(impact_block))
    assert any(f"impact.{expect_key} 必须是列表" in e for e in errs), \
        f"{expect_key} 为标量必须报错，实际：{errs}"


def test_affected_item_still_requires_evidence_token():
    """不放松：affected 有内容时每项仍必须带 evidence_token（防无证据断言）。"""
    for impact in (
        "  affected: [{entity: demo-db-01}]\n  checked_healthy: []\n  changed_other_reason: []\n  not_checked: []",
        "  affected:\n    - entity: demo-db-01\n  checked_healthy: []\n  changed_other_reason: []\n  not_checked: []",
    ):
        f, errs = F.validate_text(_report(impact))
        assert any("evidence_token" in e for e in errs), f"缺证据锚点必须报错，实际：{errs}"


def test_citations_auto_extracted_from_body_when_empty():
    """不放松：citations 空但正文引用了 step → 从正文提取（既有规范化，非新增放水）；
    正文也无引用 → 仍报错。"""
    text = _report("  affected: []\n  checked_healthy: []\n"
                   "  changed_other_reason: []\n  not_checked: []", citations="[]")
    f, errs = F.validate_text(text)
    assert not errs, f"正文有 step 引用应能自动补 citations，实际：{errs}"
    assert f.frontmatter["citations"] == ["step_1", "step_2"]

    no_ref = _report("  affected: []\n  checked_healthy: []\n"
                     "  changed_other_reason: []\n  not_checked: []", citations="[]")
    no_ref = no_ref.replace("（step_1）", "").replace("（step_2）", "")
    f2, errs2 = F.validate_text(no_ref)
    assert any("citations" in e for e in errs2), f"正文也无引用时必须报错，实际：{errs2}"
