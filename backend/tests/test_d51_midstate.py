"""D51：耗时动作的"中间态"回归锁（用户：「涉及到中间态的，比如生成报告，这个等待的过程
要有中间态，全局检查」）。

问题（实测）：等待期间没有反馈 = 用户不知道"在跑还是死了" → 重复点击。
- 巡检「立即执行」原来只有一条 3.2s 就消失的 toast，按钮还能再点 → 可能并发跑两次巡检；
- 「测试」大模型连通要 10~20s、技能「连接检查」要打一次目标机，期间按钮毫无变化；
- 工作台/看板的批量操作按条数发多次请求，同样没有任何进行中状态。

锁四件事：
① 中间态原语存在（ui.js 的 busy()/progress()，ops.css 的 .busy/.task-progress）；
② **全局**：任何页面 onclick 处理函数里出现 `await api.post|put|del`（会改状态、可能慢）
   都必须带中间态（busy/disabled/progress），不允许"点了没反应"；
③ 长任务额外要求：巡检要有区块进行中行（progress），诊断要有已用时间；
④ 中间态必须能**收尾**：诊断的 SSE 断开要有重试上限（否则界面无限显示"已用 Ns"）。
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"
PAGES = sorted((STATIC / "js").rglob("*.js"))


def _handlers(src: str) -> list[str]:
    """抓出所有 `onclick: ...` 的处理体（括号配平，含箭头函数的表达式体）。"""
    out = []
    for m in re.finditer(r"onclick\s*:\s*", src):
        i = m.end()
        depth, j = 0, i
        started = False
        while j < len(src):
            c = src[j]
            if c in "([{":
                depth += 1
                started = True
            elif c in ")]}":
                depth -= 1
                if started and depth <= 0:
                    break
            elif c == "," and depth == 0 and started:
                break
            j += 1
        out.append(src[i:j])
    return out


def test_midstate_primitives_exist():
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    assert "export async function busy(" in ui, "缺按钮级中间态原语 busy()"
    assert "export function progress(" in ui, "缺区块级进行中行原语 progress()"
    assert "sec >= 2" in ui, "长操作应在 2 秒后显示已用秒数（短操作不闪）"
    assert "el.innerHTML = prevHtml" in ui, "busy() 收尾必须恢复原内容"
    assert ".busy {" in css and ".task-progress {" in css, "中间态样式缺失"
    assert "--pointer-events: none" in css.replace("  ", " ") or "pointer-events: none" in css


def test_every_mutating_handler_has_midstate():
    """② 全局：任何会改状态且可能慢的点击处理，都必须有中间态。"""
    bad = []
    for f in PAGES:
        src = f.read_text(encoding="utf-8")
        for h in _handlers(src):
            if not re.search(r"await api\.(post|put|del)\(", h):
                continue
            if re.search(r"busy\(|disabled\s*=\s*true|progress\(|withBusy\(", h):
                continue
            # 纯"先弹窗/先校验"的分支不算（不含 await api 的调用已经过滤掉了）
            snippet = re.sub(r"\s+", " ", h)[:90]
            bad.append(f"{f.name}: {snippet}")
    assert not bad, "以下点击处理会发写请求但没有中间态（用户会以为没反应/重复点击）：\n" + "\n".join(bad)


def test_long_tasks_have_richer_feedback():
    insp = (STATIC / "js" / "pages" / "inspections.js").read_text(encoding="utf-8")
    inc = (STATIC / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    # 巡检：区块进行中行（含"巡检执行中"），且两个入口都接上
    assert insp.count("progress(body,") >= 2, "巡检两个入口（立即执行 / 立即巡检一次）都要有进行中行"
    assert "已用" in inc and "setInterval" in inc, "诊断要有已用时间计数"
    assert 'btn.textContent = "诊断中…"' in inc, "诊断按钮要有进行中文案"


def test_midstate_can_finish_on_failure():
    """④ 中间态不能永远转：诊断 SSE 连接失败要有重试上限并恢复按钮。"""
    inc = (STATIC / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    assert "sse.onerror" in inc, "SSE 必须有 onerror 收尾"
    assert "errs < 3" in inc, "EventSource 会自动重连，必须给重试上限（否则无限显示已用时间）"
    assert 'btn.textContent = "重新诊断"' in inc
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "clearInterval(timer)" in ui and "finally" in ui, "busy() 必须在失败路径也恢复按钮"
