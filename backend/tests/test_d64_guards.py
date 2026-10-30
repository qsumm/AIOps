"""D64 并发/幂等守卫回归（用户要求：检查有没有做防止重复点击提交 + 边界/异常覆盖）。

本文件锁四类：
  ① 前端写操作防重复提交：所有会写库的点击/下拉 handler 必须有守卫
     （busy() 转圈禁用，或等价的 disabled 锁）——静态机械守卫；
  ② 对话「生成中」并发守卫：回答流式输出期间不得再开第二条流（真实急性子场景）；
  ③ 服务快照的**边界与异常输入**：超大数组 / 错形状 / 脏元素 → 清理并截断，不得穿透；
  ④ 重注册同步 agent_version（升级后列表不再永远显示旧版本）。
"""
import importlib
import re

import pytest

from app.store import ring

PAGES_DIR = None
UI_JS = None


def _paths():
    import pathlib
    global PAGES_DIR, UI_JS
    static = pathlib.Path(__file__).resolve().parent.parent.parent / "static"
    PAGES_DIR, UI_JS = static / "js" / "pages", static / "js" / "ui.js"


# ── ① 前端写操作防重复提交（静态机械守卫）──
def test_all_write_handlers_are_guarded():
    """所有含 api.post/put/del 的 handler，其函数体内必须出现守卫三选一：
    busy( / disabled = true / .disabled = 。

    覆盖范围：pages/*.js —— 本测试就是"新加的按钮别忘了加防护"的机械提醒。
    允许的例外：调用点在被 busy 包裹的闭包里（如 `busy(btn, "", () => api.post(...))`），
    该行本身含 busy( 或在守卫窗口内。
    """
    import pathlib

    _paths()
    violations = []
    for f in sorted(PAGES_DIR.glob("*.js")):
        src = f.read_text(encoding="utf-8")
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"api\.(post|put|del)\(", line):
                continue
            # 该调用点向上 16 行 / 向下 2 行窗口内必须有守卫
            lo, hi = max(0, i - 16), min(len(lines), i + 3)
            window = "\n".join(lines[lo:hi])
            if re.search(r"busy\(|disabled\s*=|\.disabled\b", window):
                continue
            violations.append(f"{f.name}:{i + 1}: {line.strip()[:90]}")
    assert not violations, "以下写操作疑似无防重复提交守卫：\n" + "\n".join(violations)


def test_copilot_streaming_concurrency_guard():
    """对话生成中禁止再发（并发第二条 SSE 流会互相覆盖消息）。"""
    _paths()
    js = (PAGES_DIR / "copilot.js").read_text(encoding="utf-8")
    assert re.search(r"if \(streaming\) \{ toast\(", js), "ask() 缺生成中并发守卫"
    # 重复的 Enter 监听器必须只剩一个（曾经同元素注册两次，靠清空输入框侥幸不双发）
    assert js.count('input.addEventListener("keydown"') == 1, "Enter 监听器重复注册（会双发 ask）"


def test_status_badge_chinese_mapping():
    """状态徽章必须说人话：英文枚举 → 中文（D61 用户截图：ack/firing 直接显示）。"""
    _paths()
    ui = UI_JS.read_text(encoding="utf-8")
    for en, zh in (("firing", "告警中"), ("ack", "已确认"), ("open", "进行中"),
                   ("up", "在线"), ("down", "离线"), ("draft", "草稿")):
        # D106 起状态文案是 [label, tooltip] 数组；兼容旧的纯字符串形态
        assert re.search(rf'{en}\s*:\s*(?:\[\s*)?"{zh}"', ui), f"statusBadge 缺 {en} → {zh} 映射"


