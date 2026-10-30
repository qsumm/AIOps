"""前端静态资源门禁（D3 事故回归防线）。

事故背景：static/js/pages/login.js 少一个右括号 → 整个 ES Module 图加载失败 →
index.html 的模块脚本完全不执行 → 页面空白且无报错提示。此类错误只靠浏览器肉眼才
能发现，代价高。本测试用 node --check 对所有前端模块做语法门禁，并校验 index.html
引用的资源都真实存在。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "static"
BACKEND = Path(__file__).resolve().parents[2] / "backend"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node 不可用，跳过前端语法门禁")
def test_all_frontend_modules_parse():
    mods = sorted(STATIC.glob("js/**/*.js"))
    assert mods, "未找到前端模块"
    bad: list[str] = []
    for m in mods:
        # 复制为 .mjs 让 node 按 ES Module 解析（--check 对 import 语法需要模块模式）
        tmp = Path("/tmp") / f"_check_{m.stem}.mjs"
        tmp.write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f"{m.relative_to(STATIC.parent)}: {r.stderr.strip().splitlines()[-3:]}")
    assert not bad, "前端模块语法错误：\n" + "\n".join(bad)


def test_index_html_referenced_assets_exist():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    refs = set(re.findall(r'(?:src|href)="(/[^"]+)"', html))
    assert refs, "index.html 未引用任何本地资源"
    missing = [r for r in refs if not (STATIC / r.lstrip("/")).exists()]
    assert not missing, f"index.html 引用了不存在的资源: {missing}"


def test_index_html_imports_resolve():
    """index.html 里 import ... from "/js/..." 的每个路径都必须存在。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    imports = set(re.findall(r'from\s+"(/js/[^"]+)"', html))
    assert imports, "index.html 未 import 任何页面模块"
    missing = [i for i in imports if not (STATIC / i.lstrip("/")).exists()]
    assert not missing, f"index.html import 路径不存在: {missing}"


def test_agent_install_artifacts_served_from_repo():
    """回归（手册期实测）：上手文档让用户 `curl http://<平台>/install.sh | bash`，
    但该路径此前 404（静态目录里没有、镜像里也没带 agent 源码）——一键安装整条路径走不通。

    这里断言：安装件确实取自仓库 agent/ 目录、内容与仓库文件逐字节一致，
    且脚本具备本次修正的三项能力（真正的参数解析、管道场景下载 agent_main.py、--uninstall）。
    """
    import hashlib
    from pathlib import Path

    import pytest

    pytest.importorskip("app.routers.agent_install", reason="需要后端依赖")
    from app.routers.agent_install import SRC_DIR

    repo_agent = Path(__file__).resolve().parents[2] / "agent"
    sh = SRC_DIR / "install.sh"
    agent_py = SRC_DIR / "agent_main.py"
    assert sh.is_file() and agent_py.is_file(), f"SRC_DIR={SRC_DIR} 未解析到 agent 安装件"
    # 与仓库文件一致（避免分发的是过期副本）
    assert hashlib.sha256(sh.read_bytes()).hexdigest() == hashlib.sha256((repo_agent / "install.sh").read_bytes()).hexdigest()
    body = sh.read_text(encoding="utf-8")
    assert "while [[ $# -gt 0 ]]" in body, "install.sh 应使用真正的参数解析（原为位置式 ${2}/${4}）"
    assert "/agent_main.py" in body, "管道安装场景必须能从平台下载 agent_main.py（原实现 cp 同级文件必然失败）"
    assert "--uninstall)" in body and "bootout" in body, "install.sh 必须实现 --uninstall（原文案里有、代码里没有）"
    assert "--token|--install-token)" in body, "应同时接受 --token 与 --install-token"


# ---------------- D22：空态提示不得指向对当前角色隐藏的按钮 ----------------
def test_empty_state_hints_respect_capability_gating():
    """「点 XX 按钮」类空态文案必须与该按钮的能力门禁**同源判定**。

    用户实测报障（2026-10-14）：demo 打开事件抽屉，空态写着
    「尚未诊断——点上方『一键诊断』」，但该按钮对 demo 是隐藏的（diag 能力位）——
    提示让用户去找一个不存在的按钮。同类问题：rules 页的「新建规则」（write 门禁）、
    hosts 页的「接入新主机」（点了必 403，未隐藏）。

    守卫方式（静态、粗但有效）：无权限文案与有权限文案必须**同时存在**，
    且无权限分支要引导到 demo 真正可用的等价路径。
    """
    pages = Path(__file__).resolve().parents[2] / "static" / "js" / "pages"
    inc = (pages / "incidents.js").read_text(encoding="utf-8")
    assert "尚未诊断——点上方" in inc and "尚未诊断。AI 诊断需要 admin / member" in inc, \
        "incidents 诊断空态必须按 canDiag 分支（D22 回退？）"
    assert "AI 助手" in inc, "无权限分支应引导用户去 AI 助手（demo 可用的等价只读路径）"

    rules = (pages / "rules.js").read_text(encoding="utf-8")
    # D61 后 rules 空态 = 筛选分支 + 能力分支双层（已停用视图给专属空态）
    assert 'emptyText: state.show === "disabled"' in rules and ": canWrite ?" in rules, \
        "rules 空态必须保留筛选分支与 canWrite 分支（D22/D61）"
    assert "新建规则需要 admin 权限" in rules, "无权限分支要说明原因"

    hosts = (pages / "hosts.js").read_text(encoding="utf-8")
    assert 'can("write") ? h("button"' in hosts, "接入新主机按钮必须按 write 能力隐藏（D22 回退？）"


