"""通知内容格式（纯函数）：告警/恢复/需人工/测试 → (标题, 纯文本, 飞书卡片)。

设计原则（详见 docs/01-需求与设计/12-飞书通知消息设计.md）：
- 标题回答"出了什么事"，第一屏回答"哪台机器/哪个指标/现在什么值/持续多久"
- 不再把 32 位主机指纹、`runbook: -`、空字段塞进正文；指纹只在页脚留 8 位
- 告警红/橙/黄，恢复绿，需人工蓝/橙；所有时间按北京时间
- 链接按钮仅在配置了 PUBLIC_BASE_URL 时出现，避免死链
- 本模块不依赖 DB/网络，便于单测；主机名由调用方解析后以 dict 传入
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

CST = timedelta(hours=8)
_SEV = {1: ("S1 紧急", "red"), 2: ("S2 严重", "orange"), 3: ("S3 一般", "yellow")}


def _bj(dt: datetime | None) -> datetime:
    return (dt or datetime.utcnow()) + CST


def fmt_time(dt: datetime | None) -> str:
    return _bj(dt).strftime("%Y-%m-%d %H:%M:%S")


def fmt_dur(seconds: float | int | None) -> str:
    s = max(0, int(seconds or 0))
    if s < 60:
        return "不到 1 分钟"
    if s < 3600:
        return f"{round(s / 60)} 分钟"
    h, m = divmod(round(s / 60), 60)
    return f"{h} 小时 {m} 分钟" if m else f"{h} 小时"


def _esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(s: str, n: int = 90) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _host_line(host: dict, ident: str) -> str:
    name = (host or {}).get("name") or (ident or "")[:12]
    ip = (host or {}).get("ip") or ""
    bits = [name]
    if ip:
        bits.append(ip)
    return f"{_esc(' · '.join(bits))}\n指纹 {_esc((ident or '')[:8])}"


# 指标前缀 → 中文名（D164：语音电话里要念出人话——"磁盘使用率"，
# 不是 "disk dot percent"；飞书卡片同步受益）。
_METRIC_CN = (
    ("disk", "磁盘"), ("mem", "内存"), ("cpu", "CPU"), ("net", "网络"),
    ("load", "负载"), ("vm", "虚拟内存"), ("process", "进程"),
    ("mysql", "MySQL"), ("redis", "Redis"), ("pg", "PostgreSQL"),
    ("nginx", "Nginx"), ("biz", "业务指标"), ("exporter", "采集器"),
)
# 业务维度标签（回答"哪块盘/哪张网卡/哪个实例"——同一指标多序列时这是关键信息）
_DIM_LABELS = (("mountpoint", "挂载"), ("device", "设备"), ("interface", "网卡"),
               ("instance", "实例"), ("server", "服务"), ("db", "库"),
               ("queue", "队列"), ("region", "区域"), ("path", "路径"))


def _metric_info(ev) -> tuple[str, str]:
    tags = getattr(ev, "tags", {}) or {}
    metric = str(getattr(ev, "metric", "") or "-")
    extra = [str(x) for x in (tags.get("probe_type"), tags.get("target")) if x]
    # D164：业务维度（哪块盘…）进展示名——语音与卡片都要能回答"具体哪个对象"
    dims = [f"{label} {tags[k]}" for k, label in _DIM_LABELS if str(tags.get(k) or "").strip()]
    extra = extra + dims
    # 中文名：disk.percent → 磁盘使用率 / mysql.connections.current → MySQL 连接数
    cn = ""
    for prefix, name in _METRIC_CN:
        if metric.startswith(prefix + "."):
            tail = metric[len(prefix) + 1:]
            # D164：磁盘按挂载点的明细指标（Agent 的 disk.<挂载点>.percent）
            # 要念出"哪块盘"——disk.percent=系统盘，disk.data.percent=data 盘
            if prefix == "disk":
                if tail == "percent":
                    cn = "磁盘使用率"
                    break
                parts = tail.split(".")
                if len(parts) == 2 and parts[1] == "percent":
                    cn = f"磁盘 {parts[0]} 分区使用率"
                    break
            tail_cn = {"percent": "使用率", "connections.current": "当前连接数",
                       "connections.max": "最大连接数", "threads.running": "活跃线程数",
                       "queries.total": "查询总数", "slow_queries.total": "慢查询数",
                       "memory.used": "内存占用", "memory.max": "内存上限",
                       "keyspace.hits": "缓存命中", "io.read.bytes": "读字节",
                       "io.write.bytes": "写字节", "io.reads.completed": "读次数",
                       "io.writes.completed": "写次数", "bytes_recv": "接收流量",
                       "bytes_sent": "发送流量", "tcp.established": "TCP 连接数",
                       "available.bytes": "可用内存", "total.bytes": "总内存"}.get(tail, "")
            cn = f"{name}{tail_cn}" if tail_cn else f"{name} {tail}"
            break
    label = (cn or metric) + (f"（{' · '.join(extra)}）" if extra else "")
    return label, metric


def _value_info(ev, rule_cfg: dict) -> str:
    tags = getattr(ev, "tags", {}) or {}
    v = float(getattr(ev, "trigger_value", 0) or 0)
    metric = str(getattr(ev, "metric", "") or "")
    expr = str(rule_cfg.get("expr") or "").strip()
    if tags.get("probe_type") or tags.get("probe_status"):
        status = tags.get("probe_status") or ("down" if v >= 1 else "up")
        if status == "down":
            return "down（探测失败）" + (f"，阈值 {_esc(expr)}" if expr else "")
        return "up（探测正常）" + (f"，阈值 {_esc(expr)}" if expr else "")
    if metric.endswith(".percent"):
        s = f"{v:.1f}%"
        m = re.match(r"^\s*(>=|<=|==|!=|>|<)\s*([-\d.]+)", expr)
        if m:
            s += f"（阈值 {m.group(1)}{m.group(2)}%）"
        return s
    if "证书" in metric:
        s = f"{v:g} 天"
        if expr:
            s += f"（阈值 {_esc(expr)} 天）"
        return s
    return f"{v:g}" + (f"（阈值 {_esc(expr)}）" if expr else "")


def _duration_seconds(ev, now: datetime) -> int:
    start = getattr(ev, "first_trigger_time", None) or now
    end = (getattr(ev, "recover_time", None) or now) if getattr(ev, "is_recovered", False) else now
    return max(0, int((end - start).total_seconds()))


def _field(label: str, value: str, short: bool = True) -> dict:
    return {"is_short": short, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}


def _div(md: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": md}}


def _note(text: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _button(label: str, url: str, primary: bool = False) -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "url": url,
            "type": "primary" if primary else "default"}


def _actions(buttons: list[dict]) -> dict | None:
    buttons = [b for b in buttons if b.get("url")]
    return {"tag": "action", "actions": buttons} if buttons else None


def _card(template: str, title: str, elements: list[dict]) -> dict:
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": _cut(title, 80)}},
            "elements": [e for e in elements if e]}


def _base_url(base_url: str | None) -> str:
    b = (base_url or "").strip().rstrip("/")
    return b if b.startswith(("http://", "https://")) else ""


def _link(base: str, frag: str) -> str:
    return f"{base}/#/{frag.lstrip('/')}" if base else ""


def describe_alert(ev, rule_cfg: dict, host: dict, base_url: str = "", now: datetime | None = None) -> tuple[str, str, dict]:
    """告警/恢复：返回 (标题, 纯文本, 飞书卡片)。"""
    now = now or datetime.utcnow()
    base = _base_url(base_url)
    sev = int(getattr(ev, "severity", 3) or 3)
    sev_label, sev_color = _SEV.get(sev, _SEV[3])
    recovered = bool(getattr(ev, "is_recovered", False))
    rule_name = _esc(rule_cfg.get("name") or getattr(ev, "rule_name", "") or "-")
    metric_label, metric = _metric_info(ev)
    value = _value_info(ev, rule_cfg)
    duration = fmt_dur(_duration_seconds(ev, now))
    host_name = (host or {}).get("name") or (getattr(ev, "host_ident", "") or "")[:12]
    ident = getattr(ev, "host_ident", "") or ""
    first = fmt_time(getattr(ev, "first_trigger_time", None))
    status = "已恢复" if recovered else sev_label
    title = f"[{status}] {rule_name} · {_esc(metric)}"
    if recovered:
        rec_at = fmt_time(getattr(ev, "recover_time", None) or now)
        summary = (f"{_esc(host_name)} 的 **{_esc(metric_label)}** 已于 **{rec_at}** 恢复，"
                   f"本次持续 **{duration}**。")
    else:
        summary = (f"{_esc(host_name)} 的 **{_esc(metric_label)}** 触发告警，"
                   f"当前值 **{value}**，已持续 **{duration}**。")
    fields = [
        _field("主机", _host_line(host, ident)),
        _field("规则", rule_name),
        _field("指标", _esc(metric_label)),
        _field("恢复值" if recovered else "当前值", value),
        _field("首次触发", first),
    ]
    if recovered:
        fields.append(_field("恢复时间", fmt_time(getattr(ev, "recover_time", None) or now)))
    else:
        fields.append(_field("持续", duration))
    buttons = []
    if base:
        # 告警发生在聚类之前，卡片里还没有 incident_id；用告警指纹深链，
        # 点击时事件已生成，前端通过 /incidents/by-alert/<hash> 解析并直接打开抽屉。
        deep = _link(base, f"incidents/alert/{getattr(ev, 'hash', '')}")
        buttons = [_button("查看事件", deep, primary=True),
                   _button("查看告警", _link(base, "alerts"))]
    rb_url = str(rule_cfg.get("runbook_url") or "").strip()
    if rb_url.startswith(("http://", "https://")):
        buttons.append(_button("处置手册", rb_url))
    elements = [_div(summary), {"tag": "div", "fields": fields}, {"tag": "hr"}, _actions(buttons),
                _note(f"AI 运维监控平台 · 告警指纹 {ident[:8]} · {fmt_time(now)}（北京时间）")]
    card = _card("green" if recovered else sev_color, title, elements)
    text = (f"主机: {_host_line(host, ident)}\n规则: {rule_name}\n指标: {metric_label}\n"
            f"{'恢复值' if recovered else '当前值'}: {value}\n首次触发: {first}\n"
            f"{'恢复时间: ' + fmt_time(getattr(ev, 'recover_time', None) or now) + chr(10) if recovered else ''}"
            f"持续: {duration}\n告警指纹: {ident[:8]}")
    return title, text, card


def describe_attention(kind: str, title: str, body: str, host: dict, meta: dict | None = None,
                       base_url: str = "", now: datetime | None = None) -> tuple[str, str, dict]:
    """需人工：变更单待确认 / 诊断证据不足。"""
    meta = meta or {}
    now = now or datetime.utcnow()
    base = _base_url(base_url)
    ident = str(meta.get("host") or "")
    is_diag = kind == "diagnosis_needs_human"
    label = "AI 诊断证据不足" if is_diag else "变更单待确认"
    template = "blue" if is_diag else "orange"
    clean_title = title if title.startswith("[需人工]") else f"[需人工] {title}"
    summary = _esc(body or label)
    fields = [_field("主机", _host_line(host, ident))]
    if meta.get("incident_id"):
        fields.append(_field("关联事件", f"#{meta['incident_id']}"))
    if meta.get("runbook_id"):
        fields.append(_field("变更单", f"#{meta['runbook_id']}"))
    if is_diag:
        fields.append(_field("置信度", _esc(meta.get("confidence") or "INCONCLUSIVE")))
    else:
        if meta.get("steps") is not None:
            fields.append(_field("步骤数", str(meta["steps"])))
        if meta.get("risk"):
            fields.append(_field("风险", _esc(meta["risk"])))
        actions = [str(a) for a in (meta.get("actions") or []) if a]
        if actions:
            fields.append(_field("涉及动作", _esc("、".join(actions[:4])), short=False))
    buttons = []
    if base:
        if is_diag:
            frag = f"incidents/{meta['incident_id']}" if meta.get("incident_id") else "incidents"
            buttons = [_button("去订正", _link(base, frag), primary=True)]
        else:
            rb_frag = f"runbooks/{meta['runbook_id']}" if meta.get("runbook_id") else "runbooks"
            buttons = [_button("打开变更单", _link(base, rb_frag), primary=True)]
            if meta.get("incident_id"):
                buttons.append(_button("查看事件", _link(base, f"incidents/{meta['incident_id']}")))
    hint = ("请在事件详情中补充真实根因与实际处置；订正会沉淀为人工案例。" if is_diag
            else "确认后平台会通过 Agent 任务通道执行；结果与 task_id 写入审计。")
    elements = [_div(summary), {"tag": "div", "fields": fields}, _div(f"**需要你做什么**\n{hint}"),
                _actions(buttons), _note(f"AI 运维监控平台 · {fmt_time(now)}（北京时间）")]
    card = _card(template, clean_title, elements)
    pairs = [
        ("主机", (host or {}).get("name") or ident[:12]),
        ("关联事件", f"#{meta['incident_id']}" if meta.get("incident_id") else ""),
        ("变更单", f"#{meta['runbook_id']}" if meta.get("runbook_id") else ""),
        ("置信度", meta.get("confidence") if is_diag else ""),
        ("步骤数", meta.get("steps") if not is_diag else ""),
        ("风险", meta.get("risk") if not is_diag else ""),
        ("涉及动作", "、".join(meta.get("actions") or []) if not is_diag else ""),
    ]
    text = f"{body}\n" + "\n".join(f"{k}: {v}" for k, v in pairs if v) + f"\n{hint}"
    return clean_title, text, card


# 渠道类型 → 中文名（测试消息与提示用；写死的"飞书"对语音渠道会在电话里念出来）
_CHANNEL_TYPE_CN = {
    "feishu": "飞书",
    "dingtalk": "钉钉",
    "wecom": "企业微信",
    "phone": "电话",
    "email": "邮件",
    "webhook": "Webhook",
    "aliyun_voice": "阿里云语音",
}


def describe_test(channel_name: str, base_url: str = "", now: datetime | None = None,
                  channel_type: str = "feishu") -> tuple[str, str, dict]:
    """渠道连通性测试。

    D153：标题原先**写死**"[测试] 飞书通知渠道连通"——对阿里云语音渠道，
    电话里会 literally 念出「飞书通知渠道连通」（渠道类型完全不对），
    让人以为拨错产品。现在按渠道类型给名字。
    """
    now = now or datetime.utcnow()
    base = _base_url(base_url)
    cn = _CHANNEL_TYPE_CN.get((channel_type or "").strip().lower(), "通知")
    is_voice = (channel_type or "").strip().lower() == "aliyun_voice"
    lead = ("语音通知已连通" if is_voice else f"{cn}渠道已连通")
    elements = [
        _div(f"{cn}渠道已连通，后续**告警、恢复、需人工提醒**都会按此渠道发送。"),
        {"tag": "div", "fields": [
            _field("渠道", _esc(channel_name)),
            _field("发送时间", fmt_time(now)),
            _field("平台", "AI 运维监控平台"),
        ]},
        _actions([_button("打开平台", base, primary=True)] if base else []),
        _note("这是一条测试消息，无需处理。"),
    ]
    title = f"[测试] {lead}"
    card = _card("blue", title, elements)
    text = f"渠道: {channel_name}\n发送时间: {fmt_time(now)}\n这是一条测试消息，无需处理。"
    return title, text, card
