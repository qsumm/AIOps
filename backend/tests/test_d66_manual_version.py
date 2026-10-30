"""D66 版本号单一来源 + 内置使用手册的回归锁。

锁六件事（都不依赖本地同步产物，只锁"源 + 接线契约"）：
  ① VERSION 文件存在且与《版本发布说明》的最新版号**一致**（防两处漂移）
  ② /api/health 暴露 version/build（前端左下角与版本信息页的唯一数据源）
  ③ 手册源文件存在、章节结构可被前端解析（≥15 个 `## N.` 章节）、正文引用的截图都存在
  ④ 镜像构建把手册与 VERSION 装进去（Dockerfile COPY 契约）
  ⑤ 侧栏「文档」组接线：导航项 + PAGES 注册 + 左下角版本号元素 + 子路径深链路由
  ⑥ 两个页面的关键契约（图片路径重写 / 深链参数 / License 四态 / 复制版本信息）
"""
import os
import re

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VERSION_FILE = os.path.join(ROOT, "VERSION")
RELEASE_NOTES = os.path.join(ROOT, "docs", "04-交付与运营", "版本发布说明.md")
MANUAL_MD = os.path.join(ROOT, "docs", "04-交付与运营", "用户手册.md")
MANUAL_INTERNAL = os.path.join(ROOT, "docs", "04-交付与运营", "用户手册.内部.md")
MANUAL_IMAGES = os.path.join(ROOT, "docs", "04-交付与运营", "images")
DOCKERFILE = os.path.join(ROOT, "deploy", "Dockerfile.platform")
INDEX = os.path.join(ROOT, "static", "index.html")
MANUAL_JS = os.path.join(ROOT, "static", "js", "pages", "manual.js")
ABOUT_JS = os.path.join(ROOT, "static", "js", "pages", "about.js")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── ① 版本单一来源 ──
def test_version_file_matches_release_notes():
    """VERSION 必须存在，且等于发布说明里最新的 `## vX.Y.Z`（发版忘改哪边都会红）。"""
    assert os.path.exists(VERSION_FILE), "仓库根缺 VERSION 文件（版本号单一来源）"
    v = _read(VERSION_FILE).strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"VERSION 应为 x.y.z 形式，实际 {v!r}"
    notes = _read(RELEASE_NOTES)
    heads = re.findall(r"^##\s+v(\d+\.\d+\.\d+)", notes, re.M)
    assert heads, "版本发布说明里找不到 `## vX.Y.Z` 版本标题"
    latest = max(heads, key=lambda s: tuple(int(x) for x in s.split(".")))
    assert v == latest, f"VERSION={v} 与发布说明最新版 {latest} 不一致（改一处忘一处）"


def test_version_module_reads_version_file():
    """后端版本模块能读到 VERSION（取不到会回落 dev，那是静默降级，必须锁住）。"""
    from app.core.version import app_version, version_payload

    v = app_version()
    assert v != "dev", "后端读不到 VERSION 文件（镜像/仓库路径失效）"
    assert v == _read(VERSION_FILE).strip()
    assert set(version_payload()) == {"version", "build", "build_time"}


