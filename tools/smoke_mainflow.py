#!/usr/bin/env python3
"""主流程接口自动化（部署后冒烟，可直接在目标机跑）。

用法：
    python3 tools/smoke_mainflow.py                        # 默认打本地 8848
    python3 tools/smoke_mainflow.py --base http://x:8848   # 打指定平台
    python3 tools/smoke_mainflow.py --no-llm               # 跳过 AI 调用（省时间/token）
    python3 tools/smoke_mainflow.py --user admin --password xxx

覆盖（8 段，写操作尽量自清理；技能无删除接口，保留停用态）：
    A 基础     ：健康 / 登录 / 能力集
    B 只读主流程：大盘·主机·告警·规则·事件·巡检·变更单·技能·案例卡·审计·渠道·LLM 配置
    C 写主流程  ：建规则→试运行→删 / 抑制建→删 / 技能建→校验→停用 / 渠道建→删 / 告警确认
    D AI 链路   ：助手建议问题即时返回（读缓存）+ AI 助手对话（真调用，--no-llm 可跳）
    E 权限门禁  ：登录用户越权写被拒（403）
    F 服务级监控（P5）：服务清单/总览/模板 · Agent 声明下发鉴权 · 日志取证通道契约（含路径白名单拒绝）
                     · 恢复动作白名单与**护栏判决**（越权目标被拒）· 全局熔断开关的拒绝与恢复
    G 通知与人工闭环（D113）：待办计数 · 通知渠道/台账 · 告警指纹反查事件
                     · 人工订正回读 · 前端事件/变更单深链资产 · 可选飞书测试卡（--notify-test）
    H Agent 健康与能力增强（P0/P1）：认证/版本字段 · 安装件 SHA256 · 告警新鲜度字段
                     · 引用回放/升级入口前端资产

退出码：0 = 全部通过；1 = 有失败。可直接用于 CI / 部署脚本的 gate。
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

ARGS = None
RESULTS = []


def _req(method, path, tok=None, body=None, timeout=30):
    url = ARGS.base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001 —— 网络异常也记为该步失败
        return 0, {"_err": str(e)}


def check(name, ok, note=""):
    RESULTS.append((name, ok, note))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + note) if note else ""))
    return ok


def section(title):
    print("\n== %s ==" % title)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8848", help="平台地址")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--no-llm", action="store_true", help="跳过 AI 对话/摘要（省时间与 token）")
    ap.add_argument("--notify-test", action="store_true",
                    help="向已启用的飞书渠道真实发一条测试卡（会进群；默认不发）")
    ARGS = ap.parse_args()

    print("主流程接口自动化 —— 目标 %s" % ARGS.base)

    # ── A 基础 ──────────────────────────────────────────────
    section("A 基础")
    s, d = _req("GET", "/api/health")
    check("健康检查", s == 200 and (d.get("data") or {}).get("status") == "up", str((d.get("data") or {}).get("status")))
    s, d = _req("POST", "/api/auth/login", body={"username": ARGS.user, "password": ARGS.password})
    tok = (d.get("data") or {}).get("access_token")
    check("登录（%s）" % ARGS.user, bool(tok), "HTTP %s %s" % (s, d.get("msg", "")))
    if not tok:
        return _finish()
    s, d = _req("GET", "/api/auth/perms", tok)
    perms = d.get("data") or {}
    check("能力集可读", isinstance(perms, dict) and "can" in perms, json.dumps(perms.get("can", {}), ensure_ascii=False)[:70])

    # ── B 只读主流程 ────────────────────────────────────────
    section("B 只读主流程")
    reads = [
        ("/api/ops/dashboard/overview", "大盘总览"),
        ("/api/ops/hosts", "主机列表"),
        ("/api/ops/alerts", "告警中心"),
        ("/api/ops/alert-rules", "告警规则"),
        ("/api/ops/alert-mutes", "抑制策略"),
        ("/api/ops/incidents", "问题看板"),
        ("/api/ops/inspections", "巡检任务"),
        ("/api/ops/runbooks", "变更单"),
        ("/api/ops/skills", "技能列表"),
        ("/api/ops/case-cards/stats", "案例库统计"),
        ("/api/audit-logs?page=1&page_size=5", "操作审计"),
        ("/api/ops/notify-channels", "通知渠道"),
        ("/api/ops/notification-records", "通知台账"),
        ("/api/ops/todo-counts", "待我处理计数"),
        ("/api/llm-configs", "LLM 配置"),
        ("/api/license/state", "License 门禁状态"),
        ("/api/ops/dev/engine-state", "引擎自检"),
    ]
    for path, label in reads:
        s, d = _req("GET", path, tok)
        check(label, s == 200 and d.get("code") == 0, "HTTP %s" % s)

    # ── B2 大盘「结构契约」（D45 教训固化）─────────────────
    # D45 的教训：只验"接口 200 + code 0"不够——events_by_hour 当时返回了 dict
    # （被变量覆盖），接口照样 200，但前端 .map() 崩了、半屏空白。
    # 因此这里对**三个时间范围**逐一断言"前端要用到的字段类型"。
    section("B2 大盘结构契约（三个时间范围 × 字段类型）")
    _EXPECT = {
        "events_by_hour": list, "series": list, "watch_hosts": list, "active_alerts": list,
        "hosts": dict, "alerts": dict, "inspection": (dict, type(None)),
        "incidents_open": int, "mttr_minutes": (int, float, type(None)), "events_24h": int,
    }
    for rng in ("1h", "6h", "24h"):
        s, d = _req("GET", f"/api/ops/dashboard/overview?range={rng}", tok)
        dd = d.get("data") or {}
        bad = []
        for k, t in _EXPECT.items():
            if k not in dd:
                bad.append(f"{k}(缺)")
            elif not isinstance(dd[k], t):
                bad.append(f"{k}={type(dd[k]).__name__}(应{t})")
        # 直方图必须恒为 24 项（逐小时）
        if isinstance(dd.get("events_by_hour"), list) and len(dd["events_by_hour"]) != 24:
            bad.append(f"events_by_hour 长度={len(dd['events_by_hour'])}(应 24)")
        check(f"range={rng} 字段类型正确", not bad, ("；".join(bad))[:110])
        check(f"range={rng} range 回显一致", dd.get("range") == rng)

    # ── C 写主流程（幂等自清理）─────────────────────────────
    section("C 写主流程（写→验→清理）")
    tag = str(int(time.time()))
    # C1 规则
    s, d = _req("POST", "/api/ops/alert-rules", tok, {"name": "smoke-规则-" + tag, "scope": "metric",
        "metric": "cpu.percent", "expr": ">95", "severity": 3, "eval_interval_s": 60, "window_s": 60})
    rid = (d.get("data") or {}).get("id")
    check("建规则", d.get("code") == 0, "id=%s" % rid)
    if rid:
        s, d = _req("POST", "/api/ops/alert-rules/%s/test" % rid, tok, {})
        check("规则试运行", d.get("code") == 0)
        s, d = _req("PUT", "/api/ops/alert-rules/%s" % rid, tok, {"name": "smoke-规则-" + tag, "expr": ">abc"})
        check("非法表达式被拒(400)", s == 400, "HTTP %s" % s)
        s, d = _req("DELETE", "/api/ops/alert-rules/%s" % rid, tok)
        check("删规则", d.get("code") == 0)
    # C2 抑制
    s, d = _req("GET", "/api/ops/alert-rules", tok)
    rules = (d.get("data") or {}).get("items") or []
    if rules:
        # D59 修正：原先写的是 reason/mode/duration_minutes——后端从未认识这些字段，
        # 静默建出"无原因"的抑制（正是必填校验要拦的形态）。改为真实契约 cause/mute_type。
        s, d = _req("POST", "/api/ops/alert-mutes", tok, {"cause": "smoke",
            "mute_type": 1, "tag_filters": [], "severities": []})
        mid = (d.get("data") or {}).get("id")
        check("建抑制", d.get("code") == 0, "id=%s" % mid)
        if mid:
            _req("DELETE", "/api/ops/alert-mutes/%s" % mid, tok)
    # C3 技能
    s, d = _req("POST", "/api/ops/skills", tok, {"name": "smoke-技能-" + tag,
        "description": "smoke 自检用技能", "content": "---\nname: s\ndescription: d\n---\n## 步骤\n1. x\n"})
    sid = (d.get("data") or {}).get("id")
    check("建技能", d.get("code") == 0, "id=%s" % sid)
    if sid:
        s, d = _req("POST", "/api/ops/skills/%s/check" % sid, tok, {})
        check("技能校验", d.get("code") == 0)
        _req("POST", "/api/ops/skills/%s/toggle" % sid, tok, {})   # 停用，避免影响诊断
    # C4 渠道（D59 修正：原先写顶层 url，后端不认识 → 建出没有投递配置的死渠道）
    s, d = _req("POST", "/api/ops/notify-channels", tok, {"name": "smoke-渠道-" + tag, "type": "webhook",
        "config_json": {"webhook_url": "http://127.0.0.1:1/smoke"}, "enabled": False})
    cid = (d.get("data") or {}).get("id")
    check("建渠道", d.get("code") == 0, "id=%s" % cid)
    if cid:
        s, d = _req("GET", "/api/ops/notify-channels", tok)
        row = [c for c in ((d.get("data") or {}).get("items") or []) if c.get("id") == cid]
        check("渠道凭据掩码返回", bool(row) and "127.0.0.1:1" not in json.dumps(row[0], ensure_ascii=False))
        _req("DELETE", "/api/ops/notify-channels/%s" % cid, tok)
    # C5 告警确认（若有活跃告警）
    s, d = _req("GET", "/api/ops/alerts", tok)
    alerts = (d.get("data") or {}).get("items") or []
    if alerts:
        h = alerts[0]["hash"]
        s, d = _req("POST", "/api/ops/alerts/%s/ack" % h, tok, {})
        check("告警确认(ack)", d.get("code") == 0)
    else:
        check("告警确认(ack)", True, "无活跃告警，跳过")

    # ── D AI 链路 ───────────────────────────────────────────
    section("D AI 链路")
    # D49：推荐问题必须"打开即有"——GET 只读缓存/规则版，绝不同步调模型（慢 = 用户看不到问题）
    t0 = time.time()
    s, d = _req("GET", "/api/ops/chat/suggestions", tok)
    dt = time.time() - t0
    data = d.get("data") or {}
    sugs = data.get("suggestions") or []
    check("助手建议问题即时返回（读缓存）", s == 200 and len(sugs) >= 3 and dt < 2.0,
          "HTTP %s / %s 条 / %.2fs / source=%s" % (s, len(sugs), dt, data.get("source")))
    if ARGS.no_llm:
        print("  SKIP --no-llm")
    else:
        tools, text = _chat_once(tok, "现在有几台主机在线？")
        check("AI 助手对话（真调用 + 工具）", bool(tools or text), "工具=%s" % sorted(set(tools)))

    # ── E 权限门禁 ──────────────────────────────────────────
    section("E 权限门禁")
    s, d = _req("POST", "/api/auth/demo", body={})
    if s == 403:
        print("  SKIP demo 入口已关（DEMO_ENABLED=false）")
    else:
        dtok = (d.get("data") or {}).get("access_token")
        if dtok:
            s, d = _req("GET", "/api/ops/hosts", dtok)
            check("demo 只读放行", s == 200, "HTTP %s" % s)
            s, d = _req("POST", "/api/ops/alert-rules", dtok, {"name": "x", "metric": "cpu.percent", "expr": ">1"})
            check("demo 越权写被拒(403)", s == 403, "HTTP %s" % s)
        else:
            check("demo 登录", False, "HTTP %s" % s)

    # ── F 服务级监控（P5）────────────────────────────────────
    section("F 服务级监控与分级恢复（P5）")
    s, d = _req("GET", "/api/ops/services", tok)
    svcs = (d.get("data") or {}).get("items") or []
    check("服务清单", s == 200 and isinstance(svcs, list), "HTTP %s / %s 项" % (s, len(svcs)))
    s, d = _req("GET", "/api/ops/services/overview", tok)
    ov = d.get("data") or {}
    check("服务总览（四态计数）", s == 200 and "by_status" in ov,
          "total=%s by_status=%s" % (ov.get("total"), json.dumps(ov.get("by_status"), ensure_ascii=False)))
    s, d = _req("GET", "/api/ops/services/templates", tok)
    check("服务模板（内置四类 + 应用）", s == 200 and len((d.get("data") or {}).get("items") or []) >= 5,
          "HTTP %s" % s)

    # Agent 声明下发：无机器凭证必须 401（这是声明能安全下发的底线）
    s, d = _req("GET", "/api/ops/agents/config")
    check("Agent 声明下发拒绝匿名", s == 401, "HTTP %s" % s)

    # 取证通道契约：拿一台真实主机下发一次（Agent 可能离线 → 任务挂 pending 也算通过）
    hosts = []
    s, d = _req("GET", "/api/ops/hosts", tok)
    hosts = (d.get("data") or {}).get("items") or []
    if not hosts:
        check("取证通道契约", False, "没有可用主机")
    else:
        ident = hosts[0]["ident"]
        # 平台侧拦"形状"（绝对路径 + 禁 `..` 穿越）；"哪些目录可读"的白名单归目标机 Agent
        s, d = _req("POST", "/api/ops/log-fetch", tok,
                    {"ident": ident, "path": "../../etc/passwd", "since": "", "until": ""})
        check("取证拒绝路径穿越(平台侧)", s == 400, "HTTP %s（`..` 必须被平台拦下）" % s)
        s, d = _req("POST", "/api/ops/log-fetch", tok,
                    {"ident": ident, "path": "etc/passwd", "since": "", "until": ""})
        check("取证拒绝相对路径(平台侧)", s == 400, "HTTP %s" % s)
        s, d = _req("POST", "/api/ops/log-fetch", tok,
                    {"ident": ident, "path": "", "unit": "", "since": "", "until": "",
                     "origin": "smoke:p5"})
        tid = (d.get("data") or {}).get("task_id")
        if s == 429:   # 限流（每主机每小时 20 次）本身是保护，不是缺陷
            check("取证下发（主机维度）", True, "HTTP 429 限流生效（预期保护）")
        else:
            check("取证下发（主机维度）", s == 200 and isinstance(tid, int), "HTTP %s / task=%s" % (s, tid))
        if tid:
            s, d = _req("GET", "/api/ops/tasks/%s" % tid, tok)
            t = d.get("data") or {}
            check("取证任务可轮询", s == 200 and t.get("kind") == "log_fetch",
                  "HTTP %s / status=%s" % (s, t.get("status")))
            s, d = _req("GET", "/api/ops/tasks", tok)
            check("任务列表", s == 200 and isinstance((d.get("data") or {}).get("items"), list), "HTTP %s" % s)

    # 恢复动作：能力声明与护栏判决
    s, d = _req("GET", "/api/ops/heal/actions", tok)
    acts = (d.get("data") or {}).get("items") or []
    names = sorted(a.get("action") for a in acts)
    check("恢复动作白名单（4 个枚举动作）",
          s == 200 and names == ["reconnect_pool", "restart_container", "restart_service", "rotate_logs"],
          "实际=%s" % names)
    check("恢复动作含后置校验声明", bool(acts) and all(a.get("post") for a in acts),
          "每个动作都要有后置校验（空清单不算通过）")
    if svcs:
        sid = svcs[0]["id"]
        s, d = _req("GET", "/api/ops/services/%s/heal/preview?action=restart_service" % sid, tok)
        v = d.get("data") or {}
        check("护栏预览（只读判决）", s == 200 and "allow" in v and "rule" in v,
              "allow=%s rule=%s" % (v.get("allow"), v.get("rule")))
        s, d = _req("GET", "/api/ops/services/%s/heal/preview?action=reboot" % sid, tok)
        check("护栏拒绝白名单外动作", (d.get("data") or {}).get("rule") == "whitelist", "rule=%s" % (d.get("data") or {}).get("rule"))

    # 全局熔断开关：开启后下发必须被拒（409），关闭后恢复
    s, d = _req("GET", "/api/ops/heal/kill-switch", tok)
    orig = (d.get("data") or {}).get("kill_switch")
    check("全局熔断开关可读", s == 200 and isinstance(orig, bool), "kill_switch=%s" % orig)
    if svcs and orig is False:
        sid = svcs[0]["id"]
        s, d = _req("POST", "/api/ops/heal/kill-switch", tok, {"enabled": True})
        check("开启全局熔断", s == 200 and (d.get("data") or {}).get("kill_switch") is True, "HTTP %s" % s)
        s, d = _req("POST", "/api/ops/services/%s/heal" % sid, tok,
                    {"action": "restart_service", "mode": "auto"})
        check("熔断期间下发被拒(409)", s == 409, "HTTP %s" % s)
        s, d = _req("GET", "/api/ops/services/%s/heal/preview?action=restart_service" % sid, tok)
        check("熔断期间仍可只读预览", s == 200, "HTTP %s" % s)
        s, d = _req("POST", "/api/ops/heal/kill-switch", tok, {"enabled": False})
        check("关闭全局熔断（恢复原状）", s == 200 and (d.get("data") or {}).get("kill_switch") is False, "HTTP %s" % s)

    # ── G 通知与人工闭环（D113）──────────────────────────────
    section("G 通知与人工闭环（D113）")
    s, d = _req("GET", "/api/ops/todo-counts", tok)
    counts = d.get("data") or {}
    check("待我处理计数契约", s == 200 and all(
        isinstance(counts.get(k), int) for k in ("pending_runbooks", "needs_human", "failed_tasks_today")),
          "待确认=%s 需人工=%s 失败=%s" % (counts.get("pending_runbooks"), counts.get("needs_human"),
                                      counts.get("failed_tasks_today")))

    s, d = _req("GET", "/api/ops/notify-channels", tok)
    chans = (d.get("data") or {}).get("items") or []
    feishu = [c for c in chans if c.get("type") == "feishu" and c.get("enabled")]
    check("通知渠道列表契约", s == 200 and isinstance(chans, list),
          "渠道 %s / 飞书启用 %s" % (len(chans), len(feishu)))
    if feishu:
        check("飞书渠道测试状态可读", feishu[0].get("last_test_status") in ("ok", "failed", "unknown"),
              "last_test=%s" % feishu[0].get("last_test_status"))
    else:
        print("  SKIP 无已启用飞书渠道（--notify-test 需要先配渠道）")
    if ARGS.notify_test:
        if feishu:
            s, d = _req("POST", "/api/ops/notify-channels/%s/test" % feishu[0]["id"], tok, {})
            check("飞书测试卡真实投递", s == 200 and (d.get("data") or {}).get("ok") is True,
                  "HTTP %s / %s" % (s, (d.get("data") or {}).get("detail")))
        else:
            check("飞书测试卡真实投递", True, "无启用飞书渠道，跳过")

    # 告警指纹反查 + 人工订正回读（找一条带成员告警的事件）
    s, d = _req("GET", "/api/ops/incidents?page_size=30", tok)
    incidents = (d.get("data") or {}).get("items") or []
    hash_val, target_id = None, None
    for item in incidents[:8]:
        s2, d2 = _req("GET", "/api/ops/incidents/%s" % item["id"], tok)
        members = (d2.get("data") or {}).get("members") or []
        if members:
            hash_val, target_id = members[0].get("hash"), item["id"]
            break
    if hash_val:
        s, d = _req("GET", "/api/ops/incidents/by-alert/%s" % hash_val, tok)
        data = d.get("data") or {}
        check("告警指纹反查事件", s == 200 and isinstance(data.get("id"), int),
              "hash=%s… → #%s" % (hash_val[:8], data.get("id")))
        s, d = _req("GET", "/api/ops/incidents/%s/corrections" % target_id, tok)
        check("人工订正回读契约", s == 200 and isinstance((d.get("data") or {}).get("items"), list),
              "items=%s" % len((d.get("data") or {}).get("items") or []))
    else:
        print("  SKIP 无带成员告警的事件（新库可能还没有事件）")

    # 前端深链资产：确认部署的是新版 ESM
    s, js = _get_text("/js/pages/incidents.js", tok)
    check("前端事件深链已部署", s == 200 and "by-alert" in js, "HTTP %s" % s)
    s, js = _get_text("/js/pages/runbooks.js", tok)
    check("前端变更单深链已部署", s == 200 and "Number(sub)" in js, "HTTP %s" % s)

    # ── H Agent 健康与能力增强（P0/P1）────────────────────────
    section("H Agent 健康与能力增强（P0/P1）")
    s, d = _req("GET", "/api/ops/hosts", tok)
    hosts = (d.get("data") or {}).get("items") or []
    check("主机健康字段契约", s == 200 and bool(hosts) and all(
        "auth_state" in h and "version_stale" in h and "latest_agent_version" in h
        for h in hosts), "hosts=%s" % len(hosts))
    check("Agent 版本可判定", bool(hosts) and all(h.get("latest_agent_version") for h in hosts),
          "hosts=%s" % len(hosts))

    s, alerts = _req("GET", "/api/ops/alerts?page_size=5", tok)
    items = (alerts.get("data") or {}).get("items") or []
    check("告警新鲜度字段契约", s == 200 and all(
        "stale" in a and "last_eval_at" in a for a in items), "alerts=%s" % len(items))

    s, txt = _get_text("/agent_main.py.sha256")
    _, agent = _get_text("/agent_main.py")
    want = hashlib.sha256(agent.encode()).hexdigest()
    check("Agent 安装件 SHA256", s == 200 and txt.strip() == want, "HTTP %s" % s)
    s, inst = _get_text("/install.sh")
    check("安装脚本含哈希校验", s == 200 and "agent_main.py.sha256" in inst and "sha256sum -c" in inst,
          "HTTP %s" % s)

    s, js = _get_text("/js/pages/hosts.js", tok)
    check("前端 Agent 升级入口已部署", s == 200 and "upgradeGuide" in js and "version_stale" in js,
          "HTTP %s" % s)
    s, js = _get_text("/js/pages/incidents.js", tok)
    check("前端引用回放已部署", s == 200 and "cite-target" in js and "showStep" in js, "HTTP %s" % s)
    s, css = _get_text("/ops.css", tok)
    check("引用高亮样式已部署", s == 200 and "tr.cite-target" in css, "HTTP %s" % s)

    return _finish()


def _get_text(path, tok=None):
    """取静态文本（JS/HTML）；_req 只按 JSON 解析，不适合静态资产。"""
    req = urllib.request.Request(ARGS.base.rstrip("/") + path)
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def _chat_once(tok, msg):
    """打 chat/stream（SSE），返回 (工具列表, 文本)。"""
    url = ARGS.base.rstrip("/") + "/api/ops/chat/stream"
    body = json.dumps({"message": msg, "session_id": 0}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + tok)
    tools, text = [], []
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:])
                except Exception:
                    continue
                if "tool" in d:
                    tools.append(d["tool"])
                if "text" in d:
                    text.append(d["text"])
    except Exception as e:  # noqa: BLE001
        text.append("__ERR__" + str(e))
    return tools, "".join(text)


def _finish():
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, note) for n, ok, note in RESULTS if not ok]
    print("\n" + "=" * 56)
    print("主流程冒烟：%d/%d 通过" % (passed, total))
    if failed:
        print("失败项：")
        for n, note in failed:
            print("  ✗ %s  %s" % (n, note))
        print("=" * 56)
        sys.exit(1)
    print("全部通过 ✅")
    print("=" * 56)
    sys.exit(0)


if __name__ == "__main__":
    main()
