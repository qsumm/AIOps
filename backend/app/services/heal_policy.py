# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""自动自愈策略服务（D140 用户需求）。

用户场景（原话）：
  1. 集群平均 CPU 超阈值 → 告警触发 → 自动处理
  2. 数据库连接数满 / CPU>90% → 告警触发 → 自动 kill 进程
  3. 磁盘超 90% → 告警触发 → 自动清理/扩容

实现立场（不做玩具）：
  · **复用告警引擎的事件流**，不自建轮询——告警引擎已经把指标评估、防抖（for_duration）、
    静默、分级都做对了，自愈策略只需要「订阅告警事件」。
  · **复用 svc heal 的动作白名单与护栏**（heal.ACTIONS + evaluate_guards）——
    restart_service / restart_container / reconnect_pool / rotate_logs 四个白名单动作、
    九条护栏、限流熔断全部照走。不自造动作 = 不自造事故。
  · **三档语义**与人工 heal 一致：suggest=只评估留痕 / manual=留痕待人工确认 /
    auto=护栏通过即下发。护栏拒绝就拒绝（denied 留痕），绝不绕过。
  · **冷却**：同一策略 cooldown_min 内不重复触发（防"自愈→告警→自愈"循环）。
  · 诚实边界：k8s 扩容、云盘扩容需要对接云 API，当前动作白名单没有——这类场景
    用 reconnect_pool/rotate_logs 缓解或走后续轮次，不假装能做。
