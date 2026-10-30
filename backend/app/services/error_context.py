"""错误上下文与多源时间线（P5b，FR-410）。

要解决的问题（调研 §2 的效率黑洞）：一次 6h10m 的故障排查里，
**手工对齐各主机时间戳/日志格式占 19%**、逐台翻日志占 18%。
本模块把这两件事自动化：

  ① **错误簇聚类**：把取回的多段日志按"错误签名 + 堆栈指纹"聚成 3~5 个候选错误簇，
     并标出每簇的首次出现时间与影响（跨主机同签名可归并）——让人一眼看到"有几类错、从哪开始"；
  ② **多源时间线**：把指标/探活/日志/变更记录对齐到一条时间轴（统一 UTC → 展示本地），
     并**显式标注时区来源**，避免"看着同一时刻其实是两个时区"的经典误判；
  ③ 报告「错误上下文」段的素材：报错原文 + 前后文 + 时间点 + 关联指标。

设计纪律：
  · 只做**证据组织**，不做因果推断——因果交给 LLM（行业研究明确"全自动 RCA 仍是开放问题"）；
  · 时间一律先归一到 UTC 再排序，展示层再转本地；每条都带 `source`（哪个来源/哪个文件）；
  · 无法解析时间戳的行不丢，归入"时间未知"桶并如实标注（不猜时间）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("ctx")

# 堆栈指纹：把 "at com.a.B.c(B.java:12)" 归一为 "com.a.B.c"（去掉行号，跨版本可比）
_FRAME_RE = re.compile(r"^\s+(?:at\s+)?([\w$.]+)\(", re.M)


def stack_fingerprint(text: str) -> str:
    """从一段日志里提取"堆栈指纹"（顶层 3 帧的符号名）。

    为什么要它：同一类错误在不同时间/不同实例上，消息文本可能带不同参数（连接数、耗时），
    但**调用栈是稳定的**。用栈指纹聚合比用消息文本聚合准确得多。
    """
    frames = _FRAME_RE.findall(text or "")
    if not frames:
        return ""
    return " → ".join(frames[:3])


def cluster_segments(segments: list[dict], min_count: int = 1) -> list[dict]:
    """把取回的日志段聚成错误簇。

    入参：Agent 回传的 segments（含 signature/count/first_ts/lines）。
    出参：按"影响面×首次出现时间"排序的簇列表，每簇含：
      {key, signature, stack, count, first_ts, last_ts, hosts, sample}
    跨主机同签名会归并（hosts 记录来源主机集合）——这正是"同一根因影响多台"的信号。
    """
    buckets: dict[str, dict] = {}
    for seg in segments or []:
        lines = seg.get("lines") or []
        text = "\n".join(str(x) for x in lines)
        sig = str(seg.get("signature") or "").strip() or "未分类"
        fp = stack_fingerprint(text)
        # 聚合键：优先按「出错现场」（栈顶帧）聚，没有栈才退回签名。
        # 为什么（实测踩到两次）：
        #   ① 按整条栈聚 -> 不同深度/不同实例的同一错误被拆开（A 机 2 帧、B 机 1 帧）；
        #   ② 只按签名聚 -> 同一根因因措辞不同被拆成多簇
        #      （Too many connections (max 151) 与 Too many connections 本是同一问题）。
        # 栈顶帧即「错误现场」，最稳；签名仅作展示标签（取簇内出现最多的一条）。
        top = fp.split(" → ")[0] if fp else ""
        key = top or sig
        b = buckets.setdefault(key, {
            "key": key, "signature": sig, "stack": fp, "count": 0, "sig_votes": {},
            "first_ts": "", "last_ts": "", "hosts": set(), "sample": []})
        b["count"] += int(seg.get("count") or 0)
        b["sig_votes"][sig] = b["sig_votes"].get(sig, 0) + 1
        ts_first = str(seg.get("first_ts") or "")
        ts_last = str(seg.get("last_ts") or "")
        if ts_first and (not b["first_ts"] or ts_first < b["first_ts"]):
            b["first_ts"] = ts_first
        if ts_last and ts_last > (b["last_ts"] or ""):
            b["last_ts"] = ts_last
        for h in (seg.get("hosts") or []):
            b["hosts"].add(h)
        if len(b["sample"]) < 8:
            b["sample"].extend(str(x) for x in lines[:8 - len(b["sample"])])

    out = []
    for b in buckets.values():
        if b["count"] < min_count:
            continue
        b["hosts"] = sorted(b["hosts"])
        votes = b.pop("sig_votes", {})
        if votes:
            b["signature"] = max(votes.items(), key=lambda kv: kv[1])[0]
            b["merged_signatures"] = sorted(votes)
        out.append(b)
    out = _merge_similar(out)
    # 排序：先按「首次出现时间」（首错优先），时间未知的按影响面
    out.sort(key=lambda x: (not x["first_ts"], x["first_ts"] or "", -x["count"]))
    return out


def _norm_label(sig: str) -> str:
    """归一化簇标签用于"同不同类"的比较：去签名前缀、数字/hex/路径全部归一、去括号内容。"""
    t = re.sub(r"^[a-z]+:\s*", "", str(sig or "").lower())
    t = re.sub(r"\([^)]*\)", "", t)                    # 去掉括号里的参数
    t = re.sub(r"\b\d+(?:\.\d+)?\b", "", t)
    t = re.sub(r"\b[0-9a-f]{6,}\b", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _merge_similar(clusters: list[dict]) -> list[dict]:
    """把"同一根因但措辞不同"的簇合并。

    实测场景：同一批 Too many connections，一段日志带调用栈（按栈聚）、另一段不带
    （只能按签名聚），于是同一问题被拆成两簇——报告里"有几类错"就失真了。
    规则（保守）：归一化后**一方包含另一方**才合并；合并时保留有栈的那个、累加计数与主机。
    """
    merged: list[dict] = []
    for c in sorted(clusters, key=lambda x: (not bool(x.get("stack")), -x["count"])):
        n = _norm_label(c["signature"])
        target = None
        if n:
            for m in merged:
                mn = _norm_label(m["signature"])
                if mn and (n in mn or mn in n):
                    target = m
                    break
        if target is None:
            merged.append(c)
            continue
        target["count"] += c["count"]
        target["hosts"] = sorted(set(target.get("hosts") or []) | set(c.get("hosts") or []))
        for k in ("first_ts", "last_ts"):
            if c.get(k) and (not target.get(k) or (k == "first_ts" and c[k] < target[k])
                             or (k == "last_ts" and c[k] > target[k])):
                target[k] = c[k]
        target.setdefault("merged_signatures", [])
        target["merged_signatures"] = sorted(set(target["merged_signatures"])
                                             | set(c.get("merged_signatures") or [])
                                             | {c["signature"]})
        if len(target.get("sample") or []) < 8:
            target["sample"] = (target.get("sample") or []) + (c.get("sample") or [])[:4]
        if not target.get("stack") and c.get("stack"):
            target["stack"] = c["stack"]
    return merged


# ── 多源时间线 ────────────────────────────────────────────────────
def _iso(v) -> str:
    """把各种时间表示归一到 ISO8601（UTC 语义）。解析不出来返回空串（不猜）。"""
    if v is None:
        return ""
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    s = str(v).strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return ""


def build_timeline(*, incident: dict | None = None, alerts: list[dict] | None = None,
                   metrics: list[dict] | None = None, probes: list[dict] | None = None,
                   logs: list[dict] | None = None, runbooks: list[dict] | None = None,
                   tasks: list[dict] | None = None) -> dict:
    """把多来源事件对齐到一条 UTC 时间轴。

    每个来源的原始时间字段名不同，这是"手工对齐"的根源；这里统一归一，
    并为每条事件打上 `source`（来源）与 `raw_time`（原始表示）——**可核对**是底线，
    用户要能看出"这条时间是哪个系统给的、原本长什么样"。
    """
    items: list[dict] = []

    def add(source: str, t, title: str, detail: str = "", **extra):
        iso = _iso(t)
        items.append({"source": source, "ts": iso, "raw_time": str(t or ""),
                      "title": title, "detail": detail, **extra})

    if incident:
        add("事件", incident.get("created_at"), "事件创建", str(incident.get("title") or ""))
        if incident.get("resolved_at"):
            add("事件", incident.get("resolved_at"), "事件关闭")

    for a in alerts or []:
        add("告警", a.get("first_trigger_time") or a.get("trigger_time"),
            f"告警触发 S{a.get('severity')} {a.get('rule_name') or ''}",
            f"{a.get('metric')}={a.get('trigger_value')}", host=a.get("host_ident", ""),
            recovered=bool(a.get("is_recovered")), recover_time=_iso(a.get("recover_time")))

    for m in metrics or []:
        add("指标", m.get("ts"), f"指标异常 {m.get('metric')}", f"值 {m.get('value')}", host=m.get("host", ""))

    for p in probes or []:
        add("探活", p.get("ts"), f"探活 {p.get('name')} → {p.get('status')}", str(p.get("target") or ""),
            host=p.get("host", ""))

    for lg in logs or []:
        add("日志", lg.get("first_ts"), f"日志错误簇「{lg.get('signature')}」首次出现",
            f"命中 {lg.get('count')} 次" + (f" · 栈 {lg.get('stack')}" if lg.get("stack") else ""),
            host=",".join(lg.get("hosts") or []), source_file=lg.get("source_file") or "")

    for rb in runbooks or []:
        add("变更", rb.get("created_at"), f"变更单 {rb.get('status')}", str(rb.get("name") or ""),
            detail_extra=rb.get("summary", ""))

    for t in tasks or []:
        add("取证", t.get("created_at"), f"{'取日志' if t.get('kind') == 'log_fetch' else '动作'}任务",
            str(t.get("summary") or ""), status=t.get("status"))

    known = [x for x in items if x["ts"]]
    unknown = [x for x in items if not x["ts"]]
    known.sort(key=lambda x: x["ts"])
    return {
        "items": known + unknown,
        "total": len(items),
        "unknown_time": len(unknown),        # 如实暴露"有多少条时间没解析出来"
        "range": {"from": known[0]["ts"] if known else "", "to": known[-1]["ts"] if known else ""},
        "timezone_note": "时间统一按 UTC 归一后排序；界面按本地时区展示。"
                         "各来源的原始时间保留在 raw_time 字段，便于核对时区差异。",
    }


def error_context_section(clusters: list[dict], timeline: dict | None = None,
                          related_metrics: list[dict] | None = None) -> str:
    """生成报告用的「错误上下文」Markdown 段（FR-410）。

    强调三条底线（沿用证据链纪律）：**原文可核对**、**时间点明确**、**不写没有证据的因果**。
    """
    if not clusters:
        return ("## 错误上下文\n\n"
                "未获取到远程日志证据（Agent 未升级 / 未声明日志路径 / 时间窗不覆盖）。"
                "本段不作为结论依据——请在 `not_checked` 中如实记录。\n")

    parts = ["## 错误上下文", ""]
    parts.append(f"共聚出 {len(clusters)} 个候选错误簇（按首次出现时间排序，首错优先）：")
    parts.append("")
    for i, c in enumerate(clusters[:6], 1):
        head = f"**{i}. `{c['signature']}`** — 命中 {c['count']} 次"
        if c.get("first_ts"):
            head += f"，首次 {str(c['first_ts'])[:19]} UTC"
        if c.get("hosts"):
            head += f"，涉及主机 {', '.join(c['hosts'][:4])}"
        parts.append(head)
        if c.get("stack"):
            parts.append(f"- 调用栈指纹：`{c['stack']}`（同栈即同类错误，可跨实例归并）")
        for line in (c.get("sample") or [])[:3]:
            parts.append(f"- `{str(line)[:180]}`")
        parts.append("")
    if timeline and timeline.get("items"):
        parts.append("### 时间线（多来源对齐，UTC）")
        parts.append("")
        parts.append("| 时间(UTC) | 来源 | 事件 | 说明 |")
        parts.append("|---|---|---|---|")
        for x in timeline["items"][:20]:
            parts.append(f"| {x['ts'][:19]} | {x['source']} | {x['title']} | {str(x.get('detail') or '')[:60]} |")
        if timeline.get("unknown_time"):
            parts.append("")
            parts.append(f"> 另有 {timeline['unknown_time']} 条记录的时间未能解析，未参与排序（不猜时间）。")
        parts.append("")
    if related_metrics:
        parts.append("### 关联指标（与错误簇时间点附近）")
        parts.append("")
        for m in related_metrics[:8]:
            parts.append(f"- `{m.get('metric')}`：{m.get('summary') or m.get('value')}")
        parts.append("")
    return "\n".join(parts)


def related_metrics_from_ring(ident: str, metric_names: list[str], center_iso: str,
                              window_min: int = 10) -> list[dict]:
    """取"错误首次出现时刻前后 window_min 分钟"的指标摘要（把日志与指标对上）。"""
    from app.store import ring

    center = None
    try:
        center = datetime.fromisoformat(center_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return []
    if center is None:
        return []
    start, end = center - timedelta(minutes=window_min), center + timedelta(minutes=window_min)
    out = []
    for name in metric_names:
        pts = ring.query_points(ident, name, start, end)
        if not pts:
            continue
        vals = [v for _, v in pts]
        out.append({"metric": name, "n": len(vals), "min": round(min(vals), 2),
                    "max": round(max(vals), 2), "avg": round(sum(vals) / len(vals), 2),
                    "summary": f"{len(vals)} 点 · 最小 {round(min(vals), 2)} / 最大 {round(max(vals), 2)}"
                               f" / 均值 {round(sum(vals) / len(vals), 2)}"})
    return out
