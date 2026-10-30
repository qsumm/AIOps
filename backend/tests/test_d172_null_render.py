"""D172-1：诊断失败 stub 报告渲染出 "null" 字样 + 无意义的处置按钮。

**用户实测**（2026-10-22）：事件 #2000065 诊断失败（LLM 双通道不可用），
报告卡顶部印出独立的 "null" 一行，且失败报告下方仍显示跟进/观察/忽略按钮。

两个根因：
  ① `renderTriage` 的 `box.replaceChildren(cond ? x : null, ...)` —— DOM 规范把
     null 转成文本节点 → 页面凭空出现一行 "null"（P5 终验踩过、ui.js fill() 的
     注释明确警告"别再手写"——写代码前没查工具箱，流程问题）；
  ② 失败 stub（status=failed）没有可分诊的内容，处置按钮不应渲染。
"""
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"


def _strip_js_comments(src: str) -> str:
    """去掉行注释与块注释。

    守卫要抓的是**代码里真的调用了 replaceChildren**，不是注释里提到它——
    D172-1 的注释本身就写着"别手写 replaceChildren(null)"，不剥注释会让守卫误报。
    """
    import re

    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//")[0] for line in src.splitlines())


def test_incidents_no_conditional_null_in_replacechildren():
    """renderTriage 不得手写 replaceChildren 传条件 null（D172-1）。"""
    js = (STATIC / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    assert 'box.replaceChildren(...parts.filter(Boolean))' not in js, (
        "应使用 ui.js 的 fill()（自动丢弃 null），而不是手写 filter——手写早晚再漏")
    tri = _strip_js_comments(js[js.index("function renderTriage"):js.index("function renderReport")])
    assert "replaceChildren(" not in tri, (
        "renderTriage 的子节点写入必须走 fill()（自动丢弃 null）——"
        "手写 replaceChildren 会把 null 印成「null」（D172-1 现场）")


def test_failed_stub_shows_friendly_hint_not_triage_buttons():
    """失败 stub：显示"请重新发起诊断"的友好提示，不渲染三态处置按钮。

    D175-1 更新判定口径：失败与否看**有无结论正文**（hasConclusion = 正文 >200 字
    且不是 failed/timeout stub），不再看 status 字段——status 判定在字段形状变化时
    会把成功报告整体误判为失败（#2000065 现场：strong/CONFIRMED 报告被写"生成失败"）。
    """
    js = (STATIC / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    seg = js[js.index("function renderTriage"):js.index("function renderReport")]
    assert "hasConclusion" in seg, "应以 hasConclusion（有无结论正文）判定可分诊性"
    assert "没有可分诊的内容" in seg, "失败态应有可操作的友好提示"
    # 失败分支里不得渲染三态按钮（按钮只在成功分支）
    failed_branch = seg[seg.index("if (!hasConclusion)"):seg.index("} else {")]
    assert "chip(" not in failed_branch, "失败态不得渲染处置按钮"
    ok_branch = seg[seg.index("} else {"):seg.index("fill(box")]
    assert 'chip("followup"' in ok_branch, "成功分支保留处置按钮"
