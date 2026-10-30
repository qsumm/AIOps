#!/usr/bin/env python3
"""从《用户手册.md》生成**排版好的** Word 与 PDF（单一来源，防漂移）。

两轮教训：
  ① 手工导出的 PDF/Word 会滞后——2026-10-16 阶段检查发现客户下载的 PDF 里没有新章节；
  ② 直接 HTML→PDF 转出来太"素"（没有封面、没有页码、表格没样式、截图没图注），不像一份能发给客户的说明书。
现在这条路是：**用 python-docx 生成真正的 Word 文档**（封面 / 自动目录（带页码）/ 页眉页脚 /
标题三级样式 / 表格样式 / 代码块 / 引用块 / 截图 + 图注），再用 LibreOffice 把这个 Word
排版渲染成 PDF——即"Word 排版、导出 PDF"。

依赖：python-docx、pypdf（内容自检用，可选）、soffice（LibreOffice）。
    uv pip install --python .venv/bin/python python-docx pypdf

用法：
    python3 tools/gen_manual_docs.py            # 生成 docs/04-交付与运营/用户手册.{docx,pdf}
    python3 tools/gen_manual_docs.py --check     # 只检查交付件是否比正文旧（部署前门禁）
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "04-交付与运营"
MD = DOCS / "用户手册.md"
DOCX = DOCS / "用户手册.docx"
PDF = DOCS / "用户手册.pdf"

ACCENT = "2383E2"          # 品牌主色（与前端设计令牌一致）
# 封面（沿用此前交付版的设计：深藏蓝底 + 青色点缀）。取色来自旧版 PDF 封面截图：
COVER_BG = "172235"
COVER_CYAN = "38DAF0"
COVER_MUTED = "8F979E"
ACCENT_DARK = "1E6FCC"
INK = "1D1D1F"
MUTED = "6E6E73"
BORDER = "D9D9DE"
CODE_BG = "F5F5F7"
QUOTE_BG = "F2F6FC"
ROW_ALT = "F7F7FA"
FONT_CJK = "微软雅黑"       # Windows Word 原生；macOS/LibreOffice 自动替换为等价中文字体
FONT_LATIN = "Arial"
FONT_MONO = "Consolas"


# ─────────────────────────── markdown 解析（只覆盖手册用到的构造） ───────────────────────────
def _blocks(text: str):
    lines = text.split("\n")
    i, out = 0, []
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            buf, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(("code", buf))
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            out.append(("h", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,})$", s):
            out.append(("hr",))
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            head = _cells(s)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            out.append(("table", head, rows))
            continue
        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(("quote", " ".join(buf)))
            continue
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", s)
        if m:
            out.append(("image", m.group(1), m.group(2)))
            i += 1
            continue
        if re.match(r"^([-*+]|\d+\.)\s+", s):
            items = []
            while i < len(lines) and re.match(r"^([-*+]|\d+\.)\s+", lines[i].strip()):
                items.append(re.sub(r"^([-*+]|\d+\.)\s+", "", lines[i].strip()))
                i += 1
            out.append(("list", items))
            continue
        out.append(("para", s))
        i += 1
    return out


def _cells(row: str):
    return [c.strip() for c in row.strip().strip("|").split("|")]


TOKEN = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def _rgb(hexstr):
    from docx.shared import RGBColor
    return RGBColor.from_string(hexstr)


def _eastasia(run, font: str):
    """中文与西文分别设字体（Word 对 ascii 与 eastAsia 分开处理）。"""
    from docx.oxml.ns import qn
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rf)
    rf.set(qn("w:eastAsia"), font)
    latin = FONT_MONO if font == FONT_MONO else FONT_LATIN
    rf.set(qn("w:ascii"), latin)
    rf.set(qn("w:hAnsi"), latin)


def _runs(par, text: str, *, size=None, base_bold=False):
    from docx.shared import Pt
    for part in TOKEN.split(text or ""):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            r = par.add_run(part[2:-2])
            r.bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            r = par.add_run(part[1:-1])
            r.font.name = FONT_MONO
            _eastasia(r, FONT_MONO)
            r.font.size = Pt((size.pt if hasattr(size, "pt") else 10.5) - 1)
            _shade_run(r, CODE_BG)
        elif part.startswith("[") and "](" in part:
            mm = re.match(r"\[([^\]]+)\]\(([^)]+)\)", part)
            r = par.add_run(mm.group(1) if mm else part)
            r.font.color.rgb = _rgb(ACCENT)
        else:
            r = par.add_run(part)
        if base_bold:
            r.bold = True
        if size is not None:
            r.font.size = size
        _eastasia(r, FONT_CJK)


def _shade_run(run, hexfill):
    from docx.oxml.ns import qn
    rpr = run._element.get_or_add_rPr()
    shd = rpr.makeelement(qn("w:shd"), {})
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hexfill)
    rpr.append(shd)


def _shade_par(par, hexfill):
    from docx.oxml.ns import qn
    ppr = par._p.get_or_add_pPr()
    shd = ppr.makeelement(qn("w:shd"), {})
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hexfill)
    ppr.append(shd)


def _shade_cell(cell, hexfill):
    from docx.oxml.ns import qn
    tcpr = cell._tc.get_or_add_tcPr()
    shd = tcpr.makeelement(qn("w:shd"), {})
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hexfill)
    tcpr.append(shd)


def _border(par, side: str, color: str, size: int = 12):
    from docx.oxml.ns import qn
    ppr = par._p.get_or_add_pPr()
    bd = ppr.find(qn("w:pBdr"))
    if bd is None:
        bd = ppr.makeelement(qn("w:pBdr"), {})
        ppr.append(bd)
    el = bd.makeelement(qn(f"w:{side}"), {})
    for k, v in (("w:val", "single"), ("w:sz", str(size)), ("w:space", "6"), ("w:color", color)):
        el.set(qn(k), v)
    bd.append(el)


def _field(par, instr: str, placeholder: str = "", size=None, color: str | None = None):
    """插入 Word 域（PAGE / NUMPAGES / TOC）——页脚页码与目录靠它。"""
    from docx.oxml.ns import qn
    r1 = par.add_run()
    fc = r1._element.makeelement(qn("w:fldChar"), {})
    fc.set(qn("w:fldCharType"), "begin")
    r1._element.append(fc)
    r2 = par.add_run()
    it = r2._element.makeelement(qn("w:instrText"), {qn("xml:space"): "preserve"})
    it.text = instr
    r2._element.append(it)
    r3 = par.add_run()
    sep = r3._element.makeelement(qn("w:fldChar"), {})
    sep.set(qn("w:fldCharType"), "separate")
    r3._element.append(sep)
    if placeholder:
        par.add_run(placeholder)
    r4 = par.add_run()
    end = r4._element.makeelement(qn("w:fldChar"), {})
    end.set(qn("w:fldCharType"), "end")
    r4._element.append(end)
    for r in par.runs:
        if size is not None:
            r.font.size = size
        if color:
            r.font.color.rgb = _rgb(color)
        _eastasia(r, FONT_CJK)


def _update_fields_on_open(doc):
    """让 Word/LibreOffice 打开时更新域——否则导出的 PDF 里目录没有页码（实测过）。"""
    from docx.oxml.ns import qn
    st = doc.settings.element
    if st.find(qn("w:updateFields")) is None:
        el = st.makeelement(qn("w:updateFields"), {})
        el.set(qn("w:val"), "true")
        st.append(el)


# ─────────────────────────── 版式 ───────────────────────────
def _setup(doc):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2.0)
    sec.left_margin, sec.right_margin = Cm(2.2), Cm(2.2)

    st = doc.styles["Normal"]
    st.font.name = FONT_LATIN
    st.font.size = Pt(10.5)
    st.font.color.rgb = _rgb(INK)
    st.paragraph_format.space_after = Pt(4)
    st.paragraph_format.line_spacing = 1.35
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CJK)

    for name, size, color, before, after in (("Heading 1", 17, ACCENT_DARK, 16, 8),
                                             ("Heading 2", 13.5, INK, 12, 6),
                                             ("Heading 3", 11.5, INK, 10, 4),
                                             ("Heading 4", 11, MUTED, 8, 4)):
        s = doc.styles[name]
        s.font.name = FONT_LATIN
        s.font.size = Pt(size)
        s.font.bold = True
        s.font.color.rgb = _rgb(color)
        s.paragraph_format.space_before = Pt(before)
        s.paragraph_format.space_after = Pt(after)
        s.paragraph_format.keep_with_next = True
        s.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CJK)

def _body_header_footer(sec):
    """正文节的页眉（文档名 + 细线）与页脚（第 X 页 / 共 Y 页）。

    刻意只设在**正文节**：封面是整页深色，印页眉会破坏设计。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt
    hdr = sec.header.paragraphs[0]
    hdr.text = ""
    hdr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r = hdr.add_run("TestOPS · 用户手册")
    r.font.size = Pt(8.5)
    r.font.color.rgb = _rgb(MUTED)
    _eastasia(r, FONT_CJK)
    _border(hdr, "bottom", BORDER, 6)

    ftr = sec.footer.paragraphs[0]
    ftr.text = ""
    ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    ftr.add_run("第 ")
    _field(ftr, "PAGE", "1", size=Pt(8.5), color=MUTED)
    ftr.add_run(" 页 / 共 ")
    _field(ftr, "NUMPAGES", "1", size=Pt(8.5), color=MUTED)
    ftr.add_run(" 页")
    for r in ftr.runs:
        r.font.size = Pt(8.5)
        r.font.color.rgb = _rgb(MUTED)
        _eastasia(r, FONT_CJK)


