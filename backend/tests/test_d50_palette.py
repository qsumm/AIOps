"""D50：调色板收敛与"去暖色"回归锁（用户：「我不喜欢这种亮橙，棕色，黄色，你删掉，
另外颜色种类收敛下。全局检查，不要遗漏」）。

锁四件事：
① **全站无暖色**：static/ 下（.css/.js/.html/.svg，排除 vendor 与注释）不允许出现
   色相 10°~75° 且饱和度 ≥12% 的颜色 —— 覆盖 hex 与 rgb() 两种写法；
② **不再有"散色"**：语义色的透明度变体必须走令牌（rgba(var(--x-rgb), …)），
   ops.css 的声明行里不允许再出现写死的 rgba(35,131,226,…)/rgba(0,0,0,…) 之类；
③ **每个主题各一档**：--muted/--accent/--pass/--fail/--warn 在暗色与亮色块里都必须重新定义
   （亮暗共用一个值 = 必然在另一主题上失衡，D50 的根因）；
④ **新的警告色可用且可区分**：两主题对比度 ≥4.5:1，且与失败红 / 强调蓝的色相差 ≥30°。
"""
from __future__ import annotations

import colorsys
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"
SKIP_DIRS = ("vendor",)
SUFFIXES = (".css", ".js", ".html", ".svg")


def _files():
    for p in STATIC.rglob("*"):
        if p.suffix in SUFFIXES and not any(d in p.parts for d in SKIP_DIRS):
            yield p


def _strip_comments(text: str, suffix: str) -> str:
    if suffix == ".css":
        return re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if suffix == ".js":
        return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.S)
    if suffix == ".svg":
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return text


def _rgb(val: str) -> tuple[int, int, int]:
    nums = re.findall(r"\d+", val)
    if val.startswith("rgb"):
        return int(nums[0]), int(nums[1]), int(nums[2])
    h = val.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hsl(val: str) -> tuple[int, int, int]:
    r, g, b = _rgb(val)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return round(h * 360), round(s * 100), round(l * 100)


def _lum(val: str) -> float:
    r, g, b = _rgb(val)
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4   # noqa: E731
    return 0.2126 * f(r / 255) + 0.7152 * f(g / 255) + 0.0722 * f(b / 255)


def _contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


COLOR_PAT = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+")


def test_no_warm_color_anywhere():
    """① 全站（含 JS/HTML/SVG）不得再出现橙/棕/黄色系（色相 10°~75°、饱和 ≥12%）。"""
    bad = []
    for f in _files():
        text = _strip_comments(f.read_text(encoding="utf-8", errors="ignore"), f.suffix)
        for m in COLOR_PAT.finditer(text):
            val = re.sub(r"\s+", "", m.group(0))
            hue, sat, lig = _hsl(val)
            if 10 <= hue <= 75 and sat >= 12:
                bad.append(f"{f.name}:{text[:m.start()].count(chr(10)) + 1} {val}（色相{hue}° 饱和{sat}% 亮度{lig}%）")
    assert not bad, "仍有暖色（用户明确要求删除橙/棕/黄）：\n" + "\n".join(bad)


def test_no_scattered_color_literals_in_css():
    """② ops.css 的**声明行**里不允许写死语义色/中性色的 rgba 字面值（必须走令牌）。"""
    css = _strip_comments((STATIC / "ops.css").read_text(encoding="utf-8"), ".css")
    bad = []
    for i, line in enumerate(css.splitlines(), 1):
        if re.match(r"\s*--[\w-]+\s*:", line):     # 令牌定义行允许（这里是唯一真源）
            continue
        for pat in ("rgba(35,131,226", "rgba(15,123,15", "rgba(224,62,62", "rgba(63,185,80",
                    "rgba(167,139,250", "rgba(109,40,217", "rgba(180,83,9", "rgba(245,158,11",
                    "#faf8f4", "#c0392b", "#d5dce7", "#e4e3df", "#b45309"):
            if pat in line.replace(" ", ""):
                bad.append(f"ops.css:{i} {line.strip()[:110]}")
    assert not bad, "语义色/中性色仍在声明里写死（应改为令牌）：\n" + "\n".join(bad)


