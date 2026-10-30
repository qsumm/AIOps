"""FR-421 更新公告弹窗的机械守卫（D130）。

背景（用户原话）：「我更新了功能，用户是不感知的，只有看操作手册的更新记录，
但用户大多都不看」。所以平台必须有"版本感知的公告"：功能级更新自动弹窗告知，
优化级更新角标提示，全量历史常驻可查。

本守卫锁六件**会静默退化**的事：
  ① 数据结构完整（id 唯一 / level 合法 / 字段齐）——缺字段 = 弹窗渲染出空白块；
  ② 中英双语对称——缺一边的语言 = 切到 EN 时露出中文（与 D97 同一类缺陷）；
  ③ 声明版本不高于仓库 VERSION——不许吹比部署还新的号（诚实边界）；
  ④ 已读状态按**条目 id** 而非版本号——版本号会变，用版本号会导致老用户被判"已读"
     从而永远看不到这次更新（这正是本功能要解决的那类"更新不被感知"）；
  ⑤ 接线完整：CSS 存在并被引用、模块被 import、铃铛与版本信息页入口在；
  ⑥ 三种形态都在（分栏大卡 / 轻量卡片 / 更新中心），且都带 reduced-motion 豁免。

D131 追加第 ⑨ 锁：右栏**不得回退成"AI 生成插画"那套**（渐变/发光/玻璃卡/网格/假数据）——
用户对上一版右栏的原话是「目前太AI了」，那条纪律要靠测试锁住，不能只靠记性。

D132 试过把右栏换成"图像模型生成的套印海报"，被判「丑死了」→ **整体回退**，
资产与生成工具一并删除，视觉回到 D131 的手绘信号带（纯 CSS/SVG，无二进制资产）。

D133（右栏定为**逐格手绘像素画**）：用户反复反馈"AI 味太重 / 太丑"后，评审用视觉模型
对候选稿做「截图 → 诊断 → 修 → 复检」闭环，最终定稿为「左文右图」的像素插画：
运维工程师在双屏前值守，窗台一只黑猫。像素画由 wn_pixelscene.js **代码绘制**
（Canvas 逐格填充）——无二进制资产、无外链、可动画、扁平色块。
本守卫随设计更新：⑨ 改为锁「右栏 = 本地像素画资产（低分辨率 + 限色板 + 整数倍最近邻）」这条新纪律。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
CHANGELOG = STATIC / "js" / "changelog.js"
WHATSNEW = STATIC / "js" / "whatsnew.js"
PIXELSCENE = STATIC / "js" / "wn_pixelscene.js"
SCENE_PNG = STATIC / "img" / "whatsnew-scene.png"
CSS = STATIC / "ops-whatsnew.css"
INDEX = STATIC / "index.html"
I18N = STATIC / "js" / "i18n.js"
ABOUT = STATIC / "js" / "pages" / "about.js"
VERSION_FILE = ROOT / "VERSION"

LEVELS = {"major", "minor", "patch"}
TEXT_FIELDS = ("label", "title", "summary")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _releases() -> list[dict]:
    """从 changelog.js 里把 RELEASES 数组抠出来解析（不执行 JS，避免起服务）。"""
    src = _read(CHANGELOG)
    m = re.search(r"export const RELEASES = \[(.*?)\n\];", src, re.S)
    assert m, "changelog.js 里找不到 RELEASES 数组"
    body = m.group(1)
    # 逐条切分：顶层对象缩进 2 空格，嵌套对象缩进 ≥4 空格，故 "\n  {" 只命中顶层
    chunks = re.split(r"\n  \{", body)[1:]
    out = []
    for c in chunks:
        c = c.rstrip().rstrip(",")
        entry: dict = {}
        # 标量字段
        for key in ("id", "version", "date", "level"):
            mm = re.search(rf'\b{key}:\s*"([^"]*)"', c)
            if mm:
                entry[key] = mm.group(1)
        # 双语对象字段：label/title/summary（兼容单行 `{ zh: "", en: "" }` 与多行带尾逗号两种写法）
        for key in TEXT_FIELDS:
            mm = re.search(
                rf'\b{key}:\s*\{{\s*zh:\s*"((?:[^"\\]|\\.)*)"\s*,\s*en:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*\}}',
                c, re.S)
            if mm:
                entry[key] = {"zh": mm.group(1), "en": mm.group(2)}
        # items：lead/text 双语
        items = []
        for ib in re.findall(r"\{\s*lead:\s*\{(.*?)\}\s*,\s*text:\s*\{(.*?)\}\s*,?\s*\}", c, re.S):
            lead = re.search(r'zh:\s*"((?:[^"\\]|\\.)*)"\s*,\s*en:\s*"((?:[^"\\]|\\.)*)"', ib[0], re.S)
            text = re.search(r'zh:\s*"((?:[^"\\]|\\.)*)"\s*,\s*en:\s*"((?:[^"\\]|\\.)*)"', ib[1], re.S)
            if lead and text:
                items.append({"lead": {"zh": lead.group(1), "en": lead.group(2)},
                              "text": {"zh": text.group(1), "en": text.group(2)}})
        entry["items"] = items
        # link（可选）
        lm = re.search(r'\blink:\s*"(#\/[^"]*)"', c)
        if lm:
            entry["link"] = lm.group(1)
        out.append(entry)
    return out


# ── ① 数据结构 ──
def test_changelog_entries_well_formed():
    rels = _releases()
    assert rels, "RELEASES 为空——弹窗将没有任何内容可展示"
    ids = [r.get("id") for r in rels]
    assert all(ids), "每条更新必须有 id（已读状态靠它）"
    assert len(ids) == len(set(ids)), f"更新条目 id 重复：{ids}（重复 id 会让老用户被判已读）"
    for r in rels:
        rid = r.get("id")
        assert r.get("date"), f"{rid} 缺 date"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.get("date", "")), f"{rid} 的 date 格式应为 YYYY-MM-DD"
        assert r.get("level") in LEVELS, f"{rid} 的 level={r.get('level')!r} 不合法（只允许 {LEVELS}）"
        for f in TEXT_FIELDS:
            assert f in r, f"{rid} 缺字段 {f}"
        assert r.get("items"), f"{rid} 缺 items（弹窗主体是变更条目列表）"
        assert 1 <= len(r["items"]) <= 6, f"{rid} 的 items 数量 {len(r['items'])} 不合理（1~6 条）"


# ── ② 双语对称 ──
def test_changelog_zh_en_symmetric():
    """中英必须对称——缺一边 = 切到 EN 时露出中文（D97 同一类缺陷）。"""
    for r in _releases():
        rid = r.get("id")
        for f in TEXT_FIELDS:
            pair = r[f]
            for lang in ("zh", "en"):
                assert pair.get(lang, "").strip(), f"{rid}.{f} 缺 {lang} 文案"
        for i, it in enumerate(r.get("items", [])):
            for f in ("lead", "text"):
                for lang in ("zh", "en"):
                    assert it[f].get(lang, "").strip(), f"{rid}.items[{i}].{f} 缺 {lang} 文案"
        # 英文文案里不该混进中文（切到 EN 后露中文 = 缺陷）
        for f in TEXT_FIELDS:
            assert not re.search(r"[\u4e00-\u9fff]", r[f]["en"]), f"{rid}.{f} 的英文里混了中文"
        for i, it in enumerate(r.get("items", [])):
            for f in ("lead", "text"):
                assert not re.search(r"[\u4e00-\u9fff]", it[f]["en"]), f"{rid}.items[{i}].{f} 的英文里混了中文"


# ── ③ 声明版本不高于部署版本 ──
def test_declared_versions_not_ahead_of_version_file():
    """诚实边界：更新条目声明的版本号不得高于仓库 VERSION（不许吹比部署还新的号）。"""
    ver = _read(VERSION_FILE).strip()
    cur = tuple(int(x) for x in ver.split("."))
    for r in _releases():
        v = r.get("version")
        if not v:
            continue
        assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"{r.get('id')} 的 version 格式异常：{v}"
        assert tuple(int(x) for x in v.split(".")) <= cur, (
            f"{r.get('id')} 声明版本 {v} 高于当前 VERSION {ver}——平台还没发到这个号")


# ── ④ 已读状态用条目 id ──
def test_read_state_keyed_by_entry_id_not_version():
    """已读状态必须按**条目 id** 记，不能按版本号。

    原因：版本号会变（同一批功能可能并入不同版本号），用版本号当已读键会导致
    老用户被判"已读"从而永远看不到这次更新——正是本功能要消灭的那类问题。
    """
    src = _read(WHATSNEW)
    assert 'SEEN_KEY = "ops_seen_release"' in src
    assert "localStorage.setItem(SEEN_KEY, id)" in src, "已读状态写入的不是条目 id"
    assert "RELEASES.findIndex((r) => r.id === seen)" in src, "未读计算未按条目 id 定位"


# ── ⑤ 接线 ──
def test_assets_wired():
    assert CSS.exists(), "缺 static/ops-whatsnew.css"
    html = _read(INDEX)
    assert 'href="/ops-whatsnew.css"' in html, "index.html 未引用 ops-whatsnew.css"
    assert 'from "/js/whatsnew.js"' in html, "index.html 未 import whatsnew 模块"
    assert 'id="wn-bell"' in html, "顶栏缺更新公告入口（铃铛）"
    assert 'id="wn-badge"' in html, "顶栏缺未读角标元素"
    assert "initWhatsNew()" in html, "index.html 未触发公告"
    # 登录前不弹（登录页不该被公告打断）
    assert re.search(r"if \(getToken\(\)\) setTimeout\(\(\) => \{ initWhatsNew\(\)", html), \
        "公告触发未做登录态门禁（未登录时不应弹）"
    assert ABOUT.exists()
    about = _read(ABOUT)
    assert "openCenter" in about, "版本信息页缺「更新中心」入口（弹窗关掉后信息就丢了）"
    assert "RELEASES" in about, "版本信息页未展示更新历史"
    # i18n 词条中英双填
    i18n = _read(I18N)
    for k in ("whatsnew.view_detail", "whatsnew.later", "whatsnew.all_updates",
              "whatsnew.center", "whatsnew.items_count", "whatsnew.new_badge"):
        assert i18n.count(f'"{k}"') == 2, f"i18n.js 词条 {k} 未中英双填"


# ── ⑥ 三种形态 + 动效豁免 ──
def test_three_forms_present_and_motion_guarded():
    src = _read(WHATSNEW)
    assert "export function showMajorModal" in src, "缺分栏大卡（major）"
    assert "export function showMinorCard" in src, "缺轻量卡片（minor）"
    assert "export function openCenter" in src, "缺更新中心（历史常驻可查）"
    # 键盘可达：Esc 关闭 + 焦点圈定
    assert 'e.key === "Escape"' in src, "弹窗未支持 Esc 关闭"
    assert "trapFocus" in src, "弹窗未做焦点圈定（Tab 会跑出弹窗）"
    css = _read(CSS)
    assert "prefers-reduced-motion" in css, "弹窗动效缺 reduced-motion 豁免"
    assert "animation: none !important" in css
    # 分级打扰：patch 不打扰
    assert 'top.level === "major"' in src and 'top.level === "minor"' in src, "未按 level 分级打扰"
    assert "patch" in src, "未处理 patch 级（应完全不打扰）"


# ── ⑦ 视觉纪律：禁暖色、禁外部资源 ──
def test_visual_discipline_no_warm_color_no_external_asset():
    """D50 调色板纪律在本模块同样生效：禁暖色、禁外部图片/CDN。"""
    css = _read(CSS)
    for bad in ("#faf8f4", "#c0392b", "#b45309", "#f97316", "#fb923c", "#e11d48", "#dc2626"):
        assert bad.lower() not in css.lower(), f"弹窗样式含暖色/被禁色值 {bad}（应走强调蓝）"
    # 禁外部资源：不得出现 http(s) 引用或 url() 外链
    assert not re.search(r"url\(\s*['\"]?https?://", css), "弹窗样式引了外部资源（禁 CDN）"
    assert not re.search(r"https?://", css), "弹窗样式含外链"
    # D132：右栏改用**本地静态海报**（不是外链/CDN）
    # ——纪律从"禁图片"收紧为"禁外部资源"：图必须随镜像分发，不得引外链
    # 右侧视觉区必须纯 CSS/SVG 绘制（不得 <img src>）
    # ——D132 试过"生成一张套印海报"，被判「丑死了」已整体回退；视觉资产要么用
    #    产品自己的视觉语言手绘，要么不放，半吊子的生成插画只是换一种方式碰运气。
    assert "<img" not in src_of(WHATSNEW), "弹窗用了 <img>（应纯 SVG/CSS 绘制）"
    # 语义色只用既有令牌
    for tok in ("--accent", "--surface", "--border", "--text", "--muted"):
        assert f"var({tok})" in css, f"弹窗样式未使用平台既有令牌 {tok}"


def src_of(p: Path) -> str:
    return _read(p)


# ── ⑧ 语法门禁（D3 事故防线：本模块自身也必须是合法 ES Module）──
def test_whatsnew_modules_parse():
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node 不可用，跳过前端语法门禁")
    for f in (CHANGELOG, WHATSNEW):
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as tf:
            tf.write(f.read_text(encoding="utf-8"))
            tmp = tf.name
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name} 语法错误：{r.stderr.strip().splitlines()[-3:]}"


# ── ⑨ 右栏"去 AI 味"回归锁（D133 · 定稿：代码手绘像素画）──
def _css_block(css: str, selector: str) -> str:
    """取出某条选择器的规则块（花括号配平）——把守卫限定在视觉区，不误伤别的层。"""
    i = css.find(selector)
    if i < 0:
        return ""
    j = css.find("{", i)
    if j < 0:
        return ""
    depth, k = 0, j
    while k < len(css):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return css[j:k + 1]
        k += 1
    return ""


def test_visual_column_is_local_pixel_art_asset():
    """右栏必须是**本地像素画资产**（低分辨率 + 限色板 + 整数倍最近邻放大），且守住"去 AI 味"的硬约束。

    背景（四轮被否的教训，别再走回头路）：
      D130 玻璃拟态 + 高斯发光 + 细网格 + 假 KPI  → 用户判「太AI了」；
      D131 纯 CSS/SVG 信号带 → 干净但缺叙事；
      D132 图像模型生成的套印海报 → 用户判「丑死了」，整体回退；
      D133 代码手绘的像素画（纯色、稀疏）→ 用户判「有点丑」（缺像素工艺：网格纪律/抖动/明度多阶）；
      D134 像素化后处理的 16-bit 场景（160×90 硬网格 + 24 色调色板 + FS 抖动），资产入库为本地 PNG。

    本守卫把"右栏不许再变成那样"变成机械规则：
      · 只能引用**本地**资产（禁 http/https 外链、禁 base64 内嵌、禁 CDN）；
      · 资产必须是低分辨率像素网格（≤256px）且体积受限（≤64KB）；
      · 放大必须整数倍 + 最近邻（imageSmoothingEnabled=false），否则像素边缘发虚；
      · 右栏样式不得出现渐变 / 模糊 / 发光 / 投影（那几样正是"AI 味"来源）；
      · 画面是**场景**不是读数带 → 不得出现写死的百分比/数值（假数据不得复活）。
    """
    css = _read(CSS)
    js = _read(WHATSNEW)
    assert PIXELSCENE.exists(), "缺 static/js/wn_pixelscene.js（像素画模块）"
    scene = _read(PIXELSCENE)

    # 右栏必须挂像素画模块
    assert 'from "./wn_pixelscene.js"' in js, "whatsnew.js 未引入像素画模块"
    assert "mountPixelScene" in js, "whatsnew.js 未调用像素画"
    assert "scene.destroy()" in js, "弹窗关闭时未停掉像素画（场景销毁钩子缺失）"

    # 只能引用本地资产
    for bad, why in (
        ("http://", "外链资源"),
        ("https://", "外链资源"),
        ("data:image", "base64 内嵌图片"),
    ):
        assert bad not in scene, f"像素画模块出现 {bad}（{why}）——必须是本地入库资产"
    assert "imageSmoothingEnabled = false" in scene, "像素画放大必须最近邻（否则边缘发虚）"
    assert "imageRendering" in scene and "pixelated" in scene, "画布须设 image-rendering: pixelated"
    assert "getContext" in scene, "像素画模块没有用 Canvas 绘制"

    # 资产本体：本地、低分辨率、体积受限
    assert SCENE_PNG.exists(), f"缺像素画资产 {SCENE_PNG.relative_to(ROOT)}"
    size = SCENE_PNG.stat().st_size
    assert size <= 64 * 1024, f"像素画资产 {size} 字节过大（≤64KB）——像素画必须小"
    with Image.open(SCENE_PNG) as im:
        w, h = im.size
        assert w <= 256 and h <= 256, f"像素画资产 {w}×{h} 过大——像素画必须低分辨率（≤256px）"
        assert w * 2 >= 200, "像素画资产宽度过小，整数倍放大后不足以承载右栏"

    # 右栏样式不得有渐变 / 模糊 / 发光 / 投影
    art = _css_block(css, ".wn2-art {")
    assert art, "找不到 .wn2-art 规则块（右栏容器）"
    for bad, why in (
        ("gradient(", "渐变（AI 味的头号来源）"),
        ("backdrop-filter", "玻璃模糊"),
        ("blur(", "模糊"),
        ("box-shadow", "投影"),
        ("text-shadow", "文字发光"),
        ("filter:", "滤镜（发光）"),
    ):
        assert bad not in art, f"右栏视觉区出现 {bad}——{why}（D134 定稿为像素画 + 纯色面）"

    # 假数据不得复活：右栏不写死任何百分比/读数
    assert "%" not in art, "右栏出现写死的百分比（假数据）"
    for bad in ("cpu.percent", "mem.percent", "disk.percent"):
        assert bad not in art, f"右栏不应再出现读数带指标名 {bad}（右栏是场景，不是读数带）"

    # 旧版那套路子的类名不得复活
    for cls in ("wn-glass", "wn-spark", "wn-viz-wm", "wn-viz-dots", "wn-viz-cards", "wn-viz-wave",
                "wn-visual", "wn-viz-ver", "wn-viz-tags", "wn-trace"):
        assert cls not in css, f"旧版视觉区类名 {cls} 复活了"


def test_highlights_bilingual_symmetric():
    """弹窗「更新要点」（highlights）必须中英双填——缺一边 = 切 EN 时露出中文（D97 同类缺陷）。"""
    src = _read(CHANGELOG)
    chunks = re.findall(r"highlights:\s*\[(.*?)\n    \],", src, re.S)
    assert chunks, "changelog.js 里找不到 highlights 数组（弹窗更新要点会退化成空列表）"
    for chunk in chunks:
        # 每条 highlight 顶层以 { 开头、} 结束——按顶层缩进切块（别用 \{.*?\}，嵌套对象会被截断）
        # 每条 highlight 顶层以 { 开头、} 结束——按顶层缩进切块（别用 \{.*?\}，嵌套对象会被截断）
        items = re.split(r"\n      \},\n      \{", chunk)
        for i, item in enumerate(items):
            for f in ("title", "desc", "stat", "statLabel"):
                mm = re.search(rf'{f}:\s*\{{(.*?)\}}', item, re.S)
                assert mm, f"highlights[{i}] 缺 {f}"
                for lang in ("zh", "en"):
                    assert re.search(rf'{lang}:\s*"[^"]+"', mm.group(1)), \
                        f"highlights[{i}].{f} 缺 {lang} 文案"
                en = re.search(r'en:\s*"([^"]*)"', mm.group(1)).group(1)
                assert not re.search(r"[\u4e00-\u9fff]", en), f"highlights[{i}].{f} 的英文里混了中文"


def test_release_modal_wiring():
    """弹窗新形态接线完整：新类名存在、按钮走平台既有 .btn、共享零件没被删。"""
    js = _read(WHATSNEW)
    css = _read(CSS)
    for cls in ("wn2-modal", "wn2-mask", "wn2-main", "wn2-art", "wn2-item", "wn2-item-stat",
                "wn2-list-label", "wn2-pill", "wn2-title", "wn2-sub", "wn2-foot", "wn2-close"):
        assert f".{cls}" in css, f"缺 .{cls} 样式"
        assert cls in js, f"whatsnew.js 未用到 .{cls}"
    # 按钮复用平台既有 .btn（不另造一套按钮样式）
    assert 'class: "btn"' in js and 'class: "btn secondary"' in js, "弹窗按钮未复用平台 .btn"
    # 共用零件仍被 minor 卡 / 更新中心使用，不能删
    for cls in ("wn-badge", "wn-list", "wn-item-lead", "wn-item-text"):
        assert f".{cls}" in css, f"共用零件 .{cls} 被误删（minor 卡/更新中心要用）"
    # 新增 i18n 词条中英双填
    i18n = _read(I18N)
    for k in ("whatsnew.highlights", "whatsnew.start", "whatsnew.full_log", "whatsnew.scene_kicker"):
        assert i18n.count(f'"{k}"') == 2, f"i18n.js 词条 {k} 未中英双填"


def test_changelog_latest_matches_version():
    """D168：更新公告数据源必须与产品版本同步——发版漏更会红。

    现场：v1.4~v1.5.6 的一批发版全没登记进 changelog.js，用户在「更新中心」
    只能看到 v1.3.0（且首进弹窗弹的还是 10-19 的旧内容）。维护纪律里写了
    "发版时同时更新本文件顶部条目"，但没有守卫=没有纪律。
    """
    import os
    import re as _re

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    ver = open(os.path.join(root, "VERSION"), encoding="utf-8").read().strip()
    js = (STATIC / "js" / "changelog.js").read_text(encoding="utf-8")
    # RELEASES 第一条 = 最新条目，其 version 必须等于 VERSION
    m = _re.search(r"export const RELEASES = \[\s*\{\s*id:\s*\"[^\"]+\",\s*version:\s*\"([^\"]+)\"", js)
    assert m, "RELEASES 首条未找到"
    assert m.group(1) == ver, (
        f"更新公告最新条目 v{m.group(1)} 与 VERSION {ver} 不一致——发版漏更 changelog.js")
    # 日期也必须是近期的（防复制旧日期）：首条 date 不得早于 2026-10-20
    dm = _re.search(r"export const RELEASES = \[\s*\{\s*id:\s*\"[^\"]+\",\s*version:\s*\"[^\"]+\",\s*date:\s*\"([^\"]+)\"", js)
    assert dm and dm.group(1) >= "2026-10-20", f"最新条目日期过旧：{dm.group(1) if dm else '?'}"