# ── ③ 服务快照边界/异常输入 ──
def test_services_snapshot_rejects_malformed_and_caps_size():
    ring.set_services("d64-bad", "not-a-dict")
    assert ring.get_services("d64-bad") == {}          # 非 dict 直接拒绝

    ring.set_services("d64-bad2", {"ports": "x", "processes": None})
    d = ring.get_services("d64-bad2")
    assert d["ports"] == [] and d["processes"] == []    # 错形状归一为空列表
    assert d.get("at"), "P1 起快照带采集时间（供诊断判断新鲜度）"

    # 超大数组必须截断（100 端口 / 50 进程上限）
    big = {"ports": [{"port": 1000 + i, "addr": "x", "proc": "p"} for i in range(500)],
           "processes": [{"name": f"p{i}", "cpu": 1.0} for i in range(500)]}
    ring.set_services("d64-big", big)
    d = ring.get_services("d64-big")
    assert len(d["ports"]) == 100 and len(d["processes"]) == 50

    # 脏元素：端口非数字 / 无进程名 → 剔除；CPU 不可解析 → 归零不抛
    ring.set_services("d64-dirty", {
        "ports": [{"port": "abc"}, {"port": 22, "addr": "*:22", "proc": "sshd"}, {"nope": 1}],
        "processes": [{"name": ""}, {"name": "python", "cpu": "not-a-number"}, {"name": "ok", "cpu": 3.14}],
    })
    d = ring.get_services("d64-dirty")
    assert [p["port"] for p in d["ports"]] == [22]
    assert d["processes"] == [{"name": "python", "cpu": 0.0}, {"name": "ok", "cpu": 3.1}]


# ── ④ 重注册同步版本 ──
@pytest.mark.asyncio
async def test_reregister_syncs_agent_version():
    """Agent 升级重跑安装命令后，主机列表的版本号必须跟着变（原先永远 0.1.0）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from app.models.host import Host
    from app.services import ingest
    from app.db.session import SessionLocal
    from sqlalchemy import delete, select

    import hashlib

    fp = "d64-version-fp"
    ident = hashlib.sha256(fp.encode()).hexdigest()[:32]   # 库里的 ident 是 sha256(fp) 前 32 位
    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()

    async with SessionLocal() as db:
        tok = ingest.mint_install_token()
        await ingest.register_agent(db, {"fingerprint": fp, "hostname": "d64-host",
                                         "agent_version": "0.1.0", "install_token": tok})
        # 重注册（Agent 升级后）：不带令牌也必须成功（老指纹免令牌）+ 版本同步
        await ingest.register_agent(db, {"fingerprint": fp, "hostname": "d64-host",
                                        "agent_version": "0.2.0", "os": "Linux d64"})
        row = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        assert row.agent_version == "0.2.0", f"重注册未同步版本：{row.agent_version}"
        assert row.os == "Linux d64"
        assert row.name == "d64-host"                      # 名字不被覆盖（D56 语义）
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


# ── D69 案例卡向量质量 ──
def test_case_card_embed_quality_guards():
    """D69：短泛文本不得进入向量检索；向量文本可与展示字段解耦。

    实测背景（本机三卡库）：测试期留下的一张「CPU 高负载」卡（5 字）在向量空间里
    成了"黑洞"，把磁盘类/连接类的改写查询全吸走——Top-1 命中率 78% → 67%。
    修法：文本过短不写向量（卡仍在库、仍可被文本召回，只是不污染语义检索）；
    自动沉淀的案例卡用「标题+根因+报告摘要」算向量，展示字段保持简洁。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    cases = (root / "backend" / "app" / "services" / "cases.py").read_text(encoding="utf-8")
    assert "_MIN_EMBED_CHARS" in cases, "缺短文本守卫常量"
    m = re.search(r"_MIN_EMBED_CHARS\s*=\s*(\d+)", cases)
    assert m and int(m.group(1)) >= 10, "短文本阈值过小，挡不住泛化文本"
    assert "embed_text" in cases, "向量文本未与展示字段解耦（embed_text 参数缺失）"
    # 变量名不得叫 text（会遮蔽 sqlalchemy.text 导入）——本改动第一版就踩了这个坑
    assert not re.search(r"^\s+text = ", cases, re.M), "局部变量 text 遮蔽了 sqlalchemy 的 text() 导入"
    # 过短时必须跳过嵌入（而不是照样写一个泛化向量）
    assert re.search(r"if len\(embed_input\) < _MIN_EMBED_CHARS", cases), "未按长度守卫决定是否写向量"

    inc = (root / "backend" / "app" / "routers" / "incidents.py").read_text(encoding="utf-8")
    assert "embed_text=_embed_text" in inc, "事件关闭沉淀案例卡时未传丰富的向量文本"
    assert "ai_report_md" in inc.split("embed_text=_embed_text")[0][-400:], \
        "向量文本应包含 AI 报告摘要（否则仍是短标题）"


