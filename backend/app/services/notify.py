"""通知服务：节奏三参数 + 渠道派发 + 台账 + 熔断（FR-C5/C6；03 §1.8）。

渠道传输层走 apprise（ADR-002）——协议细节不自研；本模块只保留业务语义：
节奏判定（should_notify）、静默协同（mute.py）、发送台账（notification_records）、
**失败熔断**（夯实轮补：坏渠道连续失败 → 冷却期内跳过，避免无效投递与台账膨胀）。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.engine.state import FireState
from app.models.alert import NotificationChannel, NotificationRecord
from app.models.host import Host
from app.services.notify_format import describe_alert, describe_attention
from app.services.notify_transport import send_channel

log = logging.getLogger("notify")

# 熔断参数（夯实轮）：连续失败 ≥ 阈值 → 冷却 30 分钟；冷却到期自动重试一次
NOTIFY_FAIL_THRESHOLD = 5
NOTIFY_COOLDOWN_MIN = 30
# 「需人工」提醒去重窗口：同一 kind|title 在窗口内只投递一次，避免重复诊断刷屏
ATTENTION_DEDUPE_MIN = 10


def should_notify(st: FireState, repeat_step_min: int, max_number: int, notify_recovered: bool, ev_is_recovered: bool, now: datetime | None = None) -> bool:
    """通知节奏三参数（FR-C5 验收：首条→按步重发→封顶；恢复通知开关）。

    语义：notify_count = 1 + 已发条数；封顶 max_number 表示"最多发 max_number 条"。
    """
    now = now or datetime.utcnow()
    if ev_is_recovered:
        return notify_recovered
    if st.notify_count > 1 or st.last_sent is not None:
        if repeat_step_min == 0:
            return False  # 只发一次
        if now < st.last_sent + timedelta(minutes=repeat_step_min):
            return False
    if max_number and st.notify_count > max_number:
        return False
    return True


async def notify_attention(db, kind: str, title: str, body: str, meta: dict | None = None) -> list[dict]:
    """D113「会喊人」：把"需要人工确认/订正"推到所有启用渠道（复用熔断与台账）。

    kind 形如 hitl_pending / diagnosis_needs_human；event_hash 用 kind+title 摘要，
    与告警台账共用 NotificationRecord（通知记录页可见）。
    """
    import hashlib as _hl

    channels = (await db.execute(
        select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))).scalars().all()
    meta = meta or {}
    h = _hl.md5(f"{kind}|{title}".encode()).hexdigest()
    host = None
    if meta.get("host"):
        host = (await db.execute(select(Host).where(Host.ident == meta["host"]))).scalar_one_or_none()
    host_info = {"name": host.name if host else "", "ip": host.ip if host else ""}
    out_title, out_body, card = describe_attention(
        kind, title, body, host_info, meta, get_settings().public_base_url)
    recent = (await db.execute(select(NotificationRecord.id).where(
        NotificationRecord.event_hash == h,
        NotificationRecord.status == "sent",
        NotificationRecord.created_at >= datetime.utcnow() - timedelta(minutes=ATTENTION_DEDUPE_MIN),
    ).limit(1))).first()
    if recent:
        return [{"channel": "", "ok": True,
                 "detail": f"{ATTENTION_DEDUPE_MIN} 分钟内已提醒过，跳过重复投递"}]
    results = []
    for ch in channels:
        if ch.disabled_until is not None and ch.disabled_until > datetime.utcnow():
            results.append({"channel": ch.name, "ok": False, "detail": "熔断冷却中（跳过投递）"})
            continue
        ok, detail = await send_channel(ch.type, ch.config_json or {}, out_title, out_body, card=card)
        if ok:
            ch.fail_streak = 0
            ch.disabled_until = None
        else:
            ch.fail_streak = (ch.fail_streak or 0) + 1
            if ch.fail_streak >= NOTIFY_FAIL_THRESHOLD:
                ch.disabled_until = datetime.utcnow() + timedelta(minutes=NOTIFY_COOLDOWN_MIN)
        db.add(NotificationRecord(event_hash=h, channel=ch.name, target=str(ch.config_json)[:200],
                                  status="sent" if ok else "failed", error_detail="" if ok else detail))
        results.append({"channel": ch.name, "ok": ok, "detail": detail})
    await db.commit()
    return results


async def _resolve_new_rules(db, ev, rule_cfg: dict) -> list[dict]:
    """解析新通知规则的投递目标（没命中任何规则 → 空列表 → 回落老路径）。

    顺序（夜莺同款，见 ADR-003）：
      ① 规则级 Pipeline（通知规则上的，影响该规则的所有配置）
      ② 按 级别/时段/标签/属性 筛配置
      ③ 通知配置级 Pipeline（只影响这一条配置的这一次发送）
    """
    from app.models.notify_rule import NotifyRule
    from app.services import notify_rules as nsvc
    from app.services.pipeline import apply_pipeline

    rules = (await db.execute(
        select(NotifyRule).where(NotifyRule.enabled.is_(True)).order_by(NotifyRule.id))).scalars().all()
    if not rules:
        return []

    now = datetime.utcnow()
    event = {
        "severity": int(getattr(ev, "severity", 0) or 0),
        "tags": dict(getattr(ev, "tags", None) or {}),
        "attributes": nsvc.event_attributes(ev),
    }
    out: list[dict] = []
    for rule in rules:
        # ① 规则级 Pipeline
        ev_p, dropped = apply_pipeline(rule.pipelines or [], {"tags": dict(event["tags"])})
        if dropped:
            continue
        event["tags"] = ev_p.get("tags") or event["tags"]
        # ② 四维筛选
        for cfg in (rule.configs or []):
            if not nsvc.match_config(cfg, event, now):
                continue
            # ③ 通知配置级 Pipeline（只影响本次发送的注解/标签展示）
            cfg_ev, cfg_dropped = apply_pipeline(cfg.get("pipelines") or [],
                                                 {"tags": dict(event["tags"])})
            if cfg_dropped:
                continue
            out.append({
                "channel_id": cfg.get("channel_id"),
                "template_id": cfg.get("template_id"),
                "targets": cfg.get("targets") or [],
                "rule_id": rule.id,
                "rule_name": rule.name,
                "tags": cfg_ev.get("tags") or event["tags"],
                "annotations": cfg_ev.get("annotations") or {},
            })
    return out


async def _dispatch_by_new_rules(db, ev, rule_cfg: dict, channels, targets: list[dict]) -> list[dict]:
    """按新通知规则投递（**不再走** channels_by_level —— 两套互斥，见 ADR-003）。"""
    from app.services.notify_format import describe_alert

    host = (await db.execute(select(Host).where(Host.ident == ev.host_ident))).scalar_one_or_none()
    host_info = {"name": host.name if host else "", "ip": host.ip if host else ""}
    cfg = {**rule_cfg, "expr": rule_cfg.get("expr", ""), "severity": rule_cfg.get("severity", ev.severity)}
    title, body, card = describe_alert(ev, cfg, host_info, get_settings().public_base_url)

    by_id = {ch.id: ch for ch in channels}
    results: list[dict] = []
    for tg in targets:
        ch = by_id.get(int(tg.get("channel_id") or 0))
        if ch is None:
            results.append({"channel": f"规则「{tg['rule_name']}」", "ok": False,
                            "detail": f"配置的渠道 id={tg.get('channel_id')} 不存在或已停用"})
            continue
        if ch.disabled_until is not None and ch.disabled_until > datetime.utcnow():
            results.append({"channel": ch.name, "ok": False, "detail": "熔断冷却中（跳过投递）"})
            continue
        # 注解拼进正文（注解只给人看，不参与匹配）
        ann = tg.get("annotations") or {}
        send_body = body
        if ann:
            extra = "；".join(f"{k}={v}" for k, v in ann.items())
            send_body = f"{body}\n{extra}"
        ok, detail = await send_channel(ch.type, ch.config_json or {}, title, send_body, card=card)
        if ok:
            ch.fail_streak = 0
            ch.disabled_until = None
            db.add(NotificationRecord(event_hash=ev.hash, channel=ch.name, target=str(ch.config_json)[:200],
                                      status="sent", error_detail=""))
        else:
            ch.fail_streak = (ch.fail_streak or 0) + 1
            if ch.fail_streak >= NOTIFY_FAIL_THRESHOLD:
                ch.disabled_until = datetime.utcnow() + timedelta(minutes=NOTIFY_COOLDOWN_MIN)
                log.warning("通知渠道「%s」连续失败 %s 次 → 熔断 %s 分钟（冷却期内跳过投递）",
                            ch.name, ch.fail_streak, NOTIFY_COOLDOWN_MIN)
            db.add(NotificationRecord(event_hash=ev.hash, channel=ch.name, target=str(ch.config_json)[:200],
                                      status="failed", error_detail=detail))
        results.append({"channel": ch.name, "ok": ok, "detail": detail,
                        "rule": tg.get("rule_name", "")})
    await db.commit()
    return results


async def dispatch(db, ev, st: FireState | None, rule_cfg: dict) -> list[dict]:
    """发通知并写台账。返回发送结果列表。

    D140 分级通知：规则的 channels_by_level（{"1":[渠道名],"2":[...],"3":[...]}）非空
    且含本事件级别键时，**优先**按级别取渠道集合（S1 电话 / S2 S3 IM——用户原话：
    「严重的一定是电话通知，其他的无所谓」）；否则回退既有 channels_json。

    S1 兜底（不能静默降级）：级别 1 的目标集合若**不含任何电话类渠道**
    （channel.type 为 "phone" 或 "aliyun_voice"——D152 起阿里云语音也算电话），
    把集合替换为「全部启用渠道」并在结果里附一条 degraded 说明 ——
    宁可打扰所有人，也不能让紧急告警只躺在 IM 里。

    D167-1：恢复事件在入口统一检查「恢复通知」开关——分级路径与「通知路由」路径
    都从这里走，关了就一条都不发（返回一条说明，台账可查"为什么不响"）。
    """
    if getattr(ev, "is_recovered", False) and not rule_cfg.get("notify_recovered", True):
        return [{"channel": "", "ok": True,
                 "detail": "恢复通知已按规则关闭（恢复通知=关闭），不再投递"}]
    channels = (await db.execute(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))).scalars().all()
    by_level = rule_cfg.get("channels_by_level") or {}
    level_key = str(ev.severity)
    degraded = ""

    # ── D154（FR-423）：新通知规则优先，且与老路径**互斥** ──
    # ① 事件命中任一「启用的新通知规则」→ 走新路径，**不再走** channels_by_level；
    # ② 否则 → 走老路径（行为逐字节不变）；
    # ③ 两者都没命中 → 老的 S1 兜底。
    # 详见 ADR-003。
    new_targets = await _resolve_new_rules(db, ev, rule_cfg)
    if new_targets:
        return await _dispatch_by_new_rules(db, ev, rule_cfg, channels, new_targets)

    if isinstance(by_level, dict) and by_level.get(level_key):
        wanted = set(by_level[level_key])
        by_type = {ch.name: ch.type for ch in channels}
        if level_key == "1" and not any(by_type.get(n) in ("phone", "aliyun_voice") for n in wanted):
            # S1 兜底：电话渠道缺失（被删/停用/配置漏），升级为全渠道广播并明说
            wanted = {ch.name for ch in channels}
            degraded = ("S1 兜底生效：分级配置里没有可用的电话渠道"
                        f"（配置={sorted(by_level[level_key])}），已升级为全渠道广播——"
                        "请到「系统设置 → 通知渠道」补一个「电话」类型渠道（电话 / 阿里云语音）")
            log.warning("%s（event=%s）", degraded, ev.hash)
    else:
        wanted = set(rule_cfg.get("channels_json") or [])
    host = (await db.execute(select(Host).where(Host.ident == ev.host_ident))).scalar_one_or_none()
    host_info = {"name": host.name if host else "", "ip": host.ip if host else ""}
    rule_cfg = {**rule_cfg, "expr": rule_cfg.get("expr", ""),
                "severity": rule_cfg.get("severity", ev.severity)}
    title, body, card = describe_alert(ev, rule_cfg, host_info, get_settings().public_base_url)
    results = []
    for ch in channels:
        if wanted and ch.name not in wanted:
            continue
        # 熔断（夯实轮补）：连续失败达阈值的渠道在冷却期内直接跳过。
        # 关键：冷却期内的跳过**不写失败台账**——否则坏渠道会持续刷台账（实测 33 条/几分钟）。
        if ch.disabled_until is not None and ch.disabled_until > datetime.utcnow():
            results.append({"channel": ch.name, "ok": False, "detail": "熔断冷却中（跳过投递）"})
            continue
        ok, detail = await send_channel(ch.type, ch.config_json or {}, title, body, card=card)
        if ok:
            ch.fail_streak = 0
            ch.disabled_until = None
            db.add(NotificationRecord(event_hash=ev.hash, channel=ch.name, target=str(ch.config_json)[:200],
                                      status="sent", error_detail=""))
        else:
            ch.fail_streak = (ch.fail_streak or 0) + 1
            if ch.fail_streak >= NOTIFY_FAIL_THRESHOLD:
                ch.disabled_until = datetime.utcnow() + timedelta(minutes=NOTIFY_COOLDOWN_MIN)
                log.warning("通知渠道「%s」连续失败 %s 次 → 熔断 %s 分钟（冷却期内跳过投递）",
                            ch.name, ch.fail_streak, NOTIFY_COOLDOWN_MIN)
                # 触发熔断的这一次仍写台账（留证据）；后续冷却期内的跳过不写
            db.add(NotificationRecord(event_hash=ev.hash, channel=ch.name, target=str(ch.config_json)[:200],
                                      status="failed", error_detail=detail))
        if ok and ch.last_test_status != "ok":
            ch.last_test_status = "ok"
        results.append({"channel": ch.name, "ok": ok, "detail": detail})
    if degraded:
        results.append({"channel": "", "ok": False, "detail": degraded})
        db.add(NotificationRecord(event_hash=ev.hash, channel="分级通知",
                                  target="", status="failed", error_detail=degraded))
    await db.commit()
    return results