def _spacing(run, twips: int):
    """字符间距（Word 的"加宽"）：小标题那种拉开的效果靠它。"""
    from docx.oxml.ns import qn
    rpr = run._element.get_or_add_rPr()
    el = rpr.makeelement(qn("w:spacing"), {})
    el.set(qn("w:val"), str(twips))
    rpr.append(el)


def _cover(doc, meta: dict):
    """封面：深色整页 + 青色点缀（沿用此前交付版的设计，客户已经见过这一版）。

    Word 里做"整页深色"最稳的办法是**单格表格铺满页面**（页面背景色 LibreOffice 导出会丢），
    所以这里：封面独立一节（页边距 0）→ 一个 1×1 深色表格铺满 → 单元格里排文字。
    """
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    from docx.shared import Emu
    sec = doc.sections[0]
    sec.top_margin = sec.bottom_margin = sec.left_margin = sec.right_margin = Cm(0)
    # 封面上**不要页眉页脚**：显式建空的（否则某些渲染器会把正文节的页脚"继承"到封面），
    # 页脚距离也归零——否则页脚占位会把 29.7cm 的深色表格挤出一页。
    sec.header.is_linked_to_previous = False
    sec.footer.is_linked_to_previous = False
    sec.footer_distance = Emu(0)
    sec.header_distance = Emu(0)

    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = t.rows[0].cells[0]
    _shade_cell(cell, COVER_BG)
    t.rows[0].height = Cm(29.0)
    cell.width = Cm(21.0)

    used_first = {"v": False}

    def para(space_before=0, space_after=0):
        """取一个段落：**只有第一次**复用单元格自带的首段，之后一律新建。

        第一版没记这个状态，于是 9 个"留白空段落"全被塞进同一个段落里 → 净效果只有一段，
        整块内容顶在纸的最上沿（与旧版设计差得远）。
        """
        if not used_first["v"] and not cell.paragraphs[0].text and not cell.paragraphs[0].runs:
            p = cell.paragraphs[0]
            used_first["v"] = True
        else:
            p = cell.add_paragraph()
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after = Pt(space_after)
        return p

    # 上留白：**必须用空段落**——Word/LO 会忽略表格单元格里第一段的 space_before
    # （第一版就是这么顶到页面最上沿的，标题贴着纸边）
    for _ in range(13):
        para(space_after=0)
    # 小标题（青色 + 字间距拉宽）
    p = para()
    r = p.add_run("T E S T O P S   ·   O P S   M O N I T O R I N G   ·   U S E R   M A N U A L")
    r.font.size = Pt(8.5)
    r.font.color.rgb = _rgb(COVER_CYAN)
    r.bold = True
    _spacing(r, 40)
    _eastasia(r, FONT_CJK)
    # 青色横线
    rule = para(space_before=6, space_after=0)
    _border(rule, "bottom", COVER_CYAN, 12)
    # 主标题
    p = para(space_before=34)
    r = p.add_run("TestOPS")
    r.font.size = Pt(34)
    r.bold = True
    r.font.color.rgb = _rgb("FFFFFF")
    _eastasia(r, FONT_CJK)
    # 副标题
    p = para(space_before=8)
    r = p.add_run(f"TestOPS 用户手册 · {meta.get('version', '')}")
    r.font.size = Pt(12)
    r.font.color.rgb = _rgb(COVER_MUTED)
    _eastasia(r, FONT_CJK)
    # 信息块（左侧青色竖条 + 三行灰字）
    for i, (k, v) in enumerate([("版本", meta.get("version", "")),
                                ("适用读者", "运维与研发同学"),
                                ("随附交付", "使用 / 试用 / 售后答疑")]):
        p = para(space_before=46 if i == 0 else 9)
        p.paragraph_format.left_indent = Cm(1.6)
        r = p.add_run(f"{k}　{v}")
        r.font.size = Pt(10.5)
        r.font.color.rgb = _rgb("C9D2DC")
        _eastasia(r, FONT_CJK)
        _border(p, "left", COVER_CYAN, 18)     # 左侧青色竖条
    # 封面后另起一节：正文用正常页边距与自己的页眉页脚（封面不印页眉，否则破设计）
    from docx.enum.section import WD_SECTION
    ns = doc.add_section(WD_SECTION.NEW_PAGE)
    ns.page_width, ns.page_height = Cm(21.0), Cm(29.7)
    ns.top_margin, ns.bottom_margin = Cm(2.2), Cm(2.0)
    ns.left_margin, ns.right_margin = Cm(2.2), Cm(2.2)
    ns.header.is_linked_to_previous = False
    ns.footer.is_linked_to_previous = False
    _body_header_footer(ns)


