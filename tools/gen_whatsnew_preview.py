#!/usr/bin/env python3
"""生成「更新公告弹窗」设计预览单文件（FR-421 / D133）。

为什么要生成器而不是手写预览页：
  预览页如果手写，它和平台里真正跑的那套样式/插画**一定会漂移**——改了实现，预览还是旧的，
  评审就看了个假的。本生成器把几处真源拼成一份可直接双击打开的 HTML：

    ① 设计令牌   ← static/ops.css 的 :root / [data-theme="light"] 两块（原文照抄）
    ② 弹窗样式   ← static/ops-whatsnew.css（原文照抄）
    ③ 右侧插画   ← static/js/wn_pixelscene.js（原文内联）+ static/img/whatsnew-scene.png（本地像素画资产）
    ④ 演示文案   ← static/js/changelog.js 的第一条 major（真实发版内容）

D133 起右栏是**代码手绘像素画**（不再是 SVG 模板字符串），所以这里把像素模块整段内联，
预览里画出来的就是平台里画出来的那张图——不会漂移。

用法：
    python3 tools/gen_whatsnew_preview.py
    # 产物：docs/04-交付与运营/更新公告弹窗-预览.html
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS_CSS = ROOT / "static" / "ops.css"
WN_CSS = ROOT / "static" / "ops-whatsnew.css"
WN_SCENE = ROOT / "static" / "js" / "wn_pixelscene.js"
CL_JS = ROOT / "static" / "js" / "changelog.js"
OUT = ROOT / "docs" / "04-交付与运营" / "更新公告弹窗-预览.html"


def _tokens() -> str:
    """从 ops.css 里抠出两块令牌（含 :root/暗色 与 light）。"""
    css = OPS_CSS.read_text(encoding="utf-8")
    start = css.index(":root,")
    end = css.index("/* ===== 骨架")
    return css[start:end].strip()


def _scene_module() -> str:
    """像素画模块原文（去掉 ESM 导出关键字，供页面内联调用）。"""
    src = WN_SCENE.read_text(encoding="utf-8")
    assert "mountPixelScene" in src, "wn_pixelscene.js 里找不到 mountPixelScene"
    return src.replace("export function mountPixelScene", "function mountPixelScene")


def _first_release() -> dict:
    """取 changelog.js 的第一条（真实发版内容）。"""
    src = CL_JS.read_text(encoding="utf-8")
    body = re.search(r"export const RELEASES = \[(.*?)\n\];", src, re.S).group(1)
    first = re.split(r"\n  \{", body)[1]

    def pair(key: str) -> tuple[str, str]:
        mm = re.search(
            rf'\b{key}:\s*\{{\s*zh:\s*"((?:[^"\\]|\\.)*)"\s*,\s*en:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*\}}',
            first, re.S)
        assert mm, f"changelog.js 首条缺 {key}"
        return mm.group(1), mm.group(2)

    def scalar(key: str) -> str:
        mm = re.search(rf'\b{key}:\s*"([^"]*)"', first)
        return mm.group(1) if mm else ""

    # highlights：弹窗「更新要点」用的短句版（title/desc/stat/statLabel）
    hl_block = re.search(r"highlights:\s*\[(.*?)\n    \],", first, re.S)
    highlights = []
    if hl_block:
        for chunk in re.split(r"\n      \},\n      \{", hl_block.group(1)):
            def p2(key: str, c: str = chunk) -> tuple[str, str]:
                mm = re.search(
                    rf'\b{key}:\s*\{{\s*zh:\s*"((?:[^"\\]|\\.)*)"\s*,\s*en:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*\}}',
                    c, re.S)
                return (mm.group(1), mm.group(2)) if mm else ("", "")
            highlights.append({
                "title": p2("title"), "desc": p2("desc"),
                "stat": p2("stat"), "statLabel": p2("statLabel"),
            })
    return {
        "title": pair("title"), "summary": pair("summary"), "label": pair("label"),
        "date": scalar("date"), "version": scalar("version"),
        "highlights": highlights,
    }


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PAGE = """<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8" />
<title>更新公告弹窗 · 设计预览（D133）</title>
<style>
__TOKENS__
__WN_CSS__
body { margin: 0; padding: 28px; background: var(--bg); color: var(--text);
       font-family: -apple-system, "SF Pro Text", "PingFang SC", system-ui, sans-serif; }