# ---------------- D23：配色降噪 + 侧栏图标体系（规范 §11 落地）----------------
def test_nav_icons_unique_and_motion_guarded():
    """侧栏图标三纪律（§11）：每页一个、全站不重复、动效必须可关闭。"""
    import re as _re
    idx = (Path(__file__).resolve().parents[2] / "static" / "index.html").read_text(encoding="utf-8")
    items = _re.findall(r'data-page="([a-z]+)"[^>]*>.*?(<span class="nav-ico[^"]*">.*?</span>)', idx, _re.S)
    pages = [p for p, _ in items]
    assert len(items) == 17, f"应有 17 个带图标的导航项（16 页 + D154 通知路由），实际 {len(items)}"
    assert len(set(pages)) == 17, "导航页有重复"
    svgs = [_re.sub(r"\s+", " ", svg) for _, svg in items]
    assert len(set(svgs)) == 17, "图标出现重复——违反 §11『一个页面一个图标』唯一性纪律"
    # 动效只给铃铛一个，且必须带 reduced-motion 豁免（前端风格 疏漏的补课）
    assert idx.count("nav-ico-anim") == 1, "语义动效只允许铃铛一个"
    ops = (Path(__file__).resolve().parents[2] / "static" / "ops.css").read_text(encoding="utf-8")
    assert "navBellSway" in ops
    assert "prefers-reduced-motion" in ops and "animation: none" in ops, "动效缺 reduced-motion 豁免"

    # D25：hover/选中的一次性闪动必须覆盖除铃铛外的全部 10 个图标（铃铛走闲置摆动+hover 加速）
    for page in ("dashboard", "hosts", "incidents", "cases", "copilot", "inspections",
                 "rules", "runbooks", "skills", "settings", "audit", "services", "manual", "about"):
        assert f'[data-page="{page}"]:hover .nav-ico' in ops, \
            f"{page} 的图标缺 hover 闪动（前端风格 每图标一动的纪律）"
    for kf in ("vni-bar", "vni-sparkle", "vni-cell", "vni-blink", "vni-drop",
               "vni-tick", "vni-typed", "vni-gear", "vni-fan", "vni-strike", "vni-eyeblink",
               "vni-flip", "vni-tag", "vni-pulse", "vni-page"):
        assert f"@keyframes {kf}" in ops, f"缺关键帧 {kf}"