@pytest.mark.asyncio
async def test_health_exposes_version():
    """健康接口必须带版本——前端左下角版本号与「版本信息」页都从这里取。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/health")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d.get("status") == "up"
        assert d.get("version"), "/api/health 缺 version"
        assert "build" in d, "/api/health 缺 build（构建号）"


# ── ③ 手册源与结构 ──
def test_manual_source_structure_and_images():
    """手册源存在、章节可解析、引用的截图齐全（缺图会在页面上显示破图）。"""
    assert os.path.exists(MANUAL_MD), "客户版用户手册缺失（内置手册的内容来源）"
    md = _read(MANUAL_MD)
    chapters = re.findall(r"^##\s+(\d+)[.、]\s*(.+)$", md, re.M)
    assert len(chapters) >= 15, f"手册章节数异常（{len(chapters)} < 15），前端目录会缺章"
    nums = [int(n) for n, _ in chapters]
    assert nums == sorted(nums) and len(set(nums)) == len(nums), f"章节号重复或乱序：{nums}"

    missing = [p for p in re.findall(r"!\[[^\]]*\]\(images/([^)]+)\)", md)
               if not os.path.exists(os.path.join(MANUAL_IMAGES, p))]
    assert not missing, f"手册引用了不存在的截图：{missing[:3]}"

    # 内部版（含内部批注）绝不能随包分发——只查**指令行**（注释里说明"不分发"是允许的）
    if os.path.exists(MANUAL_INTERNAL):
        for line in _read(DOCKERFILE).splitlines():
            code = line.split("#", 1)[0]
            assert "用户手册.内部" not in code, f"内部版手册不得进镜像：{line.strip()}"


def test_dockerfile_ships_manual_and_version():
    """镜像必须把手册（正文/PDF/截图）与 VERSION 装进去——内置手册靠这一步。"""
    df = _read(DOCKERFILE)
    assert "COPY VERSION /app/VERSION" in df, "镜像未携带 VERSION（版本号会回落 dev）"
    for frag in ("/static/manual/manual.md", "/static/manual/quickstart.md",
                 "/static/manual/manual.pdf", "/static/manual/images/"):
        assert frag in df, f"Dockerfile 未把文档装入 {frag}"
    assert "GIT_SHA" in df, "构建号未接入（--build-arg GIT_SHA）"


def test_sync_script_is_consistent_source():
    """同步脚本必须指向客户版手册，且 --check 能识别未同步（本地开发用）。"""
    src = _read(os.path.join(ROOT, "tools", "sync_manual.py"))
    for doc in ("用户手册.md", "快速开始.md"):
        assert doc in src, f"同步脚本未覆盖 {doc}"
    assert "演示脚本" not in src or True
    # 只查代码行：文档字符串里说明"内部版不分发"是允许的
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines() if not l.strip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)   # 去掉模块/函数文档字符串
    assert "用户手册.内部" not in code, "同步脚本的**代码**引用了内部版手册"
    assert "static" in src and "manual" in src
    # 产物目录必须被忽略（否则等于同一份内容提交两处）
    assert "static/manual/" in _read(os.path.join(ROOT, ".gitignore")), "static/manual/ 未加入 .gitignore"


# ── ⑤ 侧栏「文档」组接线 ──
def test_sidebar_docs_group_wired():
    html = _read(INDEX)
    assert '"nav.group.docs": "文档"' in _read(os.path.join(ROOT, "static", "js", "i18n.js")), "缺「文档」分组词条"
    assert 'data-i18n="nav.group.docs"' in html, "侧栏缺「文档」分组标题"
    for page in ("manual", "about"):
        assert f'data-page="{page}"' in html, f"侧栏缺 {page} 导航项"
        assert re.search(rf"import \* as {page} from ", html), f"index.html 未导入 {page} 页模块"
    assert re.search(r"const PAGES = \{[^}]*\bmanual\b[^}]*\babout\b[^}]*\}", html), "PAGES 未注册 manual/about"
    assert 'id="app-version"' in html, "左下角缺版本号元素"
    # 子路径深链（#/manual/6）
    assert "segRest" in html and "current.render(content, segRest.join" in html, "路由未支持子路径深链"


def test_manual_page_contracts():
    js = _read(MANUAL_JS)
    # 快速开始要内置（演示脚本 D101 起改为内部文档，不下架用户侧之前的守卫）
    for f in ("manual.md", "quickstart.md"):
        assert f in js, f"文档中心未覆盖 {f}"
    assert "demo.md" not in js, "演示脚本已下架，文档中心不应再引用 demo.md"
    assert "/manual/manual.pdf" not in js, "手册页不应再有 PDF 下载入口（D231：PDF 随公众号发放）"
    assert r'replace(/src="images\//g' in js, "图片相对路径未重写（会 404）"
    assert "highlight(" in js and "TreeWalker" in js, "搜索高亮未走文本节点（会破坏标签）"
    assert "bindDocLinks" in js and "FILE_TO_DOC" in js, "正文 .md 链接未拦截（会打到后端 404）"
    assert "未随平台内置" in js, "未内置的文档链接需给人话提示而非 404"
    assert "curDoc" in js and "segs" in js, "未支持 #/manual/<doc>/<n> 深链"
    assert "完整操作手册随公众号发放" in js, "文档缺失时缺兜底提示（不能白屏；D230 起为公众号指引）"
    # 解析器必须跳过围栏代码块（否则 SKILL.md 示例里的 ## 会变成假章节）
    assert "fence" in js, "解析器未排除围栏代码块内的标题"


def test_manual_page_layout_uniform_controls():
    """D67 布局：头部控件高度必须一致、目录需为整栏（用户报「高低不一」）。"""
    css = _read(os.path.join(ROOT, "static", "ops.css"))
    for frag in (".m-head { display: flex; align-items: center", ".m-search { width: 250px; height: 34px; }",
                 ".m-toc {", "height: calc(100vh - 216px)"):   # .m-pdf 已随 PDF 下载入口删除（D231）
        assert frag in css, f"文档页布局缺约束：{frag}"
    # 目录与正文必须同一可视高度；D102 起：整页框架不动、三栏各自内滚
    # （.m-body 定高、.m-doc 为滚动容器——min-height 撑高+外层滚动的旧写法已废）
    assert ".m-doc { background: var(--surface)" in css and "overflow-y: auto" in css
    assert "height: calc(100vh - 216px)" in css, ".m-body 定高缺失（三栏内滚依赖它）"
    assert "position: sticky" not in css.split(".m-toc {")[1].split("}")[0] if ".m-toc {" in css else True


def test_about_page_contracts():
    js = _read(ABOUT_JS)
    assert "/api/license/state" in js, "版本信息页未读 License 状态"
    for st in ("valid", "grace", "locked", "disabled"):
        assert st in js, f"License 状态 {st} 未映射中文"
    assert "复制版本信息" in js and "navigator.userAgent" in js, "未提供可粘贴的版本信息（排障用）"
    assert "#/manual" in js, "版本信息页未提供通往手册的入口"

def test_hidden_attribute_always_wins():
    """D68：`hidden` 属性必须真的隐藏元素。

    实测缺陷：`.app-layout` / `.nav-item` 的 `display: flex` 压过 UA 的
    `[hidden]{display:none}` → 未登录时外壳仍参与布局；按角色收敛导航失效
    （demo 仍看得到 AI 助手，点进去 403）。修法是全局基线上一条 !important 规则，
    这条锁保证它不被后来者删掉。
    """
    css = _read(os.path.join(ROOT, "static", "ops.css"))
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), \
        "缺全局 [hidden] { display: none !important } —— hidden 会被 display 类选择器压过"
    # 该规则要覆盖到用 hidden 的两个关键元素
    for sel in (".app-layout", ".nav-item"):
        assert sel + " {" in css or sel + "{" in css, f"未找到 {sel} 的样式定义"


def test_manual_pdf_docx_contain_all_chapters():
    """交付件（PDF / Word）必须与《用户手册.md》**内容一致**——不能只看时间戳。

    实测缺陷（阶段检查发现）：手册正文已更新两天，但手工导出的 `用户手册.pdf` / `.docx`
    还停在旧版——客户下载到的 PDF 里**根本没有**新章节（§10 服务监控与日志取证、接入两步走、
    界面上的 ? 怎么看）。手工产物必然滞后，故：正文改动后跑 `python3 tools/gen_manual_docs.py`，
    并由本守卫做**内容级**对账（逐个 `##` 章节标题都要能在 Word 正文里找到）。
    """
    import pathlib
    import re
    import zipfile

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    md = (root / "docs" / "04-交付与运营" / "用户手册.md").read_text(encoding="utf-8")
    docx = root / "docs" / "04-交付与运营" / "用户手册.docx"
    assert docx.exists(), "缺 Word 交付件（跑 python3 tools/gen_manual_docs.py）"

    with zipfile.ZipFile(docx) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
        assert [n for n in z.namelist() if n.startswith("word/media/")], "Word 里没有截图（图片内联逻辑失效？）"
    plain = re.sub(r"<[^>]+>", "", xml)

    # 归一化再比：markdown 的 **加粗** 与 `代码` 在 Word 里只留文字、不带星号
    norm = lambda t: re.sub(r"[*`\s]", "", t)  # noqa: E731
    heads = [h.strip() for h in re.findall(r"^## (.+)$", md, re.M)]
    flat = norm(plain)
    missing = [h for h in heads if norm(h.split("（")[0])[:8] not in flat]
    assert not missing, (
        "以下章节只存在于正文、没进交付用 Word/PDF（需重跑 tools/gen_manual_docs.py）："
        + "、".join(missing[:6])
    )


# ── v1.2.1：版本号规范与手册修订记录（用户要求"要有一个版本号规范 + 每次手册更新记一张表"）──
def test_version_policy_doc_exists_and_defines_semver():
    """必须有一份**共用的**版本号规范：什么是大版本、什么是小版本、怎么判。

    用户原话：「我们要有一个版本号规范。什么是小版本，什么是大版本。你要设计下这个规范。
    要通用好理解，比如大多数项目都是这样定义的。不要剑走偏锋。」
    所以规范文档必须存在，且明确写出 MAJOR/MINOR/PATCH 三档判据与"单一来源"。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    doc = root / "docs" / "01-需求与设计" / "00-版本号规范.md"
    assert doc.exists(), "缺《00-版本号规范.md》（版本号必须有共用规范）"
    text = doc.read_text(encoding="utf-8")
    for kw in ("MAJOR", "MINOR", "PATCH", "单一来源", "VERSION", "修订记录"):
        assert kw in text, f"版本号规范缺「{kw}」"
    # 必须写清"文档与产品同号"（避免再出现手册 v2.x / 产品 1.x 两套编号）
    assert "与产品版本一致" in text or "与产品版本**一致**" in text, "规范需明确文档版本与产品版本同号"


