"""D159：`**` 星号泄漏守卫——字符串里的 markdown 强调必须走会渲染的出口。

背景（缺陷 D159-1，用户截图实锤）：通知路由页把带 `**加粗**` 的文案直接塞进
`h()` 的纯文本通道 → 星号原样显示（"…**由新规则接管**…"）。平台的行内强调
渲染出口只有四个（ui.js）：`note()` / `helpTip()` / `thLabel()` / `rich()`
——先整体转义再把 `**x**` 变 `<strong>`，安全且生效。

本守卫对 `static/js` **全量**扫描（用户要求"一定要全局扫"，此守卫保证以后也全
局有）：
  ① 先剥块注释与行注释（JSDoc 里的 ** 说明不算）；
  ② 逐个提取含 `**` 的字符串字面量（纯星号掩码如 `"***"` 跳过——那是脱敏显示）;
  ③ 对每个字面量做 **enclosing-call 回溯**：找到包住它的调用名，必须 ∈ 渲染白名单；
  ④ 具名例外：hosts.js 的 pfItems 清单——值在数组里定义、由稍后的
     `html: rich(v)` 消费（机械判不出来），显式白名单登记；改渲染方式时同步删。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS_DIR = ROOT / "static" / "js"

# 会渲染 **加粗** 的出口（ui.js rich() 家族 + marked 全量 markdown）
MD_SINKS = {"note", "helpTip", "thLabel", "rich", "marked.parse"}

# 具名例外：字面量内容前缀 → 允许（必须写清消费方，改渲染时同步删）
DEFERRED_OK = {
    "hosts.js": [
        ("能否**出站**", "pfItems 数组，消费方 hosts.js checkList → html: rich(v)"),
    ],
}

# 文件级局部渲染器：同名函数在别的文件未必是渲染出口（settings.js 的 hint 是个
# replaceChildren 的 div），所以按文件登记，不做全局白名单。
SINKS_BY_FILE = {
    "hosts.js": {"hint"},   # hosts.js 本地 hint() = h(html: rich(text))
}

def _iter_string_literals(src: str):
    """产出 (start, text) 字符串/模板字面量——注释与**正则字面量感知**的小型扫描器。

    为什么不能用朴素正则：incidents.js 里有 `` /```[a-z]*/g `` 这类**正则里含反引号**
    的写法，天真配对会把两段无关代码连成"跨行大模板"，把合法的 note() 误判成泄漏。
    """
    i, n = 0, len(src)
    prev_significant = ""    # 上一个有效字符（判断 / 是正则还是除号）
    prev_word = ""           # 上一个标识符（return / typeof … 后跟的是正则）
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and src[i + 1:i + 2] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and src[i + 1:i + 2] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
            yield i, src[i:j + 1]
            prev_significant, prev_word, i = '"', "", j + 1
            continue
        if c == "`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "`":
                    break
                if src[j] == "$" and src[j + 1:j + 2] == "{":
                    depth, k = 1, j + 2     # 跳过 ${…} 插值（容忍内层字符串）
                    while k < n and depth:
                        ch = src[k]
                        if ch in "'\"":
                            q = k + 1
                            while q < n:
                                if src[q] == "\\":
                                    q += 2
                                    continue
                                if src[q] == src[k]:
                                    break
                                q += 1
                            k = q
                        elif ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                        k += 1
                    j = k - 1
                j += 1
            yield i, src[i:j + 1]
            prev_significant, prev_word, i = "`", "", j + 1
            continue
        if c == "/":
            regex_ctx = (prev_significant == "" or prev_significant in "(=:[!&|?{};,+-*%<>~^"
                         or prev_word in ("return", "typeof", "case", "in", "of", "new",
                                          "do", "else", "void", "delete", "throw"))
            if regex_ctx:
                j, in_class = i + 1, False
                while j < n:
                    ch = src[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        break
                    elif ch == "\n":
                        break
                    j += 1
                k = j + 1
                while k < n and src[k].isalpha():
                    k += 1
                prev_significant, prev_word, i = "/", "", k
                continue
            prev_significant, i = "/", i + 1
            continue
        if c.isalnum() or c in "_$":
            m = re.match(r"[\w$]+", src[i:])
            prev_word, prev_significant = m.group(0), c
            i += m.end()
            continue
        prev_significant, prev_word = c, ""
        i += 1


def _strip_comments(src: str) -> str:
    """块注释与行注释置空（保留行号，换成等长空格）。
    启发式局限：字符串里的 `//`（如 URL）也会被剥掉——若星号跟在同行 URL 后会漏检；
    当前全站无此形态（实测核过），出现新形态时收紧解析器。"""

    def blank_block(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    no_block = re.sub(r"/\*.*?\*/", blank_block, src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", lambda m: " " * len(m.group(0)), ln)
                     for ln in no_block.splitlines())


def _enclosing_call(src: str, pos: int) -> str:
    """从 pos 向前找包住它的调用名（括号配对）；找不到返回 ""。

    分组括号（三元/加法外面套一层 `(…)`，前面不是函数名）视为透明，继续向外找。
    """
    depth = 0
    i = pos - 1
    while i >= 0:
        c = src[i]
        if c in ")]}":
            depth += 1
        elif c in "([{":
            if depth == 0:
                m = re.search(r"([A-Za-z_$][\w$.]*)\s*$", src[:i])
                if m:
                    return m.group(1)
                # 分组括号（前面不是标识符）→ 透明，继续向外
            else:
                depth -= 1
        i -= 1
    return ""


def test_no_md_bold_outside_render_sinks():
    """全站 js：** 字面量只允许出现在渲染出口内（或具名延迟消费白名单）。"""
    fails: list[str] = []
    checked = 0
    for f in sorted(JS_DIR.rglob("*.js")):
        src = f.read_text(encoding="utf-8")
        stripped = _strip_comments(src)
        sinks = MD_SINKS | SINKS_BY_FILE.get(f.name, set())
        for start, lit in _iter_string_literals(stripped):
            if "**" not in lit:
                continue
            body = lit[1:-1]
            if re.fullmatch(r"\*+", body):      # "***" 脱敏掩码，不是 markdown
                continue
            checked += 1
            if any(body.startswith(p) for p, _ in DEFERRED_OK.get(f.name, [])):
                continue
            call = _enclosing_call(stripped, start)
            if call in sinks:
                continue
            fails.append(f"{f.relative_to(ROOT)}: 字面量含 ** 但渲染出口是 {call or '（无调用/字面量容器）'} → {lit[:60]}")
    assert not fails, "** 星号会原样显示给用户（应改走 note/helpTip/thLabel/rich）：\n" + "\n".join(fails)
    assert checked > 0, "一处 ** 字面量都没扫到（正则失效了？）"


def test_rich_renders_bold_and_escapes():
    """渲染出口本身有效：rich() 出 <strong>、防注入（守卫的出口不是空承诺）。"""
    src = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    assert re.search(r"export const rich\s*=", src), "rich() 出口不存在（改名了？守卫要跟着改）"
    rich_src = src[src.index("export const rich"):src.index("export const rich") + 400]
    assert "<strong>" in rich_src and "&amp;" in rich_src or "esc(" in rich_src, \
        "rich() 必须先转义再替换（防 innerHTML 注入）"