# ── ⑤ 任务抽屉按 kind 分路（P5 终验实测缺陷）──
def test_task_drawer_renders_by_kind():
    """恢复动作与日志取证是两类任务、字段互不相同：抽屉标题与结果渲染必须分路。

    实测缺陷（P5 终验，浏览器里看到）：heal 下发后复用了「日志取证」抽屉，
    动作结果被当日志渲染——面板标题写"日志取证 #131"、字段位显示"扫描行数 -"，
    结果区还印"窗口内没有取到错误日志"。对用户是纯误导。
    """
    _paths()
    js = (PAGES_DIR / "services.js").read_text(encoding="utf-8")
    assert re.search(r'function pollTask\(tid, kind = "log_fetch"\)', js), "pollTask 未按 kind 分路"
    assert re.search(r"function renderAction\(box, t\)", js), "缺动作结果渲染器"
    assert 'pollTask(r.task_id, "svc_action")' in js, "heal 下发后仍走日志抽屉（会显示扫描行数 -）"
    assert 'pollTask(r.task_id, "log_fetch")' in js, "取证下发未显式声明 kind"
    assert "pollTask(r.id, r.kind)" in js, "任务列表「查看」未把 kind 带下去（动作又会渲染成日志）"
    assert "post_check" in js, "动作结果未展示后置校验（FR-411 的可见落点）"

    # 事件页「该主机未登记服务」的兜底取证也必须按触发时刻锚定窗口，不能退化成空窗
    inc_js = (PAGES_DIR / "incidents.js").read_text(encoding="utf-8")
    assert "function anchorWindow(anchor)" in inc_js, "缺锚点窗口计算"
    assert "since: w.since, until: w.until" in inc_js, "主机维度兜底取证未传锚定窗口"