def _toc(doc, entries):
    """目录：**静态生成、自带页码**（右对齐 + 点线，Word 目录的观感）。

    为什么不用 TOC 域（实测踩到）：LibreOffice 不理会 `w:updateFields`，导出的 PDF 里
    目录页是**空的**（只有一句占位提示）——客户打开就是一片空白。改成自己算页码：
    先把正文渲染一遍导出 PDF，用 pypdf 逐页抽文本定位每个标题所在页，再带着页码重渲染一次。
    """
    from docx.enum.text import WD_TAB_ALIGNMENT, WD_TAB_LEADER
    from docx.shared import Cm, Pt
    h = doc.add_paragraph()
    r = h.add_run("目录")
    r.bold = True
    r.font.size = Pt(16)
    r.font.color.rgb = _rgb(ACCENT_DARK)
    _eastasia(r, FONT_CJK)
    for level, title, page in entries:
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(3)
        pf.line_spacing = 1.2
        if level >= 2:
            pf.left_indent = Cm(0.7)
        pf.tab_stops.add_tab_stop(Cm(16.6), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
        r1 = p.add_run(title)
        r1.bold = (level == 1)
        r1.font.size = Pt(11 if level == 1 else 10)
        if level == 1:
            r1.font.color.rgb = _rgb(INK)
        _eastasia(r1, FONT_CJK)
        r2 = p.add_run("\t" + str(page))
        r2.font.size = Pt(10)
        r2.font.color.rgb = _rgb(MUTED)
        _eastasia(r2, FONT_CJK)
    doc.add_page_break()


def _norm(t: str) -> str:
    """归一化用于比对：去掉空白与 markdown 标记（PDF 抽取出来的文字会带空格噪声）。"""
    return re.sub(r"[*`\s\u3000]", "", t or "")


def _headings(body: str):
    """从正文里取出目录要列的标题（## 与 ###）。"""
    out = []
    for blk in _blocks(body):
        if blk[0] == "h" and blk[1] in (1, 2):
            t = re.sub(r"[*`]", "", blk[2]).strip()
            if t == "目录":        # 正文里那份手写目录不进目录（会自我引用）
                continue
            out.append((blk[1], t))
    return out


def _drop_manual_toc(body: str) -> str:
    """去掉正文里手写的「## 目录」清单——PDF 已经生成正式目录，留着是重复。"""
    return re.sub(r"^## 目录\s*$[\s\S]*?(?=^## )", "", body, flags=re.M)


def _page_map(pdf_path, heads, offset_hint: int = 0):
    """在 PDF 里定位每个标题所在页码（1 起）。定位不到的标题页码留 '—'。"""
    from pypdf import PdfReader
    rd = PdfReader(str(pdf_path))
    pages = [_norm(pg.extract_text() or "") for pg in rd.pages]
    out, misses = [], []
    search_from = 0
    for level, title in heads:
        key = _norm(re.sub(r"^#+\s*", "", title))
        key = key.lstrip("0123456789.")
        found = 0
        for i in range(search_from, len(pages)):
            if key and key[:14] in pages[i]:
                found = i + 1
                search_from = i          # 目录顺序 = 正文顺序，不必每次从头找
                break
        if not found:
            misses.append(title)
        out.append((level, title, found or "—"))
    return out, misses


def _render(doc, body: str):
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    img_n = 0
    for blk in _blocks(body):
        kind = blk[0]
        if kind == "h":
            level, text = min(blk[1], 4), blk[2]
            p = doc.add_paragraph(style=f"Heading {level}")
            _runs(p, text, size=Pt({1: 17, 2: 13.5, 3: 11.5, 4: 11}[level]), base_bold=True)
            if level == 1:
                _border(p, "bottom", BORDER, 8)
        elif kind == "para":
            _runs(doc.add_paragraph(), blk[1], size=Pt(10.5))
        elif kind == "list":
            for it in blk[1]:
                p = doc.add_paragraph(style="List Bullet")
                p.paragraph_format.space_after = Pt(2)
                _runs(p, it, size=Pt(10.5))
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.4)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(6)
            _runs(p, blk[1], size=Pt(10))
            _shade_par(p, QUOTE_BG)
            _border(p, "left", ACCENT, 18)
        elif kind == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.3)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(6)
            p.paragraph_format.line_spacing = 1.15
            _shade_par(p, CODE_BG)
            _border(p, "left", BORDER, 12)
            r = p.add_run("\n".join(blk[1]))
            r.font.name = FONT_MONO
            _eastasia(r, FONT_MONO)
            r.font.size = Pt(8.8)
        elif kind == "table":
            head, rows = blk[1], blk[2]
            t = doc.add_table(rows=1, cols=len(head))
            t.style = "Table Grid"
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            # 列宽策略：多列表格若平均分，短列（日期/版本/类型/编号）会占掉一半宽度、
            # 内容列被挤成一字一行。规则：首列给 2.0cm，第 2~3 列 1.9cm，其余均分剩余。
            t.autofit = False
            n = len(head)
            total = 16.6
            if n >= 4:
                widths = [total - 1.9 * (n - 1)] + [1.9] * (n - 1)
                # 更常见的形态：前几列是短标签 —— 前 3 列压窄，内容列拿大头
                widths = [2.3, 1.7, 1.6] + [(total - 5.6) / max(1, n - 3)] * (n - 3) if n >= 5 else \
                         [2.0] + [(total - 2.0) / (n - 1)] * (n - 1)
            else:
                widths = [total / n] * n
            for j, wcm in enumerate(widths):
                for row in t.rows:
                    row.cells[j].width = Cm(wcm)
            for j, txt in enumerate(head):
                cell = t.rows[0].cells[j]
                par = cell.paragraphs[0]
                par.paragraph_format.space_after = Pt(2)
                rr = par.add_run(txt.replace("**", ""))
                rr.bold = True
                rr.font.size = Pt(9.5)
                rr.font.color.rgb = _rgb("FFFFFF")
                _eastasia(rr, FONT_CJK)
                _shade_cell(cell, ACCENT_DARK)
            for k, row in enumerate(rows):
                cells = t.add_row().cells
                for j, txt in enumerate(row[:len(head)]):
                    par = cells[j].paragraphs[0]
                    par.paragraph_format.space_after = Pt(2)
                    _runs(par, txt, size=Pt(9.5))
                    if k % 2 == 1:
                        _shade_cell(cells[j], ROW_ALT)
            doc.add_paragraph().paragraph_format.space_after = Pt(2)
        elif kind == "image":
            alt, src = blk[1], blk[2]
            f = (DOCS / src).resolve()
            if not f.is_file():
                print(f"（图片缺失，跳过：{src}）", file=sys.stderr)
                continue
            img_n += 1
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(2)
            p.add_run().add_picture(str(f), width=Cm(15.2))
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap.paragraph_format.space_after = Pt(10)
            rc = cap.add_run(f"图 {img_n}　{alt}")
            rc.font.size = Pt(9)
            rc.font.color.rgb = _rgb(MUTED)
            rc.italic = True
            _eastasia(rc, FONT_CJK)
        elif kind == "hr":
            _border(doc.add_paragraph(), "bottom", BORDER, 6)
    return img_n


