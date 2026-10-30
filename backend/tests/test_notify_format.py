"""飞书通知格式纯函数测试（不依赖 apprise/DB/网络）。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services import notify_format as nf


def _ev(**kw):
    now = datetime(2026, 10, 18, 0, 40, 0)
    base = dict(hash="a" * 32, rule_id=4, rule_name="演示-探活失败",
                host_ident="3b404a5ec42d5e31414c77188605846d", metric="业务端口",
                severity=1, trigger_value=1.0, labels={}, tags={}, is_recovered=False,
                first_trigger_time=now - timedelta(minutes=3), trigger_time=now, recover_time=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_alert_card_fields_and_no_hash_noise():
    ev = _ev(tags={"probe_type": "tcp", "target": "127.0.0.1:80", "probe_status": "down"})
    host = {"name": "demo-web-01", "ip": "10.0.1.11"}
    title, text, card = nf.describe_alert(
        ev, {"name": "演示-探活失败", "expr": ">0", "runbook_url": ""}, host,
        base_url="http://platform.example.com:8848", now=datetime(2026, 10, 18, 0, 40, 0))
    flat = str(card)
    assert card["header"]["template"] == "red"
    assert title.startswith("[S1 紧急]") and "业务端口" in title
    assert "demo-web-01" in flat and "10.0.1.11" in flat
    assert "down（探测失败）" in flat
    assert "3b404a5ec42d5e31414c77188605846d" not in flat   # 完整指纹不进正文
    assert "runbook: -" not in flat and "runbook" not in flat
    assert "查看告警" in flat and "http://platform.example.com:8848/#/alerts" in flat
    assert "http://platform.example.com:8848/#/incidents/alert/" + ev.hash in flat
    assert "3 分钟" in flat


def test_recovery_card_green_and_duration():
    start = datetime(2026, 10, 18, 0, 38, 0)
    ev = _ev(is_recovered=True, first_trigger_time=start,
             recover_time=start + timedelta(minutes=16), trigger_value=0.0,
             tags={"probe_type": "tcp", "target": "127.0.0.1:80", "probe_status": "up"})
    title, text, card = nf.describe_alert(
        ev, {"name": "演示-探活失败", "expr": ">0"}, {"name": "demo-web-01", "ip": ""},
        base_url="", now=start)
    flat = str(card)
    assert card["header"]["template"] == "green"
    assert title.startswith("[已恢复]")
    assert "up（探测正常）" in flat
    assert "16 分钟" in flat
    assert '"tag": "action"' not in flat   # 没配 base_url 不出按钮（避免死链）


def test_attention_runbook_card():
    host = {"name": "vm-0-13-centos-8954", "ip": ""}
    title, text, card = nf.describe_attention(
        "hitl_pending", "变更单 #22 待确认",
        "变更单 #22 已生成 1 个可执行步骤，等待确认后再由 Agent 执行。",
        host, meta={"runbook_id": 22, "incident_id": 19, "host": "3b" * 32,
                    "steps": 1, "risk": "mid", "actions": ["nginx_fix_bad_conf"]},
        base_url="http://platform.example.com:8848")
    flat = str(card)
    assert card["header"]["template"] == "orange"
    assert "[需人工]" in title
    assert "#22" in flat and "#19" in flat and "nginx_fix_bad_conf" in flat
    assert "打开变更单" in flat and "/#/runbooks/22" in flat and "/#/incidents/19" in flat
    assert "task_id" in flat


def test_attention_diagnosis_card():
    title, text, card = nf.describe_attention(
        "diagnosis_needs_human", "事件 #19 诊断证据不足",
        "AI 诊断结论为 INCONCLUSIVE，需要补充真实根因。", {"name": "demo-web-01", "ip": ""},
        meta={"incident_id": 19, "confidence": "INCONCLUSIVE"}, base_url="http://x")
    flat = str(card)
    assert card["header"]["template"] == "blue"
    assert "INCONCLUSIVE" in flat and "去订正" in flat and "/#/incidents/19" in flat


def test_test_card_and_escaping():
    title, text, card = nf.describe_test("飞书-运维告警群", base_url="http://x")
    flat = str(card)
    assert card["header"]["template"] == "blue"
    assert "测试" in title and "飞书-运维告警群" in flat and "打开平台" in flat
    ev = _ev(rule_name='<script>alert(1)</script>', host_ident="a" * 32)
    _, _, card2 = nf.describe_alert(ev, {"name": '<script>x</script>', "expr": ">0"},
                                    {"name": "<b>host</b>", "ip": ""}, base_url="")
    flat2 = str(card2)
    assert "<script>" not in flat2 and "&lt;script&gt;" in flat2
    assert "<b>host</b>" not in flat2


def test_value_formats():
    ev = _ev(metric="cpu.percent", severity=2, trigger_value=92.4, tags={})
    _, _, card = nf.describe_alert(ev, {"name": "演示-CPU高负载", "expr": ">85"},
                                   {"name": "demo-web-01", "ip": ""}, base_url="")
    flat = str(card)
    assert "92.4%" in flat and "阈值 >85%" in flat