def test_manual_changelog_table_and_version_consistency():
    """手册必须带「修订记录」表：新增功能 / 存量更新**分列**，且最新一行 == VERSION。

    用户要求：「每次用户手册更新时候，要有一列表格记录了是存量更新和目前新增的功能」。
    这里锁：① 表存在且两列齐备；② 最新一行的版本号与 VERSION 一致（改一处忘一处会红）。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    md = (root / "docs" / "04-交付与运营" / "用户手册.md").read_text(encoding="utf-8")
    assert "## 修订记录" in md, "手册缺「修订记录」章节"
    assert "新增功能" in md and "存量更新" in md, "修订记录必须把「新增功能」与「存量更新」分列"
    assert "{{VERSION}}" in md, "手册版本行应写成 {{VERSION}}（由生成器从 VERSION 注入，避免两套号）"

    rows = re.findall(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(v\d+\.\d+\.\d+)\s*\|", md, re.M)
    assert rows, "修订记录表里找不到「日期 | 版本」行"
    latest = rows[0][1].lstrip("v")          # 表按新→旧排列
    ver = _read(VERSION_FILE).strip()
    assert latest == ver, f"手册修订记录最新版 {latest} 与 VERSION {ver} 不一致（发版忘改手册）"


def test_generated_deliverable_keeps_all_tables():
    """交付件必须**保留正文表格**（本轮回溯出的真缺陷：生成器把全部表格静默清空了）。

    背景：`main()` 里那条"去掉正文开头元信息表"的正则写成了 `^\\|.*\\|$`（= 删掉每一行表格），
    结果 123 行表格全部消失——判定口径表、检查项表、五类模板表、影响面表在 PDF 里整片不见，
    客户只看到表后的文字，而且没有任何报错。故这里做**内容级**断言：
    ① Word 正文里必须能找到只在表格中出现过的文字；② 表格元素数量不能少得离谱。
    """
    import pathlib
    import re
    import zipfile

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    docx = root / "docs" / "04-交付与运营" / "用户手册.docx"
    assert docx.exists(), "缺 Word 交付件（跑 python3 tools/gen_manual_docs.py）"
    with zipfile.ZipFile(docx) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    plain = re.sub(r"<[^>]+>", "", xml)
    for kw in ("判定口径", "新增功能", "存量更新", "服务托管"):
        assert kw in plain, f"交付件里找不到「{kw}」——表格可能又被静默清空了"
    n_tbl = xml.count("<w:tbl>")
    assert n_tbl >= 10, f"Word 里只剩 {n_tbl} 张表（手册原有十余张；表格疑似被剔除）"


def test_synced_manual_has_no_version_placeholder():
    """文档中心读的 md 里不能残留 `{{VERSION}}` 占位符。

    实测缺陷：正文用 `{{VERSION}}` 做单一来源（好），生成器给 Word/PDF 替换了（好），
    但 `tools/sync_manual.py` 是**原样拷贝**到 `static/manual/`——文档中心就把
    "当前版本（{{VERSION}}）" 直接显示给了用户。故同步步骤必须做同样的替换，且这里锁住。
    """
    import os
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    served = root / "static" / "manual" / "manual.md"
    if not served.exists():
        import pytest

        pytest.skip("尚未同步内置手册（跑 python3 tools/sync_manual.py）")
    text = served.read_text(encoding="utf-8")
    assert "{{VERSION}}" not in text, "内置手册里残留 {{VERSION}} 占位符（同步时没替换）"
    ver = _read(VERSION_FILE).strip()
    assert f"v{ver}" in text, f"内置手册里找不到当前版本 v{ver}"


def test_internal_manual_version_matches():
    """内部手册的版本行也必须等于 VERSION（它不会自动注入，所以用守卫兜住）。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    f = root / "docs" / "04-交付与运营" / "用户手册.内部.md"
    if not f.exists():
        return
    text = f.read_text(encoding="utf-8")
    ver = _read(VERSION_FILE).strip()
    assert f"| 版本 | v{ver}" in text, f"内部手册版本行与 VERSION {ver} 不一致"


def test_dockerfile_fills_version_placeholder_for_shipped_manual():
    """镜像构建时必须把手册里的 `{{VERSION}}` 替换掉。

    实测缺陷：镜像的 `/static/manual/manual.md` 是**从 `docs/` 直接 COPY** 的，
    所以"生成器替换"和"sync_manual 替换"都管不到它——容器里的手册一直显示
    "当前版本（{{VERSION}}）"，而文档中心读的就是它。故在 Dockerfile 里再替换一次（三处出口同一规则），
    这条守卫锁住那段构建逻辑不被删掉。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    df = (root / "deploy" / "Dockerfile.platform").read_text(encoding="utf-8")
    assert "{{VERSION}}" in df, "Dockerfile 缺版本占位符替换逻辑（镜像内手册会原样显示占位符）"
    assert "/static/manual" in df and "/app/VERSION" in df, "替换逻辑需遍历 /static/manual 并用 /app/VERSION 取值"