.preview-note { max-width: 790px; margin: 0 auto 18px; font-size: 12.5px; color: var(--muted); line-height: 1.7; }
.preview-note code { font-family: var(--font-mono); }
</style>
</head>
<body>
<div class="preview-note">
  这是「更新公告弹窗（major）」的<strong>设计预览单文件</strong>，由 <code>tools/gen_whatsnew_preview.py</code>
  从真源生成：令牌取自 <code>static/ops.css</code>，样式取自 <code>static/ops-whatsnew.css</code>，
  右侧像素画由 <code>static/js/wn_pixelscene.js</code> 实时绘制（与平台里跑的是同一份代码），
  文案取自 <code>static/js/changelog.js</code> 的首条 major。平台里的真实弹窗由
  <code>static/js/whatsnew.js</code> 渲染，行为（已读记录、Esc 关闭、焦点圈定、更新中心）以平台为准。
</div>
<div class="wn2-mask open" style="position:static;opacity:1;padding:0;background:transparent">
  <div class="wn2-modal open" style="position:static;transform:none;opacity:1;margin:0 auto">
    <button class="wn2-close" type="button" aria-label="关闭">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
    <div class="wn2-main">
      <div class="wn2-head">
        <span class="wn2-pill mono">v__RELEASE_VER__</span>
        <span class="wn2-when">__DATE__</span>
      </div>
      <h2 class="wn2-title">__TITLE__</h2>
      <p class="wn2-sub">__SUMMARY__</p>
      <div class="wn2-list-label">更新要点</div>
      <div class="wn2-list">
__HL_ROWS__
      </div>
      <div class="wn2-foot">
        <button class="btn" type="button">开始体验</button>
        <button class="btn secondary" type="button">看完整更新日志</button>
      </div>
    </div>
    <div class="wn2-art">
      <div class="wn2-scene" id="scene"></div>
      <div class="wn2-caption mono"><span>OPS · 值班中</span><span>v__RELEASE_VER__</span></div>
    </div>
  </div>
</div>
<script>
__SCENE_JS__
document.getElementById("scene").append(mountPixelScene({ src: "../../static/img/whatsnew-scene.png" }).el);
</script>
</body>
</html>
"""


def main() -> None:
    rel = _first_release()
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    hl_rows = []
    for hl in rel["highlights"]:
        hl_rows.append(
            '        <div class="wn2-item">\n'
            '          <div class="wn2-item-main">\n'
            f'            <div class="wn2-item-title">{_esc(hl["title"][0])}</div>\n'
            f'            <div class="wn2-item-desc">{_esc(hl["desc"][0])}</div>\n'
            '          </div>\n'
            '          <div class="wn2-item-stat">\n'
            f'            <div class="wn2-stat-n mono">{_esc(hl["stat"][0])}</div>\n'
            f'            <div class="wn2-stat-l">{_esc(hl["statLabel"][0])}</div>\n'
            '          </div>\n'
            '        </div>')
    if not hl_rows:   # 没有 highlights 的条目：退回 items 摘要，保证预览不为空
        for lead, _ in rel.get("items", []):
            hl_rows.append('        <div class="wn2-item"><div class="wn2-item-main">'
                           f'<div class="wn2-item-title">{_esc(lead)}</div></div></div>')

    html = (PAGE
            .replace("__TOKENS__", _tokens())
            .replace("__WN_CSS__", WN_CSS.read_text(encoding="utf-8").strip())
            .replace("__SCENE_JS__", _scene_module())
            .replace("__HL_ROWS__", "\n".join(hl_rows))
            .replace("__TITLE__", _esc(rel["title"][0]))
            .replace("__SUMMARY__", _esc(rel["summary"][0]))
            .replace("__DATE__", rel["date"])
            .replace("__RELEASE_VER__", rel["version"] or ver)
            .replace("__VERSION__", ver))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成：{OUT}（{len(html)} 字节，源：ops.css + ops-whatsnew.css + wn_pixelscene.js + changelog.js）")


if __name__ == "__main__":
    main()