# ── ⑥ replaceChildren 的 null 子节点（会渲染成文字 "null"）──
_STR = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`", re.S)
_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_LINE = re.compile(r"//[^\n]*")
_RX = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+/[a-z]*")


def _strip_js(src):
    """剥掉字符串/正则/注释，避免括号与 replaceChildren(null) 这类文字被误判。"""
    return _RX.sub("/r/", _LINE.sub("", _BLOCK.sub("", _STR.sub("''", src))))


def _bare_null_args(path):
    """找出把 null 直接交给 replaceChildren 的调用点（返回 ["文件:行"]）。"""
    src = _strip_js(path.read_text(encoding="utf-8"))
    hits = []
    for m in re.finditer(r"replaceChildren\(", src):
        i, depth, args, cur = m.end(), 1, [], ""
        while i < len(src) and depth > 0:
            ch = src[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    break
            elif ch == "," and depth == 1:
                args.append(cur)
                cur = ""
                i += 1
                continue
            cur += ch
            i += 1
        args.append(cur)
        if "filter(Boolean)" in "".join(args):   # 显式过滤过，安全
            continue
        if any(a.strip() == "null" or re.search(r":\s*null\s*$", a.strip()) for a in args):
            hits.append(f"{path.name}:{src[:m.start()].count(chr(10)) + 1}")
    return hits


def test_no_bare_null_children_in_replace_children():
    """Element.replaceChildren(null) 会按 DOM 规范渲染成文本 "null"。

    D58 在种子报告上踩过一次（当场加了 .filter(Boolean)），P5 终验又踩到 5 处——
    恢复结果面板上真的印出了两行 "null"（用户看到的就是脏）。条件子节点一律改用
    ui.js 的 fill()（内部丢弃 null/undefined/false/空串）。
    """
    import pathlib

    _paths()
    static = pathlib.Path(__file__).resolve().parent.parent.parent / "static" / "js"
    ui = (static / "ui.js").read_text(encoding="utf-8")
    assert "export function fill(" in ui, "缺 fill() 助手"
    assert "k !== null && k !== undefined && k !== false" in ui, "fill() 未过滤假值"
    # ui.js 是 fill() 自身的实现（其 replaceChildren 参数就是那个过滤表达式），不参与扫描
    scanned = [p for p in static.rglob("*.js") if p.name != "ui.js"]
    hits = [h for p in sorted(scanned) for h in _bare_null_args(p)]
    assert not hits, "以下调用点会把 null 渲染成文字 \"null\"（改用 fill()）：\n" + "\n".join(hits)




# ── ⑦ 恢复对话框：按钮语义必须跟着「执行方式」走（用户实测反馈）──
def test_heal_dialog_button_semantics_follow_mode():
    """默认「仅建议」下那个按钮以前叫「执行」，点了只弹一句"护栏判决如下（未下发）"——
    按钮名叫执行、实际什么都没执行（用户实测："我点击执行，为什么提示未下发？符合预期吗？"）。
    现在：按钮文案随执行方式变；护栏已拒绝时按钮禁用并把理由放进 tooltip。
    """
    _paths()
    js = (PAGES_DIR / "services.js").read_text(encoding="utf-8")
    assert "查看判决（不下发）" in js, "「仅建议」下按钮必须叫查看判决，不能叫执行"
    assert "执行（白名单自动）" in js and "执行（需确认）" in js, "按钮文案必须随执行方式变化"
    assert "goBtn.disabled = m !== \"suggest\" && v.allow === false" in js, \
        "护栏已拒绝时必须禁用按钮（不让人点了再看模糊提示）"
    assert "仅建议（未下发）" in js and "要真正执行，请把「执行方式」改为" in js, \
        "仅建议下的提示必须说清「没有下发」以及「要怎么才能下发」"


# ── ⑧ 悬浮问号（helpTip）：给"专业但不好懂"的控件补说明 ──
def test_helptip_primitive_and_layout_safety():
    """用户要求：专业控件加悬浮问号说明，且**布局合理不遮挡**、删除这类自明操作不加。

    这里锁四件事：① 原语存在且导出；② 气泡是 fixed + 默认 display:none + z-index 高于抽屉
    （901）——不然会被抽屉/表格裁切或挡住内容；③ 位置由 JS 夹紧视口并自动上下翻转；
    ④ 关键控件确实挂了说明（防止后人清掉），而**删除/关闭/刷新这类自明操作不挂**。
    """
    _paths()
    static = PAGES_DIR.parent.parent          # static/
    ui = (UI_JS).read_text(encoding="utf-8")
    assert "export function helpTip(" in ui, "缺 helpTip 原语"
    assert "getBoundingClientRect" in ui and "window.innerHeight" in ui, \
        "helpTip 必须按视口夹紧并自动翻转（否则贴边/底部会被裁切）"

    css = (static / "ops.css").read_text(encoding="utf-8")
    assert ".htip-bubble" in css and "display: none" in css, "气泡必须默认隐藏（静态不占位、不遮挡）"
    assert "position: fixed" in css, "气泡必须 fixed 定位（否则在抽屉/表格的 overflow 里被裁切）"
    m = re.search(r"\.htip-bubble\s*\{[^}]*z-index:\s*(\d+)", css, re.S)
    assert m and int(m.group(1)) >= 1000, "气泡层级必须高于抽屉（901），否则会被抽屉盖住"

    # 关键控件挂了说明
    must_have = {
        "services.js": ["执行方式", "日志路径", "systemd 单元", "全局熔断"],
        "rules.js": ["检测算法", "持续时长", "重发步长"],
        "runbooks.js": ["执行方式", "回滚方式"],
        "settings.js": ["主模型", "快模型", "Embedding"],
        "hosts.js": ["目标机系统", "服务探活", "安装令牌"],
        "skills.js": ["优先级", "连接检查"],
        "inspections.js": ["cron 表达式"],
    }
    for fname, keys in must_have.items():
        src = (PAGES_DIR / fname).read_text(encoding="utf-8")
        for k in keys:
            assert "helpTip(" in src, f"{fname} 没有任何 helpTip"
            assert f'"{k}"' in src or f'"{k}（' in src or k in src, f"{fname} 缺控件文案 {k}"

    # 自明操作不该挂说明（问号泛滥就是噪音）
    for fname in ("hosts.js", "rules.js", "settings.js"):
        src = (PAGES_DIR / fname).read_text(encoding="utf-8")
        for obvious in ('"删除", helpTip', '"关闭", helpTip', '"刷新", helpTip'):
            assert obvious not in src, f"{fname} 给自明操作加了说明：{obvious}"


# ── ⑨ "幽灵类名"守卫：JS 里用到的类名必须在 CSS 里有定义（用户实测：案例库 KPI 无背景）──
def test_no_phantom_css_classes_in_frontend():
    """前端用到的 class 名必须在**平台样式表**里有定义（或属于白名单/JS 选择器钩子）。

    背景（用户实测截图点名）：案例库页自己拼了 `.kpi / .kpi-value / .kpi-label` 三个类，
    而这三个类**在 CSS 里根本不存在** → 渲染成一坨没有背景的裸文字，"空洞、突兀"。
    根因是同一类数字卡在两个页面各写了一套（服务监控有样式、案例库没有）。
    修法：KPI 行提到 ui.js 的 `kpiRow()`（样式只有一处），并加本守卫防止再有人自造类名。

    D130 扩容：扫描范围从 `ops.css` 一个文件扩到**全部 `ops*.css`**——
    《前端风格规范》§9 早就允许「ops.css（或 ops-*.css 按页拆分）」，
    守卫若只认 ops.css，新增的 ops-*.css 里定义的类会被误报成"幽灵类名"。
    """
    import pathlib
    import re

    static = pathlib.Path(__file__).resolve().parent.parent.parent / "static"
    css_files = sorted(static.glob("ops*.css"))      # ops.css + ops-*.css（按页拆分）
    assert css_files, "找不到平台样式表（ops*.css）"
    defined: set[str] = set()
    for cf in css_files:
        text = re.sub(r"/\*.*?\*/", "", cf.read_text(encoding="utf-8"), flags=re.S)
        defined |= set(re.findall(r"\.([A-Za-z][\w-]*)", text))

    used: dict[str, set[str]] = {}
    for f in sorted(static.rglob("*.js")) + [static / "index.html"]:
        if "vendor" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'class:\s*"([^"]+)"|class="([^"]+)"', text):
            for name in (m.group(1) or m.group(2)).split():
                if re.fullmatch(r"[a-z][\w-]*", name):
                    used.setdefault(name, set()).add(f.name)

    # 白名单：① 工具/状态类（无样式也合法）；② JS 选择器钩子或语义标记（靠父级样式）
    # ②里的既有条目：`.picked-mark`（copilot.js）——它就是**纯钩子**：勾选态的颜色/字重
    #   由内联 style 给（`color:var(--accent);font-weight:600`，令牌合规），不需要 CSS 定义。
    WHITE = {"mono", "muted", "note", "card", "btn", "ghost", "secondary", "danger", "sm", "big",
             "active", "empty", "table", "md-body", "cite", "unit", "open", "on", "skip", "pass",
             "warn", "fail", "accent", "row-actions", "clickable", "loading", "hidden", "primary",
             "no-fixes-tip", "m-crumb-doc", "m-crumb-sec", "chat-cmp", "chat-ctx", "chat-ctx-tag",
             "chat-folded", "picked-mark"}
    missing = {k: sorted(v) for k, v in used.items() if k not in defined and k not in WHITE}
    assert not missing, ("以下 class 在 CSS 里没有定义（会渲染成没有样式的裸元素，"
                         "要么补样式、要么改用 ui.js 的原语）：\n"
                         + "\n".join(f"  .{k} ← {', '.join(v)}" for k, v in sorted(missing.items())))


def test_kpi_row_is_shared_primitive():
    """KPI 行必须是 ui.js 的**共用原语**（两页各写一套就会再次出现"一个有样式一个没有"）。"""
    _paths()
    ui = UI_JS.read_text(encoding="utf-8")
    assert "export function kpiRow(" in ui, "缺共用 KPI 行原语"
    css = (PAGES_DIR.parent.parent / "ops.css").read_text(encoding="utf-8")
    for cls in (".kpi-row", ".kpi-item", ".kpi-v", ".kpi-l"):
        assert cls in css, f"CSS 缺 {cls}（KPI 行会没有背景）"
    for f in ("services.js", "cases.js"):
        src = (PAGES_DIR / f).read_text(encoding="utf-8")
        assert "kpiRow(" in src, f"{f} 未使用共用原语"
        assert re.search(r'class: `kpi |class: "kpi"', src) is None, f"{f} 仍在自造 .kpi 类名"