def test_d23_color_changes_present():
    """配色降噪六项的静态守卫：改回来测试就红。"""
    ops = (Path(__file__).resolve().parents[2] / "static" / "ops.css").read_text(encoding="utf-8")
    assert "--muted: #6e6e73" in ops, "P4：light muted 应为 #6e6e73（5.1:1）"
    assert ".md-body strong { font-weight: 600; }" in ops, "P2：正文强调应为 600"
    assert ".badge.PROGRESS" in ops, "P1：open 徽章应为蓝虚线 PROGRESS"
    assert '.inc-flag:not(.muted)::before' in ops, "P3：✓ 只加在已有报告上"
    assert "border-bottom: none" in ops, "P6：引用 step 应为浅底 chip"
    js = (Path(__file__).resolve().parents[2] / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    assert '"S3 通知", "SKIP"' in js, "P5：S3 通知徽章应为灰"
    inc = (Path(__file__).resolve().parents[2] / "static" / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    assert 'open" ? "PROGRESS"' in inc, "P1：open 状态应映射 PROGRESS"


def test_no_duplicate_page_titles():
    """页题只允许出现在顶栏 h1（app 路由写入），内容区不得再渲染同名 h2。

    用户实测（2026-10-15）：每个页面标题都显示两遍——顶栏 h1 与内容区 page-head h2
    各渲染一次，历史设计瑕疵。修法：内容区 h2 全部移除（前端风格 解剖 §4：
    顶栏=页题+计数胶囊+动作区），login 页无顶栏、其 h2 保留。
    """
    pages = Path(__file__).resolve().parents[2] / "static" / "js" / "pages"
    for f in pages.glob("*.js"):
        if f.name == "login.js":
            continue  # 登录页是独立全屏页，无顶栏
        assert 'h("h2"' not in f.read_text(encoding="utf-8"), \
            f"{f.name} 内容区不得再渲染 h2 页题（顶栏已有，避免双标题）"
    idx = (Path(__file__).resolve().parents[2] / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="page-title"' in idx, "顶栏 h1（唯一页题）必须存在"


def test_login_replicates_teststar_editorial_design():
    """登录页 = 前端风格 现行 editorial/C 版的一比一复刻（用户点名要求，细节不许漏）。

    逐项核对（对照 前端风格 v7-restyle.css §登录页 与 index.html 流星层）：
      流星层 9 颗（8 匀速 + 1 随机加速 mt-rush）、两段式加速 keyframes、
      reduced-motion 豁免、Manifesto 语录池与换一句、演示入口与错误提示不丢。
    """
    login = (Path(__file__).resolve().parents[2] / "static" / "js" / "pages" / "login.js").read_text(encoding="utf-8")
    ops = (Path(__file__).resolve().parents[2] / "static" / "ops.css").read_text(encoding="utf-8")
    # 流星：8 颗匀速 + 1 颗 mt-rush（9 = 全部；少一颗都算遗漏细节）
    # 匀速流星经 MT 数组循环生成（模板只出现一次），以周期值计数：8 颗均为 16s
    assert login.count('"16s"') == 8, f"匀速流星应 8 颗（周期 16s），实际 {login.count(chr(34)+'16s'+chr(34))} 颗"
    assert 'mt-rush' in login and 'animationiteration' in login and 'roll' in login, \
        "随机加速流星及其『每圈重摇参数』脚本缺失"
    # 两段式加速 keyframes（60% 时间走 50% 路程 → 1.5 倍速台阶）+ reduced-motion
    assert "@keyframes lqaMeteor" in ops and "@keyframes lqaMeteorRush" in ops
    assert "translate3d(-23vw, 29vh, 0)" in ops, "加速流星的中段锚点缺失（两段式速度曲线）"
    assert "@media (prefers-reduced-motion: reduce) { .login-screen .mt" in ops
    # Manifesto：语录池 ≥6 组 + 换一句 + 编号
    assert login.count("h: \"") >= 6, "语录池应 ≥6 组（前端风格 为 8 组）"
    assert "reroll" in login and "lqid" in login and "MANIFESTO" in login.upper()
    # 功能不丢：登录提交 / 错误提示；演示一键入口已下架（D27，公网安全）
    assert "await login(" in login and "login-error" in login
    assert "演示模式一键进入" not in login and "loginDemo" not in login, \
        "演示一键入口已下架，不得回流到登录页"
    # 样式：登录页独立色板与黑色 CTA（前端风格 C 版口径，非蓝色按钮）
    assert ".login-screen { position: fixed" in ops, "登录页需固定铺满"
    # D50：登录页按钮底色/纸底改为局部令牌（原来写死 #000/#FAF8F4，且暖奶油底与第二个红一并收敛）
    assert "background: var(--ink)" in ops and "--paper: #f7f8fa" in ops, \
        "登录页应走局部令牌（D50 收敛），不再写死 #000/#FAF8F4"


def test_d28_clipboard_has_http_fallback():
    """D28 事故回归：剪贴板复制必须能在 HTTP 非安全上下文下降级。

    事故背景：平台以 `http://<公网IP>:<端口>` 部署时，`navigator.clipboard` 是
    undefined（该 API 只在 HTTPS/localhost 存在）。代码直接调用
    `navigator.clipboard.writeText(...)` 会抛
    "Cannot read properties of undefined (reading 'writeText')"——「接入新主机/生成
    安装命令」的复制因此报错。本地 127.0.0.1 属安全上下文，测不出来。
    本门禁：① ui.js 必须提供带 execCommand 降级的 copyText；② 各页面不得再直接裸调
    navigator.clipboard（一律走 copyText）。
    """
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "export function copyText" in ui, "ui.js 必须导出 copyText"
    assert "execCommand" in ui, "copyText 必须含 execCommand 降级（HTTP 下可用）"
    assert "isSecureContext" in ui, "copyText 必须按安全上下文判定"

    offenders = []
    for m in sorted(STATIC.glob("js/pages/*.js")):
        src = m.read_text(encoding="utf-8")
        # 去掉注释行后检查裸调用
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))
        if "navigator.clipboard.writeText" in code:
            offenders.append(m.name)
    assert not offenders, f"以下页面直接裸调 navigator.clipboard（HTTP 下会报错），应改用 copyText：{offenders}"


def test_d29_incident_merge_split_wired():
    """D29：手册 §6.4 承诺「手动合并/拆分」，前端必须真的接入后端 API。

    缺陷背景：后端 `/api/ops/incidents/merge`、`/api/ops/incidents/{iid}/split`
    早已实现，但前端 incidents.js 从未调用——手册写了、界面没有，用户按手册操作找不到入口。
    """
    src = (STATIC / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    assert "/api/ops/incidents/merge" in src, "前端必须调用合并 API（手册 §6.4）"
    assert "/split" in src, "前端必须调用拆分 API（手册 §6.4）"
    assert "合并到其他事件" in src, "详情需有合并入口"
    assert "拆分" in src, "成员告警需有拆分入口"


def test_d35_agent_install_preconditions_shown():
    """D35：接入新主机的界面必须显式说明前置条件（OS/Python/权限/网络/依赖）。

    背景：用户要求——客户在自己服务器接入前要能先确认环境是否满足，否则
    "装失败"时无从排查。前置条件条目与 install.sh 实际行为一一对应。
    """
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "export function precheckCard" in ui, "ui.js 必须提供 precheckCard"
    for kw in ("操作系统", "Python", "权限", "网络"):
        assert kw in ui, f"前置条件缺少「{kw}」条目"
    assert "/usr/bin/python3" in ui, "macOS 的 Python 说明应指向系统自带解释器"
    hosts = (STATIC / "js" / "pages" / "hosts.js").read_text(encoding="utf-8")
    st = (STATIC / "js" / "pages" / "settings.js").read_text(encoding="utf-8")
    assert "precheckCard(" in st, "「Agent 接入」必须展示前置条件"
    # 「接入新主机」改成了两步式向导（v0.6）：前置条件不再是静态卡片，而是**自检项清单**
    # （覆盖面反而更广：多了时间同步/服务托管/既有安装）。这里锁"意图"——那些条目必须还在，
    # 而不是绑死某个函数名（否则改版即误红）。
    for kw in ("操作系统", "Python", "网络", "资源与权限", "服务托管", "依赖", "既有安装"):
        assert f'["{kw}"' in hosts, f"接入向导的检查项清单缺少「{kw}」"


def test_d37_fmt_time_converts_to_local_timezone():
    """D37：fmtTime 必须按本地时区显示（后端存 UTC）。

    缺陷（用户在服务器上发现）：平台所有绝对时间比本地早 8 小时。
    根因：`fmtTime` 直接 `String(s).slice(0,19)` 截取后端返回的 UTC 字符串，
    从不做时区换算。修复：解析为 UTC 后按浏览器本地时区渲染。
    """
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    # 取 fmtTime 定义段
    i = ui.index("export const fmtTime")
    seg = ui[i:i + 900]
    assert "getFullYear" in seg and "getHours" in seg, \
        "fmtTime 必须用本地时区 getter 渲染（此前直接截字符串 → 时间差 8 小时）"
    assert "isNaN" in seg, "fmtTime 需对无法解析的时间做降级"
    # 不再有"直接截字符串"的老实现
    assert 'slice(0, 19) : "-"' not in ui, "旧的字符串截取实现未清除"


def test_d38_toolbar_selects_do_not_fill_row():
    """D38：页头工具条里的下拉不得继承表单的 width:100%。

    缺陷（用户报障）：告警中心页头「实时流 + 状态/级别筛选 + 批量按钮」被挤成竖排、
    且下拉占满整行宽度。根因：select() 复用 .vs-input（为表单设计，width:100%），
    放进 .page-head .actions 这个 flex 容器后每个下拉都撑满 → flex-wrap 换行。
    修复：.page-head .actions select.vs-input { width: auto; min-width: … }
    """
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    assert ".page-head .actions select.vs-input" in css, "工具条下拉需覆盖 width:100%（否则竖排占满行）"
    seg = css[css.index(".page-head .actions select.vs-input"):][:120]
    assert "width: auto" in seg, "工具条下拉应改回自适应宽度"


def test_d40_host_shows_name_not_ident():
    """D40：界面显示主机**名**，而不是 ident 哈希。

    缺陷（用户报障：「391877d6656a 是啥？看不懂」）：后端多处只返回 host_ident
    （机器指纹哈希），前端直接截前 10 位显示 → 大盘 CPU Top5 / 告警表主机列 /
    AI 摘要卡 / 告警详情都显示一串哈希，用户无法辨认是哪台机器。
    修复：ui.js 提供 hostLabel()（ident→主机名映射，未命中回退短 ident），各页先
    await ensureHostNames() 再渲染。
    """
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "export function hostLabel" in ui, "ui.js 需提供 hostLabel"
    assert "export async function ensureHostNames" in ui, "ui.js 需提供 ensureHostNames"
    assert "ident.slice(0, 10)" in ui, "hostLabel 未命中时应回退短 ident"
    # 三个展示页不得再直接截 host_ident 显示
    for page in ("dashboard.js", "alerts.js", "incidents.js"):
        src = (STATIC / "js" / "pages" / page).read_text(encoding="utf-8")
        assert "hostLabel(" in src, f"{page} 必须用 hostLabel 显示主机名"
        assert "ensureHostNames()" in src, f"{page} 渲染前需 await ensureHostNames()"
        assert "hostLabel" in src.split("\n")[5] or "hostLabel" in src[:600], f"{page} 需 import hostLabel"


def test_d41_host_detail_is_shared_module():
    """D41：主机详情必须是**独立模块**，供主机管理页与大盘共用。

    缺陷背景：详情原先定义在 hosts.js 的 render 闭包里，大盘点击要直达同一抽屉时
    外部拿不到（试过"动态 import + 模块内变量共享"，因模块实例解析差异而点击无效——
    实测踩到）。抽成 host_detail.js 后两处 import 同一份，行为一致、无共享状态。
    """
    lib = STATIC / "js" / "host_detail.js"
    assert lib.exists(), "host_detail.js 独立模块缺失"
    src = lib.read_text(encoding="utf-8")
    assert "export function showHostDetail" in src
    assert "export async function openHostDetail" in src
    hosts = (STATIC / "js" / "pages" / "hosts.js").read_text(encoding="utf-8")
    dash = (STATIC / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")
    assert 'from "../host_detail.js"' in hosts, "主机管理页应复用独立详情模块"
    assert 'from "../host_detail.js"' in dash, "大盘应复用独立详情模块（点击直达详情）"
    assert "showHostDetail(r)" in hosts, "主机列表点行应打开详情"
    assert "openHostDetail(t.ident)" in dash, "大盘榜单点击应直达该主机详情"


def test_d42_dashboard_visual_polish():
    """D42：大盘视觉打磨的关键点（用户报「糙、不精致」）——锁住，防回退。

    覆盖面：卡片层次（阴影+更淡边框）、数字冲击力（字号/字重/等宽/单位分层）、
    间距栅格（16px）、进度条不铺满整行、图表虚线网格 + 精细 tooltip、表格分隔线更淡。
    """
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    assert "--shadow-card" in css and "--hairline" in css and "--border-soft" in css, "精细化令牌缺失"
    assert "box-shadow: var(--shadow-card)" in css, "卡片缺层次（阴影）"
    assert "font-variant-numeric: tabular-nums" in css, "数字应用等宽（tabular-nums）"
    assert ".stat-value .unit" in css, "数字应有单位分层"
    # 进度条不得再铺满整行（原 grid 最后一列 1fr → 拉成色块，用户报「粗糙」）
    assert "minmax(0, 132px)" in css, "进度条应限宽（不得铺满整行）"
    assert "rgba(16, 24, 40, .04)" in css or "16, 24, 40" in css, "缺卡片阴影具体值"

    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "export function tooltipStyle" in ui, "图表 tooltip 应统一精细样式"
    assert "type: [4, 4]" in ui, "网格应为虚线（提供参照又不过重）"
    assert "axisLine: { show: false }" in ui, "坐标轴线应隐去（更干净）"

    # 大盘不得再自绘 stat-card（会绕开 ui.js 的单位分层，D42 实测踩到）
    dash = (STATIC / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")
    assert "statCard(label, value, sub, cls)" in dash, "大盘统计卡应复用 ui.js 的 statCard"
    assert 'class: `stat-card' not in dash, "大盘不应自绘 stat-card（会丢失单位分层）"


def test_d43_global_visual_language():
    """D43：D42 的视觉语言必须**全局统一**（不只是大盘）——锁住令牌与关键组件。

    用户要求：「其他界面都按照这种风格 更新优化精细一点…全局，包括子菜单。弹窗等。」
    """
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    for tok in ("--radius-sm", "--shadow-pop", "--ease", "--focus-ring"):
        assert tok in css, f"缺全局令牌 {tok}"
    # 关键组件都纳入统一打磨
    for sel in (".nav-item", ".detail-panel", ".drawer-mask", ".btn", ".badge",
                ".vs-input", ".tabs", ".toast", ".inc-card", ".topbar"):
        assert sel in css, f"组件 {sel} 未纳入全局精细化"
    assert ":focus-visible" in css, "缺键盘焦点可见性（可访问性）"
    assert "::-webkit-scrollbar" in css, "缺统一滚动条样式"
    # 深色主题必须覆盖阴影令牌（深色里黑阴影基本不可见，应改浅色描边/更深投影）
    dark_blocks = re.findall(r'\[data-theme="dark"\]\s*\{([^}]*)\}', css)
    assert any("--shadow-card" in b and "--shadow-pop" in b for b in dark_blocks), \
        "深色主题需覆盖阴影令牌（深色里黑阴影不可见）"


def test_d44_no_emoji_and_professional_icons():
    """D44：界面与报告一律**不用 emoji**（用户：「不要 ⚠️ 这种很low的 icon，要专业的、用心的」）。

    改用 ui.js 的 svgIcon 线性图标体系（与侧栏导航同构）；后端 Markdown 报告
    不能嵌 SVG，改用专业文字表达。
    """
    import re
    EMOJI = re.compile(r"[⚠✅❌🎉🔒⏳◐💭]")
    # 前端：源码里的**字符串**不得含 emoji（注释允许说明性提及）
    for f in list((STATIC / "js").rglob("*.js")) + [STATIC / "index.html"]:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith(("//", "*", "/*")):
                continue
            if EMOJI.search(line):
                raise AssertionError(f"{f.name}:{i} 仍含 emoji：{line.strip()[:70]}")
    # 图标体系存在
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "export function svgIcon" in ui and "ICON_PATHS" in ui, "缺专业图标体系"
    for n in ("alert", "check", "x", "lock", "clock", "theme"):
        assert f"{n}:" in ui, f"图标集缺 {n}"
    # 后端报告文案不得含 emoji
    for f in (BACKEND / "app" / "services").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if EMOJI.search(line):
                raise AssertionError(f"{f.name}:{i} 报告文案仍含 emoji：{line.strip()[:70]}")
    # 报告宽幅（否则宽表格被挤成竖排断行）
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    assert ":has(.report-wide)" in css, "缺报告宽幅规则（含宽表格的报告不该用窄抽屉）"


def test_d48_copilot_ui_hierarchy():
    """D48：AI 助手的界面要素（专业/规范/可信/好用）——锁住，防回退。

    - 专业：用户消息用克制的浅底（不是高饱和大蓝块）；助手消息白底细框
    - 可信：每条消息带角色与时间；工具调用结构化成卡片（查阅透明度）
    - 好用：输入区自适应高度 + Enter 发送 / Shift+Enter 换行 + 快捷键提示
    """
    js = (STATIC / "js" / "pages" / "copilot.js").read_text(encoding="utf-8")
    css = (STATIC / "ops.css").read_text(encoding="utf-8")

    # 可信：角色 + 时间戳 + 工具卡片
    assert "msgMeta(" in js and "msg-time" in js, "消息需带角色与时间（可信）"
    assert "msg-who" in js, "消息需标注发送者"
    assert "toolBlock(" in js and "本次查阅了" in js, "工具调用需结构化呈现"
    # 好用：快捷键 + 自适应
    assert 'e.key === "Enter" && !e.shiftKey' in js, "Enter 发送需实现"
    assert "scrollHeight" in js and "max-height" not in js, "输入框需自适应高度（JS 侧）"
    assert "chat-input-hint" in js and "Enter 发送" in js, "需给快捷键提示"
    # 专业：克制配色（浅底 + 细框，而非高饱和大色块）
    assert ".msg.user { background: var(--accent-weak)" in css, "用户消息应用克制的浅底（D50 起走令牌）"
    assert ".msg.agent { background: var(--surface)" in css, "助手消息应用白底细框"
    assert ".msg-tools {" in css and ".tool-line {" in css, "工具卡片样式缺失"
    assert ".chat-input-hint {" in css, "快捷键提示样式缺失"


def test_d58_incident_report_dual_source_fallback():
    """D58：详情报告必须双来源回退——findings 表为空时回落事件表 ai_report_md。

    用户实测 #5（演示种子数据）：列表按 ai_report_md 判「已有 AI 报告」，详情只读
    findings 表 → 显示「尚未诊断」，自相矛盾。锁：回退分支存在 + 占位提示只在两处皆空时出现。
    """
    js = (STATIC / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    assert "full.ai_report_md" in js and "findings_md: full.ai_report_md" in js, \
        "详情需从事件表 ai_report_md 回退渲染（种子/导入的报告）"
    assert "该报告为预置" in js or "无工具调用记录" in js, "无 steps 时需给出可读说明"


def test_d54_stat_value_unit_split_safe_for_pure_numbers():
    """D54-1：statCard 的"数值/单位"拆分不得把纯数字的末位当单位（155→15+小5 的用户实测）。

    正则语义即契约：单位组必须以非数字开头（\D.+），纯数字值整体保持大字。
    """
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "\\D.*" in ui and "match(/^([\\d.,]+)" in ui, "statCard 单位拆分必须要求单位以非数字开头（含单字符单位 %）"
    # 旧写法（会把 155 拆成 15+5）不得回来
    assert "match(/^([\\d.,]+)(.+)$/)" not in ui, "旧的贪婪拆分正则回来了（会把纯数字末位当单位）"


def test_d49_chat_suggest_prefetch_and_refresh():
    """D49：建议问题「定时预生成 + 缓存 + 手动刷新」的前端契约（防回退）。

    用户要求：打开对话框不能等模型（等十几秒就看不到问题了）+ 刷新按钮立即更新。
    前端三件事必须成立：
    ① 读缓存（GET 即时）+ 规则版时做一次 12 秒补取，把模型版平滑换上；
    ② 刷新按钮 → POST /refresh，加载中禁止连点，失败可恢复；
    ③ 界面显示"更新于 HH:MM"（时间戳来自后端，只有一个来源）。
    """
    js = (STATIC / "js" / "pages" / "copilot.js").read_text(encoding="utf-8")
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    css = (STATIC / "ops.css").read_text(encoding="utf-8")

    assert 'api.get("/api/ops/chat/suggestions")' in js, "建议问题必须读缓存接口"
    assert 'api.post("/api/ops/chat/suggestions/refresh")' in js, "刷新按钮必须打 /refresh"
    assert 'sug.source === "rule"' in js and "12000" in js, "规则版需做一次后台补取（12s）"
    assert "welcomeSeq" in js, "异步结果回填需有'是否还在欢迎屏'的守卫"
    assert 'fmtTime(sug.generated_at).slice(11, 16)' in js, "需显示'更新于 HH:MM'"
    assert "btn.disabled = true" in js and "更新中…" in js, "刷新需有加载态并防连点"
    # D49 实测缺陷：querySelector("span") 会选中**图标** span（图标被文字顶掉、标签还是"刷新"）
    assert 'querySelector("span:not(.ico)")' in js, "更新中状态必须只改标签，不能改到图标"
    assert ".chat-sug-refresh.loading .ico svg" in css, "加载态需让线性图标转圈"
    assert 'toast(e.message || "更新失败' in js, "刷新失败需可恢复并提示"
    # 图标与样式：线性刷新图标（不用 emoji/字符图标）
    assert "refresh:" in ui and "23 4 23 10 17 10" in ui, "刷新图标需加入线性图标表"
    assert ".chat-sug-refresh {" in css and ".chat-sug-head {" in css and ".chat-sug-time {" in css, \
        "刷新按钮/标题行样式缺失"


def test_d59_required_field_system_wired_everywhere():
    """D59：全局表单必填体系——共享校验助手 + 必填红星 + 关键表单接入（防回退）。

    用户报：渠道名称可空保存（列表一行空白）且保存后不能编辑。锁三层：
    ① ui.js 有共享助手（needText/needUrl/reqLabel）
    ② 渠道编辑器支持编辑（PUT + 留空保留掩码凭据的语义）
    ③ 各表单的保存回调真的调用了校验（settings/rules/alerts/inspections/skills/runbooks/hosts）
    """
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "export const needText" in ui and "export const needUrl" in ui and "export const reqLabel" in ui, \
        "ui.js 缺共享校验助手（needText/needUrl/reqLabel）"
    assert 'class: "req"' in ui, "必填红星元素缺失"

    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    assert ".req { color: var(--fail)" in css, "必填红星样式缺失"

    st = (STATIC / "js" / "pages" / "settings.js").read_text(encoding="utf-8")
    assert "api.put(`/api/ops/notify-channels/${c.id}`" in st, "渠道编辑必须走 PUT（保存后不能编辑的用户报缺陷）"
    assert "留空 = 保持不变" in st, "编辑框留空必须明示=保留旧值（出参是掩码不能写回）"
    assert "渠道名称为必填项" not in st, "错误文案应来自共享助手而非页内硬编码"
    for kw in ("needText(name.value", "needUrl("):
        assert kw in st, f"settings.js 表单未接入校验：{kw}"

    rules = (STATIC / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
    assert "api.put(`/api/ops/prom-datasources/${ds.id}`" in rules, "数据源必须有编辑入口（PUT 后端早已有、UI 一直缺）"
    assert 'ds_dialog(r)' in rules or "ds_dialog(ds)" in rules, "数据源对话框需支持编辑模式"
    assert "needText(f.name.value" in rules, "规则表单未接入名称校验"

    alerts = (STATIC / "js" / "pages" / "alerts.js").read_text(encoding="utf-8")
    assert 'needText(cause.value, "屏蔽原因"' in alerts, "抑制表单未接入原因必填校验"

    for page, kw in (("inspections", 'needText(name.value, "任务名"'),
                     ("skills", 'needText(name.value, "技能名称"'),
                     ("runbooks", 'needText(name.value, "变更单名称"'),
                     ("hosts", 'needText(input.value, "主机名"')):
        js = (STATIC / "js" / "pages" / f"{page}.js").read_text(encoding="utf-8")
        assert kw in js, f"{page}.js 表单未接入必填校验"


def test_d60_no_internal_jargon_in_ui():
    """D60：用户可见文案不得出现内部代号/文档编号（用户报「前端风格 是啥？」）。

    只查**渲染字符串**（JS 字符串字面量），代码注释里保留出处说明是允许的——
    这里用"出现在引号内"的粗略判定：对每个文件，剔除整行注释后再查关键词。
    """
    import re as _re

    def strip_line_comments(js: str) -> str:
        # 先整体剔除 /* ... */ 块注释（可跨行），再剔除行注释
        js = _re.sub(r"/\*.*?\*/", "", js, flags=_re.S)
        out = []
        for line in js.splitlines():
            if line.strip().startswith("//"):
                continue
            out.append(line)
        return "\n".join(out)

    banned = ("前端风格", "轨 A", "轨 B", "规范 §", "ADR-", "BUILD 00")
    pages = ["settings", "rules", "alerts", "login", "hosts", "incidents", "dashboard"]
    for p in pages:
        js = strip_line_comments((STATIC / "js" / "pages" / f"{p}.js").read_text(encoding="utf-8"))
        for kw in banned:
            assert kw not in js, f"{p}.js 渲染文案里仍有内部代号「{kw}」"


def test_d60_effective_time_structured_editor():
    """D60→D144：规则「生效时段」必须有结构化编辑，不能裸露 [] 让用户猜。

    用户报：「只默认填了一个 []，作为用户看到 [] 知道是干啥的吗？」
    D144 用户再报：「生效时段改为时间选择框，如 00:00~23:59，用户自己选。
    标题是告警通知生效时间 (UTC+8)」→ 简化为纯时间范围（默认全天），
    星期/多时段降级为「高级 JSON」；并修掉旧实现 raw JSON 预填导致
    结构化编辑永远不生效的 bug。
    """
    js = (STATIC / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
    assert "collectEff" in js, "生效时段需结构化收集"
    assert 'type: "time"' in js, "必须是时间选择框（用户点名）"
    assert "告警通知生效时间 (UTC+8)" in js, "标题必须是「告警通知生效时间 (UTC+8)」"
    assert "00:00" in js and "23:59" in js, "默认 00:00~23:59 = 全天"
    assert "rule-advanced" in js and "高级" in js, "按星期/多时段保留为高级 JSON 入口"
    # raw JSON 不得预填原值（否则结构化编辑永远不生效——D144 实测 bug）
    assert 'value: JSON.stringify(rule?.effective_time' not in js, "高级 JSON 不得预填原值（会覆盖时间框编辑）"
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    assert ".eff-time-row {" in css, "时间选择行样式缺失"
    assert ".rule-editor {" in css and ".form-section {" in css, "规则编辑分段样式缺失"
    assert ".detail-panel:has(.rule-editor)" in css, "规则编辑抽屉必须加宽（用户点名「界面有点窄了」）"


def test_d145_page_head_buttons_reach_their_handlers():
    """D145：页头按钮引用的函数必须在 **render 作用域**（不能只定义在 draw() 内部）。

    实测缺陷：巡检页「新建巡检」按钮点下去**毫无反应**——`editor` 被定义在 draw() 内部
    （缩进 4 空格），而按钮定义在 render 作用域 → 运行时 ReferenceError: editor is not
    defined，行内「编辑」能用（它也在 draw 内）掩盖了这个问题。E2E 全量回归时抓到的。

    机械规则：某函数若**只在** 4 空格缩进处定义（= 嵌套在 draw 内），而文件前 40 行
    （页头区）却引用了它 → 必然 ReferenceError。
    """
    import re
    pages_dir = Path(__file__).resolve().parents[2] / "static" / "js" / "pages"
    problems = []
    for f in pages_dir.glob("*.js"):
        lines = f.read_text(encoding="utf-8").splitlines()
        defs = {}          # name -> set(indent)
        for ln in lines:
            m = re.match(r"^(\s*)function (\w+)\(", ln)
            if m:
                defs.setdefault(m.group(2), set()).add(len(m.group(1)))
        head = "\n".join(lines[:40])
        for name, indents in defs.items():
            if 4 in indents and 2 not in indents and re.search(rf"\b{name}\s*\(", head):
                problems.append(f"{f.name}: {name} 只定义在 draw() 内部，却被页头引用")
    assert not problems, "页头按钮引用不到处理函数（点击会静默失败）：" + "; ".join(problems)


def test_d144_alerts_checkbox_column_before_seq():
    """D144：告警中心的勾选列必须在序号之前，且**表头全选框与行复选框同列**。

    用户报：「序号后面是勾选的空格，空格应该在序号前面」——此前拆成两列
    （表头一列放全选框、行复选框又一列），表头框与行勾选上下错开一列、
    中间还夹着一个空表头格。合并成一列后：勾选列（含表头全选框）→ 序号 → 级别。
    """
    js = (STATIC / "js" / "pages" / "alerts.js").read_text(encoding="utf-8")
    seg = js[js.index("body.replaceChildren(table(["):][:4000]
    start = seg.index('{ title: h("input"')
    end = seg.index('{ title: "序号"')
    col = seg[start:end]
    assert "alert-sel-all" in col, "表头全选框必须与行复选框在同一列"
    assert "data-hash" in col, "行复选框必须与表头全选框同列"
    assert 'title: "", width: "34px"' not in seg, "不应再有独立的空复选框列（表头与行错位）"
    assert end < seg.index('thLabel("级别"'), "序号必须排在级别之前"
    # 「全选本页」工具按钮不能把表头那个开关也当成行勾选（此前会误改表头状态）
    assert 'querySelectorAll("#alerts-body input[data-hash]")' in js, "全选本页只应作用于行复选框"


def test_d60_mute_lifecycle_visible():
    """D60：抑制创建后必须有「然后呢」——可查看、可解除（用户报创建成功却不知道作用在哪看）。"""
    js = (STATIC / "js" / "pages" / "alerts.js").read_text(encoding="utf-8")
    assert "showMutes" in js and "抑制管理" in js, "需有抑制管理列表入口"
    assert "解除" in js and "/api/ops/alert-mutes/${r.id}" in js, "抑制列表需可解除（DELETE）"
    assert "新建抑制" in js, "抑制入口需自解释命名（不是『抑制…』）"
    # 创建后应把用户带到能看到的地方
    assert "showMutes();" in js, "创建成功后应自动打开抑制管理（别让用户猜）"

# ── 前端 JS 语法自检（我在本轮里连续踩了三次"字符串里混进 ASCII 双引号"）──


def test_manual_headings_have_no_inline_markdown():
    """文档中心三篇的**标题**里不得出现行内 markdown 标记。

    实测缺陷（用户截图）：手册有个标题写成 `## 界面上的 **?** 怎么看`——正文渲染成粗体没问题，
    但目录与面包屑取的是原始标题文本，界面上直接显示 `界面上的 **?** 怎么看`，看着像打错了。
    文档中心已做剥离（`plainTitle`）作为防御，但**标题本身就该是纯文本**，故这里再锁一道。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    docs = root / "docs" / "04-交付与运营"
    bad = []
    for name in ("用户手册.md", "快速开始.md"):
        f = docs / name
        if not f.exists():
            continue
        for i, ln in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            if re.match(r"^#{2,4}\s", ln) and re.search(r"\*\*|`|\[", ln):
                bad.append(f"{name}:{i} {ln.strip()[:60]}")
    assert not bad, "以下标题含行内 markdown 标记（目录/面包屑会原样显示）：\n" + "\n".join(bad)
