"""D175（用户验收反馈，2026-10-23）：五处"能跑但看不懂/找不到/消失太快"。

用户原话锚定：
  ① "已经出结论了，但还提示诊断未产出有效结论（生成失败）" —— #2000065 报告
     strong/CONFIRMED 且 10 步证据链完整，分诊条却判"生成失败"；
  ② "提示弹窗消失的太快了，起码等待6s"；
  ③ "近 7 日 token 用量（按 purpose 分账），按 purpose 是啥意思？？不要有英文描述"；
  ④ "演示模式的提示删掉"（内部实现细节不该给最终用户看）；
  ⑤ "无法看到是啥问题啊，问题特征，最关键的是啥，没有体现"（问题看板/告警中心）
     + "这里的执行诊断。入口在哪？找得不到执行诊断按钮啊"（悬空引用）。

守卫策略：**静态守卫锁代码形态**（能红在"下次又写错"的那一刻），配合 e2e 锁真实渲染。
"""
from __future__ import annotations

import re

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去注释后的 JS 代码（守卫只该看代码，不该被注释里提到的旧写法误伤）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//")[0] for line in src.splitlines())


# ── ① 分诊条必须按「单条诊断对象」取数据，且失败判定基于"有无结论正文" ──────────────
def test_triage_reads_single_finding_not_array():
    inc = _read(STATIC / "js" / "pages" / "incidents.js")
    tri = inc[inc.index("function renderTriage"):inc.index("function renderReport")]
    code = _code_only(tri)
    # /findings 端点返回单条对象（有 triage / finding_id / findings_md 顶层字段），
    # 读 f.findings 数组 = 恒空 = 恒判失败（D175-1 现场）
    assert re.search(r"f\.findings\b", code) is None, \
        "renderTriage 不得读 f.findings（端点返回单条对象，不是数组）"
    assert "f.triage" in code and "f.finding_id" in code, "应直接读单条对象的 triage/finding_id"
    # 失败判定必须有"有结论正文"的成分，不能只看 status 字段
    assert "hasConclusion" in code, "失败判定应基于报告正文（hasConclusion），而非仅 status 字段"


# ── ② toast 停留时长 ≥6s ────────────────────────────────────────────────────────
def test_toast_lives_at_least_six_seconds():
    ui = _code_only(_read(STATIC / "js" / "ui.js"))
    seg = ui[ui.index("export function toast"):ui.index("export function", ui.index("export function toast") + 10)]
    m = re.search(r"Math\.max\((\d{4,6})", seg)
    assert m, "toast 应有 Math.max 下限（毫秒）"
    assert int(m.group(1)) >= 6000, f"toast 停留时长下限 {m.group(1)}ms < 6s（用户明确要求起码 6s）"


# ── ③④ 设置页不得出现英文内部术语 / 内部实现提示 ─────────────────────────────────
def test_settings_no_internal_jargon_in_user_facing_strings():
    src = _read(STATIC / "js" / "pages" / "settings.js")
    code = _code_only(src)
    for bad in ("按 purpose", "purpose 分账", "LLM_DAILY_TOKEN_LIMIT", "DEMO_ENABLED",
                "seed_demo", ".env 的 LLM_", "AES-GCM"):
        assert bad not in code, f"设置页用户可见文案含内部术语：{bad}"
    # 用途枚举必须有中文映射（且覆盖后端全部 7 个 purpose 枚举值）
    assert "PURPOSE_ZH" in code
    for k in ("rca", "chat", "summary", "inspect", "fixcheck", "embed", "suggest"):
        assert re.search(rf'\b{k}:\s*"', code), f"PURPOSE_ZH 缺枚举 {k} 的中文映射"
    # 标题不得再出现英文术语
    assert "按 purpose 分账" not in code