# ── ⑩ 前端不得把 JSON 字段当数组/对象直接用（用户实测：查看/编辑点了没反应）──
def test_frontend_does_not_treat_json_column_as_array_blindly():
    """raw SQL 查出来的 JSON 列是**字符串**，前端直接用 `.join/.length/...` 会抛异常。

    实测报障：案例库「查看 / 编辑」点了没反应——`tags.join(" / ")` 抛 TypeError，
    onclick 里的异常不会弹提示，用户只看到"按钮坏了"。
    修法：接口层保证 tags 永远是数组（`_as_list`），前端再兜一层 `tagsArr()`；本守卫防回退。
    """
    _paths()
    rb = (PAGES_DIR.parent.parent.parent / "backend" / "app" / "routers" / "runbooks.py").read_text(encoding="utf-8")
    assert "def _as_list(" in rb and '"tags": _as_list(' in rb, "接口层必须把 tags 归一成数组"
    js = (PAGES_DIR / "cases.js").read_text(encoding="utf-8")
    assert "function tagsArr(" in js, "前端缺 tags 容错解析"
    assert "r.tags.join(" not in js and "(r?.tags || []).join(" not in js, \
        "前端仍在直接对 tags 调 .join（JSON 列可能是字符串 → 按钮会静默失效）"