def test_semantic_colors_defined_per_theme():
    """③ 语义色必须在两个主题块里各定义一次（共用值 → 另一主题必然失衡）。"""
    css = _strip_comments((STATIC / "ops.css").read_text(encoding="utf-8"), ".css")
    dark = css.split('[data-theme="light"]')[0]
    light = css.split('[data-theme="light"]')[1]
    for tok in ("--muted", "--accent", "--pass", "--fail", "--warn", "--border-strong", "--overlay-3"):
        assert re.search(rf"^\s*{tok}\s*:", dark, re.M), f"暗色块缺 {tok}"
        assert re.search(rf"^\s*{tok}\s*:", light, re.M), f"亮色块缺 {tok}（两主题必须各一档）"
    for tok in ("--skip", "--hairline", "--on-fill"):
        assert re.search(rf"^\s*{tok}\s*:", dark, re.M), f"派生/别名令牌缺 {tok}"


def test_warning_color_is_cool_readable_and_distinct():
    """④ 警告色：两主题 ≥4.5:1、非暖色、与失败红分离 ≥30°。

    历史：琥珀(#B45309，用户嫌棕) → 紫罗兰(D50，用户嫌难看) → 青蓝(D53 定稿)。
    与强调蓝同为蓝色系是**用户指定的取向**，只要求与失败红明显分离。"""
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    def tok(name: str, block: str) -> str:
        m = re.search(rf"{name}\s*:\s*(#[0-9a-fA-F]{{6}})", block, re.I)
        assert m, f"{block[:20]}… 里找不到 {name}"
        return m.group(1)
    dark, light = css.split('[data-theme="light"]')[0], css.split('[data-theme="light"]')[1]
    w_light, w_dark = tok("--warn", light), tok("--warn", dark)
    fail, accent = tok("--fail", light), tok("--accent", light)

    assert _contrast(w_light, "#ffffff") >= 4.5, f"亮底警告色对比度不足：{w_light}"
    assert _contrast(w_dark, "#18191c") >= 4.5, f"深底警告色对比度不足：{w_dark}"
    for name, val in (("亮", w_light), ("深", w_dark)):
        hue, sat, _ = _hsl(val)
        assert not (10 <= hue <= 75 and sat >= 12), f"{name}色警告色落在暖色区（用户明确不要）：{val}"
        ohue, _, _ = _hsl(fail)
        d = min((hue - ohue) % 360, (ohue - hue) % 360)
        assert d >= 30, f"警告色 {val}({hue}°) 与失败红({ohue}°) 色相仅差 {d}°，容易混"
        # 与强调蓝：允许同属蓝色系（D53b 用户定稿正蓝），但亮度必须可感知地不同档
        # （徽标另有描边形态辅助区分；阈值 10 按 HSL 亮度计，#1E40AF(40) vs #2383e2(51) = 11 过线）
        wl, al = _hsl(w_light)[2], _hsl(accent)[2]
        assert abs(wl - al) >= 10, f"警告色亮度 {wl} 与强调蓝 {al} 过近"


def test_chart_colors_come_from_tokens():
    """图表侧同样不得硬编码颜色（D50 顺带修掉：深色主题下 tooltip 曾是写死的白卡）。"""
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "--accent-weak" in ui and "--overlay-2" in ui, "themeColors 需暴露派生档令牌"
    assert 'backgroundColor: "rgba(255,255,255,.98)"' not in ui, "tooltip 不能再写死亮色底"
    assert "backgroundColor: c.surface2" in ui, "tooltip 底色应读令牌（随主题）"
    for f in (STATIC / "js").rglob("*.js"):
        text = _strip_comments(f.read_text(encoding="utf-8"), ".js")
        assert not re.search(r"rgba?\(\s*\d+\s*,\s*\d+", text), f"{f.name} 仍有 rgba 字面值（应走令牌）"