# ── ⑤⑥ 列表必须给"问题特征"：事件卡片给结论摘要、告警列表给指标中文名+AI 判断 ────────
def test_issue_and_alert_lists_surface_the_problem():
    inc = _read(STATIC / "js" / "pages" / "incidents.js")
    assert "root_cause_head" in inc, "问题看板卡片应展示 AI 结论一句话摘要（root_cause_head）"
    alerts = _read(STATIC / "js" / "pages" / "alerts.js")
    assert "metric_label" in alerts, "告警列表应优先展示指标中文名（metric_label）"
    assert "ai_summary" in alerts.split("function detail")[0], "告警列表应有 AI 判断列"

    # 后端：列表 payload 必须带这些字段
    be = _read(ROOT / "backend" / "app" / "routers" / "incidents.py")
    assert "root_cause_head" in be and "_root_cause_head" in be
    be_alerts = _read(ROOT / "backend" / "app" / "routers" / "alerts.py")
    assert "metric_label" in be_alerts


# ── ⑦ 文案里引用的入口必须真实存在（悬空引用守卫）────────────────────────────────
def test_diagnosis_entry_exists_where_copy_references_it():
    alerts = _read(STATIC / "js" / "pages" / "alerts.js")
    # 详情文案说"可点「执行诊断」" → 页面必须有同名按钮
    assert '执行诊断（AI）' in _code_only(alerts), "告警详情必须有「执行诊断（AI）」按钮（文案引用了它）"
    # 深链路径与 incidents 的解析一致
    assert "#/incidents/alert/" in alerts
    inc = _read(STATIC / "js" / "pages" / "incidents.js")
    assert 'deep.startsWith("alert/")' in inc, "问题看板必须解析 alert/ 深链（否则告警页跳过去落空）"


# ── 举一反三：全局不再把环境变量名/内部脚本路径露给用户 ─────────────────────────────
def test_no_env_names_or_script_paths_in_user_facing_strings():
    for name in ("DEMO_ENABLED", "DEMO_CHAT_ENABLED", "LLM_DAILY_TOKEN_LIMIT",
                 "backend/scripts/seed_demo.py"):
        for f in (STATIC / "js" / "pages").glob("*.js"):
            code = _code_only(_read(f))
            assert name not in code, f"{f.name} 用户可见代码含内部实现细节：{name}"


# ── ⑧ response_model 白名单：新增响应字段必须同步 Pydantic schema（D175-5 现场）────────
@pytest.mark.asyncio
async def test_incident_list_root_cause_head_survives_response_model():
    """response_model 是白名单：_inc_summary 加了字段但漏改 IncidentItem → Pydantic
    **静默丢字段**（不报错、200 照常）。D175-5 上线后就是栽在这里——容器里源码正确、
    单测里 dict 正确，唯独线上接口没有该字段。守卫：列表响应必须真有 root_cause_head 键。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        r = await ac.get("/api/ops/incidents", params={"page": 1, "page_size": 5}, headers=hdr)
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        assert "root_cause_head" in items[0], (
            "root_cause_head 被 response_model 丢了——_inc_summary 与 IncidentItem 必须两处同步")
        # 成员告警的新字段同样不能被丢
        with_members = [x for x in items if x.get("active_services")]
        if with_members:
            svc = with_members[0]["active_services"][0]
            assert "metric_label" in svc, "active_services.metric_label 被 response_model 丢了"


def test_issue_card_shows_metric_label_with_reading():
    """卡片上的指标标签必须**带读数**（"磁盘 data 分区使用率 91.0"）。

    回归锁一个真实的前端 bug：`s.metric_label || s.metric + (读数)`
    因运算符优先级被解析成 `s.metric_label || (s.metric + 读数)`——
    标签存在时读数被整段吞掉，界面只剩指标名、看不出"到底多高"。
    """
    src = (STATIC / "js" / "pages" / "incidents.js").read_text(encoding="utf-8")
    code = _code_only(src)
    assert "metric_label" in code
    # 标签与读数必须是**并列**拼接（标签在括号里），不能是 `||` 右侧加法
    bad = re.search(r"metric_label\s*\|\|\s*s\.metric\s*\n?\s*\+", code)
    assert bad is None, (
        "metric_label || s.metric + 读数 会被解析成 metric_label || (s.metric + 读数)——"
        "标签存在时读数被吞掉（真实 bug，公网截图发现）")
    assert re.search(r"\(s\.metric_label\s*\|\|\s*s\.metric[^)]*\)\s*\+", code), \
        "标签与读数应并列拼接"