# ── ⑤ D86：页面文件用了 ui.js 的助手却漏导入（busy is not defined 一类的静默失效）──
def test_page_modules_import_every_ui_helper_they_use():
    """每个 pages/*.js 用到的 ui.js **具名导出**，必须都在自己的 import 语句里出现过。

    背景（D86，实测确认）：copilot.js 用的 `busy()` 没导入——侧栏删除会话必报
    `busy is not defined`、弹"删除失败"且会话删不掉；而同一页其他 handler 又正常，
    是"静默不生效"里最难察觉的一类。**新页面加按钮时，只要用了 ui.js 的新助手，
    漏 import 就会被这个测试当场拦住。**
    """
    export_names = []
    for line in UI_JS.read_text(encoding="utf-8").splitlines():
        m = re.search(r"export (?:async )?function (\w+)|export \{[^}]*\b(\w+)\b", line)
        if m:
            export_names.append(m.group(1) or m.group(2))
    export_names = {n for n in export_names if n}
    # 再加 UI_JS 顶部 / 靠 export 语句暴露的常见动词（保险：捕获非 function 的具名导出）
    export_names |= set(re.findall(r"export\s*\{([^}]+)\}", UI_JS.read_text(encoding="utf-8")) and
                        [w.strip() for w in re.findall(r"export\s*\{([^}]*)\}", UI_JS.read_text(encoding="utf-8"))[0].split(",") if w.strip()])

    used, imported = set(), set()
    for f in sorted(PAGES_DIR.glob("*.js")):
        src = f.read_text(encoding="utf-8")
        imp = re.search(r'from "\.\./ui\.js";', src)
        if imp:
            head = src[:imp.start()]
            imported |= {w for w in re.findall(r"\{\s*([^}]+?)\s*\}", head)
                         for w in re.split(r"[,\s]+", w) if w}
        # 词边界匹配"裸调用"，避免把字符串/注释里的撞进来
        for n in export_names:
            if re.search(rf"(?<![\w.$]){re.escape(n)}\s*\(", src):
                used.add(n)
    missing = sorted(used - imported)
    assert not missing, f"以下 ui.js 助手被使用却未 import（D86 同款，会静默失效）：{missing}"


# ── ⑥ D87：对话上下文管理接线守卫 ──
def _backend_routers() -> str:
    import pathlib
    return pathlib.Path(__file__).resolve().parent.parent.parent / "backend" / "app" / "routers" / "incidents.py"


def test_chat_stream_uses_context_management():
    """chat_stream 必须走 chat_context（滚动摘要 + 窗口），而不是把整段历史上送。

    D87 背景：改前 `history = [system] + msgs`（整段）。长会话 prompt 线性增长 → 费钱、变慢、
    最终撞上下文上限。若有人"简化"回原样，这个守卫会拦住。
    """
    src = (_backend_routers()).read_text(encoding="utf-8")
    assert "from app.services import chat_context as ctx" in src, "chat_stream 未引入 chat_context"
    assert "ctx.build_history(" in src, "chat_stream 未用 build_history 拼上下文"
    assert "ctx.maybe_compact(" in src, "chat_stream 未做阈值压缩"
    # 不允许回到"整段上送"的老写法（build_history 的 system 只能出现一次，由它统一注入）
    assert not re.search(r"history\s*=\s*\[\{\"role\": \"system\", \"content\": prompts\.CHAT_PROMPT\}\] \+ msgs", src)


def test_chat_context_rolls_summary_incrementally_not_full_resend():
    """摘要必须是**增量**合并；同时禁止把"展示用的全量历史"整段发给模型（D87 泄漏风险）。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent.parent /
           "backend" / "app" / "services" / "chat_context.py").read_text(encoding="utf-8")
    assert 'json.dumps({"已有摘要": summary, "需要并入的对话": seg}' in src
    assert "messages[kept:]" in src, "build_history 必须只取最近窗口"