def _find_soffice() -> str | None:
    for cand in ("soffice", str(pathlib.Path.home() / ".local/bin/soffice"),
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        p = shutil.which(cand) or (cand if pathlib.Path(cand).exists() else None)
        if p:
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验交付件是否比正文新")
    args = ap.parse_args()
    if not MD.exists():
        print(f"找不到正文：{MD}", file=sys.stderr)
        return 2
    if args.check:
        stale = [p.name for p in (PDF, DOCX) if not (p.exists() and p.stat().st_mtime >= MD.stat().st_mtime)]
        if stale:
            print("✗ 以下交付件比正文旧：" + "、".join(stale) + "\n  修法：python3 tools/gen_manual_docs.py")
            return 1
        print("✓ PDF / DOCX 均不早于正文")
        return 0

    try:
        from docx import Document
    except Exception:
        print("缺 python-docx：uv pip install --python .venv/bin/python python-docx pypdf", file=sys.stderr)
        return 2
    soffice = _find_soffice()

    # 版本号**唯一来源**：仓库根 VERSION（手册不再自造版本号，见 docs/01-需求与设计/00-版本号规范.md）
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "dev"
    text = MD.read_text(encoding="utf-8").replace("{{VERSION}}", "v" + ver)
    meta = {"version": "v" + ver}
    body = text.split("\n", 1)[1] if text.startswith("#") else text
    # 只去掉**正文最开头**那张元信息表（封面已经承担了它）。
    # 血泪教训：第一版写成 `re.sub(r"^\|.*\|$", "", body, flags=re.M)`——那是"删掉每一行表格"，
    # 全文 123 行表格（判定口径 / 检查项 / 五类模板 / 影响面…）被**静默清空**，
    # 客户拿到的 PDF 里表格整片消失、只剩表后的文字。必须锚定在开头。
    body = re.sub(r"\A(?:\s*\|[^\n]*\|[ \t]*\n)+", "", body)
    body = _drop_manual_toc(body)                            # 去掉手写「## 目录」（已有正式目录）
    # 内置演示视频：PDF/Word 不内嵌视频，把 <video>…</video> 整块换成一句说明（内置文档中心可播）
    body = re.sub(r"<video[^>]*>.*?</video>", "[此处为内置演示视频，可于平台内置「文档中心」播放。]", body, flags=re.S)

    heads = _headings(body)

    def build(entries, out_docx: pathlib.Path):
        d = Document()
        _setup(d)
        _cover(d, meta)
        _toc(d, entries)
        n = _render(d, body)
        _update_fields_on_open(d)
        d.save(str(out_docx))
        return n

    def to_pdf(src_docx: pathlib.Path, out_pdf: pathlib.Path) -> bool:
        if out_pdf.exists():
            out_pdf.unlink()
        r = subprocess.run([soffice, "--headless", "--norestore", "--convert-to", "pdf:writer_pdf_Export",
                            "--outdir", str(out_pdf.parent), str(src_docx)],
                           capture_output=True, text=True, timeout=900)
        return out_pdf.exists()

    # 探页：**先只排"封面 + 正文"**（不含目录），这样标题不会在目录页上被自己匹配到
    # （第一版就是这么错的：所有页码都成了 2，因为每个标题都在目录页出现过）。
    # 再把"目录自身占几页"精确量出来加上去——目录内容是确定的，页数也就确定。
    tmp_docx, tmp_pdf = DOCS / "_manual_pass1.docx", DOCS / "_manual_pass1.pdf"
    toc_docx, toc_pdf = DOCS / "_manual_toc.docx", DOCS / "_manual_toc.pdf"

    def build_nolist(out_docx: pathlib.Path, with_toc: list):
        d = Document()
        _setup(d)
        _cover(d, meta)
        if with_toc:
            _toc(d, with_toc)
        n = _render(d, body)
        _update_fields_on_open(d)
        d.save(str(out_docx))
        return n

    n_img = build_nolist(tmp_docx, [])
    entries, misses = [], []
    toc_pages = 1
    if soffice and to_pdf(tmp_docx, tmp_pdf):
        entries, misses = _page_map(tmp_pdf, heads)
        # 量"目录自己占几页"：**只排封面 + 目录**，不能把正文也排进去
        # （第一版就是这里错的：整篇都排了，于是每条页码都被加上"全文页数"）
        d2 = Document()
        _setup(d2)
        _cover(d2, meta)
        _toc(d2, [(lv, t, "—") for lv, t in heads])
        d2.save(str(toc_docx))
        if to_pdf(toc_docx, toc_pdf):
            from pypdf import PdfReader
            toc_pages = max(1, len(PdfReader(str(toc_pdf)).pages) - 1)   # 减掉封面那页
        if entries:
            entries = [(lv, t, (p if p == "—" else p + toc_pages)) for lv, t, p in entries]
    if not entries:
        entries = [(lv, t, "—") for lv, t in heads]

    # 第二遍：带真实页码定稿（页数不变——只有数字不同，所以分页不受影响）
    n_img = build(entries, DOCX)
    print(f"✓ {DOCX.name}  {DOCX.stat().st_size // 1024} KB"
          f"（{n_img} 张截图 + 封面 + 目录 {len(entries)} 条 + 页眉页脚）")
    if misses:
        print(f"（目录里有 {len(misses)} 条没定位到页码，已留占位：{[m[:16] for m in misses[:3]]}）", file=sys.stderr)

    if not soffice:
        for f in (tmp_docx, tmp_pdf, toc_docx, toc_pdf):
            f.unlink(missing_ok=True)
        print("（找不到 soffice：只生成了 Word，未能生成 PDF）", file=sys.stderr)
        return 0
    if not to_pdf(DOCX, PDF):
        print("✗ PDF 生成失败（soffice 未能导出）", file=sys.stderr)
        return 3
    for f in (tmp_docx, tmp_pdf, toc_docx, toc_pdf):
        f.unlink(missing_ok=True)
    print(f"✓ {PDF.name}  {PDF.stat().st_size // 1024} KB")
    # 分发用副本（对外统一文件名，勿手改）：TestOPS用户操作使用手册V<版本>.pdf
    try:
        ver = meta.get("version", "") or ""
        ver_tag = (ver or "").lstrip("vV")
        dist = DOCS / f"TestOPS用户操作使用手册V{ver_tag}.pdf"
        if not dist.exists() or dist.stat().st_mtime < PDF.stat().st_mtime:
            shutil.copy2(PDF, dist)
            print(f"✓ 分发副本 {dist.name}  {dist.stat().st_size // 1024} KB")
    except Exception as e:  # 副本失败不影响主产物
        print(f"（分发副本生成失败：{e}）", file=sys.stderr)

    missing = []
    try:
        import zipfile
        with zipfile.ZipFile(DOCX) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
            media = [n for n in z.namelist() if n.startswith("word/media/")]
        plain = re.sub(r"<[^>]+>", "", xml)
        norm = lambda t: re.sub(r"[*`\s]", "", t)   # noqa: E731
        for key in ("服务监控与日志取证", "案例库", "环境自检", "界面上的"):
            if norm(key)[:6] not in norm(plain):
                missing.append(key)
        if not media:
            missing.append("（截图）")
        try:
            from pypdf import PdfReader
            rd = PdfReader(str(PDF))
            print(f"✓ PDF 共 {len(rd.pages)} 页")
        except Exception as e:  # noqa: BLE001
            print(f"（PDF 页数自检跳过：{type(e).__name__}）")
    except Exception as e:  # noqa: BLE001
        print(f"（内容自检跳过：{e}）")
    if missing:
        print("✗ 交付件缺内容：" + "、".join(missing), file=sys.stderr)
        return 1
    print("✓ 交付件内容自检通过（新章节 + 截图均在文件内）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