"""
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.db.session import SessionLocal

from app.models.alert import AlertRule
from app.models.host import Host
from app.models.ops import HealPolicy, HealPolicyRun
from app.models.svc import Service

log = logging.getLogger("heal_policy")

# 触发值：事件里带的指标读数（trigger_value），解析失败按 None 处理
def _parse_value(v) -> float | None:
    try:
        return float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _metric_matches(policy: HealPolicy, ev) -> bool:
    """直填指标的比对：metric 名匹配（前缀兼容，disk.percent~disk.percent./）且比较成立。"""
    if policy.trigger_rule_id:
        return True   # 关联规则的策略由「事件来自该规则」判定，不走这里
    m = (ev.metric or "").strip()
    t = (policy.trigger_metric or "").strip()
    if not t:
        return False
    if m != t and not m.startswith(t.rstrip(".")):
        return False
    val = _parse_value(ev.trigger_value)
    if val is None:
        return False
    if (policy.trigger_op or ">=") == "<=":
        return val <= policy.trigger_threshold
    return val >= policy.trigger_threshold


async def _pick_service(db, policy: HealPolicy, host_ident: str):
    if policy.service_id:
        s = await db.get(Service, policy.service_id)
        if s is None:
            return None, f"策略指定的服务 id={policy.service_id} 不存在"
        return s, ""
    host = (await db.execute(select(Host).where(Host.ident == host_ident))).scalar_one_or_none()
    if host is None:
        return None, f"告警主机 {host_ident} 不在主机清单中"
    svc = (await db.execute(
        select(Service).where(Service.host_id == host.id).limit(1))).scalar_one_or_none()
    if svc is None:
        return None, (f"主机 {host.name} 上没有已登记服务——动作需要服务声明"
                      "（systemd 单元/容器名）才能安全下发；请到「服务监控」登记或在该策略里指定服务")
    return svc, ""


async def evaluate_for_event(db, *, ev, rule) -> None:
    """告警事件到达时的策略评估入口（引擎触发，异常由调用方兜底）。

    流程：找启用策略 → 冷却检查 → 触发条件匹配 → 护栏评估 → 按档位执行/留痕。
    任何一步不满足都**留痕**（HealPolicyRun）——自愈不是黑盒。
    """
    now = datetime.utcnow()
    policies = (await db.execute(
        select(HealPolicy).where(HealPolicy.enabled.is_(True)))).scalars().all()
    if not policies:
        return

    host = (await db.execute(select(Host).where(Host.ident == ev.host_ident))).scalar_one_or_none()
    host_name = host.name if host else ev.host_ident

    for pol in policies:
        # ── 触发条件 ──
        # 关联规则的策略由「本规则的告警事件」驱动；直填指标的策略由**预测通道**独占驱动
        # （predict_loop）——两条通道不交叉，否则同一策略会被重复触发/重复留痕。
        if not pol.trigger_rule_id:
            continue
        if not rule or rule.id != pol.trigger_rule_id:
            continue

        # ── 冷却 ──
        if pol.last_fired_at and now < pol.last_fired_at + timedelta(minutes=pol.cooldown_min or 0):
            continue

        val = _parse_value(ev.trigger_value)
        base = {"alert_hash": ev.hash, "host_ident": ev.host_ident,
                "metric": ev.metric, "metric_value": val,
                "action": pol.action, "mode": pol.mode}
        await _execute_policy(db, pol=pol, base=base, host_name=host_name)




async def _execute_policy(db, *, pol: HealPolicy, base: dict, host_name: str,
                          predict_info: dict | None = None) -> None:
    """护栏评估 + 三档执行（告警触发与预测触发共用；predict_info 非空 = 预测触发）。"""
    from app.services import heal

    if pol.action not in heal.ACTIONS:
        await _record(db, pol, {**base, "status": "denied",
                                "guard_rule": "whitelist",
                                "guard_reason": f"动作不在白名单：{pol.action!r}"})
        return

    svc, why = await _pick_service(db, pol, base["host_ident"])
    if svc is None:
        await _record(db, pol, {**base, "status": "denied",
                                "guard_rule": "no_service", "guard_reason": why})
        return

    upstream_ok = await heal.check_upstream(db, base["host_ident"])
    facts = await heal.collect_facts(
        db, service_id=svc.id, host_id=svc.host_id,
        kill_switch=bool(getattr(__import__("app.core.config", fromlist=["get_settings"]).get_settings(), "heal_kill_switch", False)),
        upstream_ok=upstream_ok)
    target, target_err = heal.resolve_target(pol.action, svc.spec_json or {})
    verdict = heal.evaluate_guards(
        action=pol.action, service=svc, guard=facts["guard"],
        recent_heals=facts["recent_heals"], host_tasks_last_hour=facts["host_tasks_last_hour"],
        upstream_ok=facts["upstream_ok"], active_runbooks=facts["active_runbooks"],
        kill_switch=facts["kill_switch"], idle_confirmed=(svc.status == "up"),
        dry_run=(pol.mode == "suggest"), target_error=target_err)

    if pol.mode == "suggest":
        detail = {"verdict": verdict, "service": svc.name,
                  "action_label": heal.ACTIONS[pol.action]["label"],
                  "note": "仅建议：护栏已评估，未下发执行"}
        if predict_info:
            detail["predict_note"] = predict_info["predict_note"]
        await _record(db, pol, {**base, "status": "dry_run",
                                "guard_rule": verdict.get("rule", ""),
                                "guard_reason": verdict.get("reason", ""), "detail": detail})
        # 预测触发时额外全渠道通知（会喊人）
        if predict_info:
            await _notify_predict(db, pol, predict_info)
        return

    if not verdict.get("allow"):
        await _record(db, pol, {**base, "status": "denied",
                                "guard_rule": verdict.get("rule", ""),
                                "guard_reason": verdict.get("reason", "")})
        return

    if pol.mode == "manual":
        detail = {"service": svc.id, "service_name": svc.name,
                  "action_label": heal.ACTIONS[pol.action]["label"],
                  "post_check": heal.ACTIONS[pol.action]["post"], "verdict": verdict}
        if predict_info:
            detail["predict_note"] = predict_info["predict_note"]
        await _record(db, pol, {**base, "status": "waiting_confirm",
                                "guard_rule": verdict.get("rule", ""),
                                "guard_reason": verdict.get("reason", ""), "detail": detail})
        if predict_info:
            await _notify_predict(db, pol, predict_info)
        return

    # auto 档：护栏已通过 → 下发
    run = await _record(db, pol, {**base, "status": "dispatched", "detail": {}})
    try:
        task_id = await _dispatch(db, pol=pol, svc=svc, host_name=host_name)
        run.task_id = task_id
        run.detail = {"task_id": task_id, "service": svc.name,
                      "action_label": heal.ACTIONS[pol.action]["label"],
                      "post_check": heal.ACTIONS[pol.action]["post"]}
        if predict_info:
            run.detail["predict_note"] = predict_info["predict_note"]
            await _notify_predict(db, pol, predict_info)
    except Exception as e:  # noqa: BLE001
        run.status = "failed"
        run.guard_reason = str(e)[:500]
        log.exception("自愈策略「%s」下发失败", pol.name)
    await db.commit()


async def _notify_predict(db, pol: HealPolicy, info: dict) -> None:
    """预测命中的全渠道通知（复用 D113 会喊人：熔断/台账/去重继承）。"""
    try:
        from app.services import notify as notify_svc
        await notify_svc.notify_attention(
            db, kind="heal_predict",
            title=f"预测预警：{info['metric']} 预计 {info['predict_min']} 分钟后"
                  f"{'跌破' if False else '越过'}阈值（策略：{pol.name}）",
            body=info["predict_note"] + " 请提前处理。",
            meta={"host": info["host_ident"]})
    except Exception:  # noqa: BLE001
        log.exception("预测通知发送失败（policy=%s）——留痕已在", pol.name)


async def _record(db, pol: HealPolicy, data: dict):
    """写执行留痕 + 更新策略状态（last_fired_at 只在真实触发时推进）。"""
    run = HealPolicyRun(
        policy_id=pol.id, policy_name=pol.name,
        alert_hash=data.get("alert_hash", ""), host_ident=data.get("host_ident", ""),
        metric=data.get("metric", ""), metric_value=data.get("metric_value"),
        action=data.get("action", ""), mode=data.get("mode", ""),
        status=data.get("status", "dry_run"),
        guard_rule=data.get("guard_rule", ""), guard_reason=data.get("guard_reason", ""),
        detail=data.get("detail") or {})
    db.add(run)
    pol.last_fired_at = datetime.utcnow()
    pol.last_status = run.status
    pol.last_detail = {"guard_reason": run.guard_reason,
                       "detail": run.detail, "run_id": None}
    await db.commit()
    await db.refresh(run)
    return run


async def _dispatch(db, *, pol: HealPolicy, svc, host_name: str) -> int:
    """auto 档下发：与 svc.py heal 端点同一条执行路径（任务通道 + 后置校验）。"""
    from app.services import heal
    from app.routers.tasks import enqueue

    spec = svc.spec_json or {}
    target, target_err = heal.resolve_target(pol.action, spec)
    payload = {"action": pol.action, "service": target,
               "service_id": svc.id, "mode": heal.ACTIONS[pol.action]["mode"]}
    payload["_post"] = {"mode": heal.ACTIONS[pol.action]["mode"], "port": spec.get("port") or 0}
    host = await db.get(Host, svc.host_id)
    t = await enqueue(db, host, "svc_action", payload,
                      origin=f"heal_policy:{pol.id}", created_by=f"heal_policy:{pol.name}")
    return t.id


async def confirm_run(db, *, run_id: int, user) -> dict:
    """manual 档的「人工确认执行」：取 waiting_confirm 的留痕，走同一护栏+下发路径。

    确认时护栏**重新评估**（不是照旧结论放行）——留痕到执行之间世界可能已经变了
    （服务恢复了/有人先手工重启了），拿旧结论放行等于绕护栏。
    """
    run = await db.get(HealPolicyRun, run_id)
    if run is None:
        raise ValueError("执行留痕不存在")
    if run.status != "waiting_confirm":
        raise ValueError(f"该留痕状态为 {run.status}，不是待确认")
    pol = await db.get(HealPolicy, run.policy_id)
    if pol is None or not pol.enabled:
        raise ValueError("策略已删除或停用")
    if pol.mode != "manual":
        raise ValueError("仅「人工确认」档的策略可以确认执行")

    from app.services import heal
    svc = await db.get(Service, run.detail.get("service") or 0) if run.detail else None
    if svc is None:
        svc_rows = (await db.execute(
            select(Service).where(Service.host_ident == run.host_ident).limit(1))).scalars().all()
        svc = svc_rows[0] if svc_rows else None
    if svc is None:
        raise ValueError("原目标服务已不存在，无法确认执行")

    upstream_ok = await heal.check_upstream(db, run.host_ident)
    facts = await heal.collect_facts(
        db, service_id=svc.id, host_id=svc.host_id,
        kill_switch=bool(getattr(__import__("app.core.config", fromlist=["get_settings"]).get_settings(), "heal_kill_switch", False)),
        upstream_ok=upstream_ok)
    target, target_err = heal.resolve_target(run.action, svc.spec_json or {})
    verdict = heal.evaluate_guards(
        action=run.action, service=svc, guard=facts["guard"],
        recent_heals=facts["recent_heals"], host_tasks_last_hour=facts["host_tasks_last_hour"],
        upstream_ok=facts["upstream_ok"], active_runbooks=facts["active_runbooks"],
        kill_switch=facts["kill_switch"], idle_confirmed=(svc.status == "up"),
        dry_run=False, target_error=target_err)
    if not verdict.get("allow"):
        run.status = "denied"
        run.guard_rule = verdict.get("rule", "")
        run.guard_reason = verdict.get("reason", "")
        await db.commit()
        raise ValueError(f"确认时护栏拒绝（{verdict.get('rule')}）：{verdict.get('reason')}")

    # D160：原先这里多传了一个 _dispatch 签名里并不存在的 ev 关键字 → 每次人工确认都
    # TypeError → **人工确认档自愈 100% 500**（真机验收发现：确认执行直接 500）。
    task_id = await _dispatch(db, pol=pol, svc=svc, host_name=run.host_ident)
    run.status = "dispatched"
    run.task_id = task_id
    run.detail = {**(run.detail or {}), "task_id": task_id, "confirmed_by": user.username,
                  "service": svc.name}
    await db.commit()
    return {"task_id": task_id, "service": svc.name,
            "action_label": heal.ACTIONS[run.action]["label"]}

# ─────────────────────────────────────────────────────────────
# AI 阈值推荐（D142 · 用户原话：「阈值都是我看历史数据拍脑门定的阈值，
# 其实可以用AI推测出一个比较合适的」）
#
# 立场：这里的"AI"是**可解释的统计推断**（分位数 + 趋势斜率），不是黑盒大模型——
# 运维要的是「为什么是这个数」，给得出依据的推荐才敢用于自动自愈。
# 数据源：metric_agg 1m 粒度（与告警引擎同源，历史 7 天）。
# ─────────────────────────────────────────────────────────────
def _pct(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（q∈[0,1]）。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _trend_slope_per_hour(pts: list[tuple[datetime, float]]) -> float | None:
    """最小二乘斜率（单位/小时）；样本 <10 或时间跨度 <1h 返回 None（不可信）。"""
    if len(pts) < 10:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in pts]
    ys = [v for _, v in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom < 1e-9:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


async def recommend_threshold(db, *, metric: str, op: str = ">=", days: int = 7) -> dict:
    """基于全部主机的历史数据推荐阈值（策略是全主机触发的，推荐也按全主机聚合）。

    返回：样本量 / 分位数 / 峰值 / 趋势斜率 / 推荐 warn(critical 触发用) 阈值 + 人话依据。
    op=">="（超过型，如 CPU）用上尾分位数；op="<="（低于型，如剩余空间）用下尾。
    """
    days = max(1, min(int(days), 30))
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    rows = (await db.execute(sa_text(
        "SELECT value_avg FROM metric_agg WHERE metric=:m AND ts BETWEEN :s AND :e"),
        {"m": metric, "s": start, "e": end})).all()
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < 60:
        return {"ok": False, "reason": (
            f"指标 {metric} 近 {days} 天只有 {len(vals)} 个数据点（需要 ≥60）——"
            "可能是指标名不对、主机刚接入或采集时间太短。先确认「主机详情」里有该指标的历史曲线。")}
    vals.sort()
    p50, p90, p95, p99 = (_pct(vals, q) for q in (0.50, 0.90, 0.95, 0.99))
    peak, trough = vals[-1], vals[0]

    # 趋势（1m 粒度直接用，最多取最近 7 天 × 1440 点做回归，控制算力）
    tr_rows = (await db.execute(sa_text(
        "SELECT ts, value_avg FROM metric_agg WHERE metric=:m AND ts BETWEEN :s AND :e ORDER BY ts"),
        {"m": metric, "s": start, "e": end})).all()
    slope = _trend_slope_per_hour([(r[0], float(r[1])) for r in tr_rows if r[1] is not None])

    if op == "<=":
        # 低于型（如剩余空间、连接余量）：正常水位在高位，异常是"跌破"
        p05, p01 = _pct(vals, 0.05), _pct(vals, 0.01)
        recommended = round(max(p05 * 0.85, trough), 1)
        warn = round(max(p10 := _pct(vals, 0.10), recommended * 1.15), 1)
        rationale = (
            f"近 {days} 天 {len(vals)} 个数据点：正常水位 5% 分位={_pct(vals,0.05):.1f}、"
            f"最低={trough:.1f}。推荐触发阈值 {recommended}（跌破 5% 分位再留 15% 余量）——"
            "低于它说明水位已经跌穿历史正常区间。")
        extra = {"p05": round(p05, 1), "p01": round(p01, 1)}
    else:
        # 超过型（如 CPU/内存/磁盘占用）：正常水位在低位，异常是"冲破"。
        # 基准 = p99*1.05（99% 分位再留 5% 余量）；历史峰值明显更高时压到峰值之下
        # （超过峰值 = "从未发生过"的数）。极平分布（峰值≈p95，历史从未越线）时
        # 峰值 cap 不适用——任何触发阈值都高于全部历史值，此时推荐 p99*1.05 并明说。
        recommended = max(p99 * 1.05, p95 + 2)
        if peak > recommended * 1.02:
            recommended = min(recommended, peak * 0.98)
        recommended = round(max(recommended, p95), 1)   # 永远不低于 p95（否则天天误触发）
        # 文案口径统一：「95% 分位」就是正常水位上限，不再用"正常水位"指代别的分位数
        rationale = (
            f"近 {days} 天 {len(vals)} 个数据点：95% 分位（正常水位上限）={p95:.1f}、"
            f"99% 分位={p99:.1f}、历史峰值={peak:.1f}。推荐触发阈值 {recommended}"
            + ("——高于 99% 分位且低于历史峰值：超过它说明冲出历史正常区间。" if peak > recommended * 1.02
               else "——历史峰值即 99% 分位（高位长驻），本值已高于全部历史观测："
                    "首次触发后请人工确认阈值是否合理。"))
        extra = {"p95": round(p95, 1), "p99": round(p99, 1)}

    trend_note = ""
    if slope is not None:
        arrow = "上升" if slope > 0 else "下降"
        trend_note = f"趋势：近 {days} 天整体每小时的{arrow}斜率约 {abs(slope):.2f}"
        if abs(slope) > 0.5:
            trend_note += "（趋势明显，建议同时关注根因而不仅是阈值）"
    return {"ok": True, "metric": metric, "days": days, "samples": len(vals),
            "p50": round(p50, 1), "p90": round(p90, 1), "p95": round(p95, 1),
            "p99": round(p99, 1), "peak": round(peak, 1), "trough": round(trough, 1),
            "recommended": recommended, "rationale": rationale, "trend": trend_note, **extra}


# ─────────────────────────────────────────────────────────────
# 预测型自愈（D143 · 用户原话：「目前这些都是出问题后的补救措施，
# 那是不是可以用AI提前预测，提前处理？」）
#
# 立场（第一版刻意保守）：线性趋势外推 + **只通知不动手**——
# 预测必然有误报（流量波动/定时任务都会造成假趋势），让自动动作跑在误报上
# 比错过真故障更危险。等用户用一段时间、确认预测命中率可接受，再谈"预测命中自动执行"。
# 数据：每主机 metric_agg 1m 最近 30 分钟；样本 ≥10 才外推。
# ─────────────────────────────────────────────────────────────
PREDICT_LOOKBACK_MIN = 30     # 回看窗口：30 分钟
PREDICT_MIN_POINTS = 10       # 最少样本：少于 10 个 1m 点不外推（趋势不可信）


async def _history_points(db, ident: str, metric: str, minutes: int) -> list[tuple[datetime, float]]:
    # 注意：datetime 参数必须**截断微秒**——SeekDB 的 DATETIME(0) 列对带微秒的
    # 参数化比较会报 1210 Invalid argument（实测）。统一传整秒 ISO 字符串。
    end = datetime.utcnow().replace(microsecond=0)
    start = (end - timedelta(minutes=minutes)).replace(microsecond=0)
    rows = (await db.execute(sa_text(
        "SELECT ts, value_avg FROM metric_agg WHERE ident=:i AND metric=:m "
        "AND ts BETWEEN :s AND :e ORDER BY ts"),
        {"i": ident, "m": metric,
         "s": start.strftime("%Y-%m-%d %H:%M:%S"), "e": end.strftime("%Y-%m-%d %H:%M:%S")})).all()
    return [(r[0], float(r[1])) for r in rows if r[1] is not None]


async def predict_loop() -> None:
    """预测周期任务（sched 每 60s 调一次）：对开启预测的策略做趋势外推。

    流程：取策略 → 对每台上报该指标的主机取最近 30 分钟 1m 序列 → 最小二乘斜率 →
    外推 predict_min 分钟 → 预测值越过阈值 → 留痕(status=predicted) + 全渠道通知。
    冷却沿用策略 cooldown_min（按 last_fired_at），防止每个周期都重复喊。
    """
    from app.models.alert import NotificationChannel  # noqa: F401  (通知走 notify_attention)

    async with SessionLocal() as db:
        await _predict_loop(db)


async def _predict_loop(db) -> None:
    now = datetime.utcnow()
    policies = (await db.execute(
        select(HealPolicy).where(HealPolicy.enabled.is_(True),
                                 HealPolicy.predict_min > 0))).scalars().all()
    for pol in policies:
        if pol.trigger_rule_id:
            continue          # 挂规则的策略由告警驱动（规则自己会触发），预测只对直填指标开放
        # 冷却：从 DB 重读最新 last_fired_at（循环快照可能在同轮被 _record 推进后过期）
        await db.refresh(pol)
        if pol.last_fired_at and now < pol.last_fired_at + timedelta(minutes=pol.cooldown_min or 0):
            continue
        metric = (pol.trigger_metric or "").strip()
        if not metric:
            continue
        # 主机清单：报告过该指标的主机（metric_agg 有数据即算）
        idents = (await db.execute(sa_text(
            "SELECT DISTINCT ident FROM metric_agg WHERE metric=:m "
            "AND ts >= :s"), {"m": metric,
            "s": (now - timedelta(minutes=PREDICT_LOOKBACK_MIN * 2)).strftime("%Y-%m-%d %H:%M:%S")})).all()
        for (ident,) in idents:
            # 轮内冷却：任一主机命中并留痕后，本轮不再处理其它主机（策略级冷却）
            await db.refresh(pol)
            if pol.last_fired_at and datetime.utcnow() < pol.last_fired_at + timedelta(minutes=pol.cooldown_min or 0):
                break
            pts = await _history_points(db, ident, metric, PREDICT_LOOKBACK_MIN)
            if len(pts) < PREDICT_MIN_POINTS:
                continue
            slope = _trend_slope_per_hour(pts)
            if slope is None:
                continue
            slope_per_min = slope / 60.0
            last_ts, last_v = pts[-1]
            predicted = last_v + slope_per_min * pol.predict_min
            hit = (predicted <= pol.trigger_threshold) if (pol.trigger_op or ">=") == "<=" \
                else (predicted >= pol.trigger_threshold)
            if not hit:
                continue
            # 冷却（预测通知与触发留痕共用 last_fired_at，防每分钟重复喊）
            await _record(db, pol, {
                "status": "predicted",
                "alert_hash": f"predict:{pol.id}:{ident}:{now.strftime('%Y%m%d%H')}",
                "host_ident": ident, "metric": metric, "metric_value": round(last_v, 1),
                "action": pol.action, "mode": pol.mode,
                "guard_reason": (
                    f"预测命中：{metric} 当前 {last_v:.1f}，按近 {PREDICT_LOOKBACK_MIN} 分钟趋势"
                    f"（每小时 {slope:+.2f}）外推 {pol.predict_min} 分钟后约 {predicted:.1f}，"
                    f"将{'跌破' if pol.trigger_op == '<=' else '超过'}阈值 {pol.trigger_threshold}。"
                    "预测只通知不动手——请人工确认后再决定是否执行。"),
                "detail": {"predicted": round(predicted, 1), "predict_min": pol.predict_min,
                           "slope_per_hour": round(slope, 3), "lookback_min": PREDICT_LOOKBACK_MIN,
                           "predict_note": (
                               f"预测命中：{metric} 当前 {last_v:.1f}，按近 {PREDICT_LOOKBACK_MIN} 分钟趋势"
                               f"（每小时 {slope:+.2f}）外推 {pol.predict_min} 分钟后约 {predicted:.1f}，"
                               f"将{'跌破' if pol.trigger_op == '<=' else '超过'}阈值 {pol.trigger_threshold}。"
                               "预测命中后按档位处理：suggest 留痕/ manual 待确认/ auto 护栏通过即下发。")},
            })
            # 全渠道通知（复用 D113「会喊人」：熔断/台账/去重全继承）
            try:
                from app.services import notify as notify_svc
                await notify_svc.notify_attention(
                    db, kind="heal_predict",
                    title=f"预测预警：{metric} 预计 {pol.predict_min} 分钟后{'跌破' if pol.trigger_op == '<=' else '超过'} {pol.trigger_threshold}",
                    body=(f"主机 {ident[:10]}… 指标 {metric} 当前 {last_v:.1f}，趋势外推 "
                          f"{pol.predict_min} 分钟后约 {predicted:.1f}。"
                          f"关联自愈策略「{pol.name}」（动作：{pol.action}），请提前处理。"),
                    meta={"host": ident})
            except Exception:  # noqa: BLE001
                log.exception("预测通知发送失败（policy=%s ident=%s）——留痕已在", pol.name, ident)
