"""D113 守卫：人工订正闭环 + 待我处理 + 主动通知。

覆盖三件事：
  ① 订正/查询/待办三个路由已注册（防"后端写了前端没接"或反之）
  ② 前端存在订正入口与待我处理卡（静态检查，防白版式漏接）
  ③ notify_attention 可用（会喊人）
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_routes_registered():
    """路由注册静态检查（不 import app.main：测试环境未装 apprise 等运行期依赖）。"""
    inc = (ROOT / "backend/app/routers/incidents.py").read_text(encoding="utf-8")
    assert '@router.post("/incidents/{iid}/correct")' in inc
    assert '@router.get("/incidents/{iid}/corrections")' in inc
    col = (ROOT / "backend/app/routers/collect.py").read_text(encoding="utf-8")
    assert '@router.get("/todo-counts")' in col
    # 公网真机教训：rca_findings 的置信度列名是 self_assessed_strength（枚举小写），
    # 不是 confidence——写错列名只会在真实 MySQL 上报 1054，静态守卫必须拦住。
    assert "f.self_assessed_strength='inconclusive'" in col
    assert "f.confidence" not in col
    # 诊断结论 INCONCLUSIVE 也必须主动喊人（不只是被动计数）
    rca = (ROOT / "backend/app/services/rca.py").read_text(encoding="utf-8")
    assert 'diagnosis_needs_human' in rca and "notify_attention" in rca
    # 订正弹窗必须是全宽纵向表单：塞进 detail-meta 两列会让输入框只剩半宽；
    # 全局 .vs-input 的 38px 行高也不能作用于 textarea（会被压成一行）。
    inc_src = (ROOT / "static/js/pages/incidents.js").read_text(encoding="utf-8")
    assert 'class: "corr-form"' in inc_src and 'class: "vs-input corr-main"' in inc_src
    css = (ROOT / "static/ops.css").read_text(encoding="utf-8")
    assert "textarea.vs-input:not(.chat-input)" in css
    assert ".corr-field textarea.corr-main" in css
    # 订正弹窗必须默认展示 AI 原结论（否则用户不知道"订正什么"）
    assert 'class: "corr-ai"' in inc_src and "AI 原结论" in inc_src
    assert "aiFixBullets" in inc_src and "填入根因框" in inc_src
    assert ".corr-ai-text" in css and "corr-ai-fix-n" in css
    # 纯文本化不能把 bad_var_xyz 这类技术标识符里的下划线也剥掉
    assert "[`*_#>]" not in inc_src and "aiPlainText" in inc_src
    # 诊断完成后必须回填 inc.has_report/has_fixes，否则「生成变更单」误报未诊断
    assert "onclick: () => runDiag(inc)" in inc_src
    assert "inc.has_report = Boolean(full.ai_report_md)" in inc_src
    assert "syncGenBtn(inc.has_fixes)" in inc_src
    # 订正字段必须有统一长度上限，且前端 maxlength/label 关联齐全
    inc_api = (ROOT / "backend/app/routers/incidents.py").read_text(encoding="utf-8")
    assert "_CORR_NOTE_MAX" in inc_api and "_CORR_RES_MAX" in inc_api
    assert 'maxlength: "4000"' in inc_src and 'maxlength: "500"' in inc_src
    assert 'id: "corr-root"' in inc_src and 'for: input.id' in inc_src
    assert "corr-count" in inc_src and "粘贴内容" in inc_src
    # 飞书通知：卡片构建 + 传输统一入口 + 测试卡
    nf = (ROOT / "backend/app/services/notify_format.py").read_text(encoding="utf-8")
    assert "describe_alert" in nf and "describe_attention" in nf and "describe_test" in nf
    assert "incidents/alert/" in nf
    inc_js = (ROOT / "static/js/pages/incidents.js").read_text(encoding="utf-8")
    rb_js2 = (ROOT / "static/js/pages/runbooks.js").read_text(encoding="utf-8")
    assert "by-alert" in inc_js and "incidents/alert" not in inc_js  # 路由由 sub 解析
    assert "await detail(Number(sub))" in rb_js2
    nt = (ROOT / "backend/app/services/notify_transport.py").read_text(encoding="utf-8")
    assert "send_feishu_card" in nt and '"msg_type": "interactive"' in nt and "send_channel" in nt
    notify = (ROOT / "backend/app/services/notify.py").read_text(encoding="utf-8")
    assert "send_channel" in notify and "describe_alert" in notify
    alerts = (ROOT / "backend/app/routers/alerts.py").read_text(encoding="utf-8")
    assert "describe_test" in alerts and "send_channel" in alerts
    # 结构化动作必须先校验确认凭据再下发 Agent（全流程演练发现的 HITL 缺口）
    rb_svc = (ROOT / "backend/app/services/runbook.py").read_text(encoding="utf-8")
    a = rb_svc.index('if step.get("action"):')
    c = rb_svc.index("_consume_confirm(db, runbook_id, step_idx, confirm_token)", a)
    e = rb_svc.index("enqueue_and_wait", a)
    assert c < e, "action 分支必须先 _consume_confirm 再 enqueue_and_wait"
    rb_js = (ROOT / "static/js/pages/runbooks.js").read_text(encoding="utf-8")
    assert 'executed_via === "agent"' in rb_js and "Agent 任务通道" in rb_js
    assert "showAgentResult" in rb_js
    # 执行后先 draw() 再展示结果；不能再 detail(rid) 覆盖结果抽屉
    h = rb_js.index('if (r.executed_via === "agent")')
    assert "detail(rid)" not in rb_js[h:h+260], "结果抽屉被 detail(rid) 立刻覆盖"
    # 「需人工」提醒必须真的去重（文档承诺，不能只是算哈希）
    notify_src = (ROOT / "backend/app/services/notify.py").read_text(encoding="utf-8")
    assert "ATTENTION_DEDUPE_MIN" in notify_src and "跳过重复投递" in notify_src
    assert "max-height: 168px" not in css   # 禁止再出现会裁半行的内滚参考区
    # 「今日」必须按北京日界（UTC 零点会在北京时间 08:00 才换日）
    assert "beijing_day_start_utc" in col
    from app.services.daytime import beijing_day_start_utc as bj_start
    from datetime import datetime
    # 北京 2026-10-18 07:30 == UTC 10-17 23:30 → 日界应为 UTC 10-17 16:00
    assert bj_start(datetime(2026, 10, 17, 23, 30)) == datetime(2026, 10, 17, 16, 0)
    # 北京 2026-10-17 23:59 == UTC 10-17 15:59 → 日界应为 UTC 10-16 16:00
    assert bj_start(datetime(2026, 10, 17, 15, 59)) == datetime(2026, 10, 16, 16, 0)


def test_frontend_has_correction_entry_and_todo_card():
    inc = (ROOT / "static/js/pages/incidents.js").read_text(encoding="utf-8")
    assert "/correct`" in inc and "drawCorrections" in inc and "订正结论" in inc
    dash = (ROOT / "static/js/pages/dashboard.js").read_text(encoding="utf-8")
    assert "todo-counts" in dash and "待我处理" in dash
    css = (ROOT / "static/ops.css").read_text(encoding="utf-8")
    assert ".todo-item" in css


def test_notify_attention_available():
    from app.services.notify import notify_attention
    assert callable(notify_attention)
