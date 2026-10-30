# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""告警引擎：对账调度 + 规则 worker + 事件消费（FR-C1/C2/C7；03 §1.5/§1.6）。"""
import asyncio
import copy
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.engine import detect as detect_mod
from app.engine.state import Ev, StateMachine, effective_now
from app.models.alert import AlertCurEvent, AlertHisEvent, AlertMute, AlertRule
from app.models.host import Host
from app.services import alert_freshness as freshness
from app.services import mute as mute_svc
from app.services import notify as notify_svc
from app.store import ring

log = logging.getLogger("engine")

RECONCILE_S = 9  # 对账周期（夜莺同款）
# 自监控：对账循环的心跳与异常计数（03 §1.6 的"平台自监控"落地；/dev/engine-state 可查）
_last_reconcile: datetime | None = None
_reconcile_count = 0
_reconcile_errors = 0
event_queue: asyncio.Queue[Ev] = asyncio.Queue(maxsize=100_000)

# 告警 SSE 广播：订阅者各一条队列
_sse_subs: set[asyncio.Queue] = set()


async def broadcast(kind: str, ev: Ev) -> None:
    payload = {
        "kind": kind,
        "hash": ev.hash, "rule_id": ev.rule_id, "rule_name": ev.rule_name,
        "host_ident": ev.host_ident, "metric": ev.metric, "severity": ev.severity,
        "trigger_value": ev.trigger_value, "is_recovered": ev.is_recovered,
        "trigger_time": ev.trigger_time.isoformat(), "first_trigger_time": ev.first_trigger_time.isoformat(),
        "tags": ev.tags,
    }
    dead = []
    for q in _sse_subs:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_subs.discard(q)


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _sse_subs.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _sse_subs.discard(q)


# ---------------- Worker ----------------
def next_rule_delay(rule, now: datetime | None = None) -> float:
    """规则下一次评估前要睡多久（秒）。

    - `cron_pattern` 非空 → 按 cron 求下次触发时刻（FR-C1「评估间隔（秒）或 cron」）；
      非法 cron 表达式回落到 eval_interval_s，并留一条 warning（不影响告警主流程）。
    - 否则 → eval_interval_s（下限 5s，防抖）。
    """
    interval = max(int(getattr(rule, "eval_interval_s", 15) or 15), 5)
    cron = (getattr(rule, "cron_pattern", "") or "").strip()
    if not cron:
        return float(interval)
    try:
        from apscheduler.triggers.cron import CronTrigger

        now = now or datetime.utcnow()
        nxt = CronTrigger.from_crontab(cron).get_next_fire_time(None, now)
        if nxt is None:
            return float(interval)
        delta = (nxt.replace(tzinfo=None) - now).total_seconds()
        return max(delta, 1.0)
    except Exception:  # noqa: BLE001 —— 表达式非法就退回固定间隔
        log.warning("规则 %s 的 cron 表达式无效（%s），回退固定间隔 %ss", getattr(rule, "id", "?"), cron, interval)
        return float(interval)


class RuleWorker:
    """每条启用规则一个：按 eval_interval 评估 → 状态机 → 入队。"""

    def __init__(self, rule: AlertRule):
        self.rule = rule
        self.sm = StateMachine({
            "id": rule.id, "name": rule.name, "severity": rule.severity,
            "for_duration_s": rule.for_duration_s, "recover_duration_s": rule.recover_duration_s,
            "query": f"{rule.scope}:{rule.metric}:{rule.algorithm}:{rule.expr}:{';'.join(rule.queries_json or [])}",
            "append_tags": rule.append_tags or {},
        })
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._sm_by_hash: dict[str, FireStateLike] = {}
        self.eval_error_streak = 0  # 03 §1.6：连续 eval_error 计数（达 3 触发平台自监控告警）

    async def start(self) -> None:
        await self._replay()
        self.task = asyncio.create_task(self._loop(), name=f"rule-{self.rule.id}")

    async def stop(self) -> None:
        self._stop.set()
        if self.task:
            self.task.cancel()

    async def _replay(self) -> None:
        """重启回放：该规则的活跃事件装回状态机（FR-C2 验收）。"""
        async with SessionLocal() as db:
            rows = (await db.execute(select(AlertCurEvent).where(
                AlertCurEvent.rule_id == self.rule.id, AlertCurEvent.stale.is_(False)))).scalars().all()
        self.sm.replay([
            {"hash": r.hash, "rule_id": r.rule_id, "rule_name": r.rule_name,
             "host_ident": r.host_ident, "metric": r.metric, "severity": r.severity,
             "trigger_value": r.trigger_value, "tags": r.tags or {},
             "first_trigger_time": r.first_trigger_time, "trigger_time": r.trigger_time,
             "notify_cur_number": r.notify_cur_number}
            for r in rows
        ])
        if rows:
            log.info("规则 %s 回放活跃事件 %s 条", self.rule.id, len(rows))

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._eval_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("规则 %s 评估异常", self.rule.id)
            await asyncio.sleep(next_rule_delay(self.rule))

    async def _eval_once(self) -> None:
        now = datetime.utcnow()
        # D144：生效时段在 UI 上明确标注 UTC+8，按北京时间判定；其余计算仍用 UTC。
        # （此前直接拿 UTC 小时比较：中国用户填 09:00~18:00，实际要到北京时间 17:00 才生效）
        if self.rule.disabled or not effective_now(self.rule.effective_time or [], now + timedelta(hours=8)):
            # P0：规则不在生效时段/已停用 → 本轮不评估，活跃行必须标记陈旧，
            # 否则它们会永久停在 firing，被事件收敛和 AI 诊断当当前证据。
            await self._mark_own_stale("outside_effective_time" if not self.rule.disabled else "rule_disabled")
            return
        anomalies: list[tuple[dict, float]] = []
        if self.rule.scope == "metric":
            async with SessionLocal() as db:
                hosts = (await db.execute(select(Host))).scalars().all()
            window = [now - timedelta(seconds=self.rule.window_s), now]
            # D154（FR-424）：指标支持 Prometheus 风格的维度选择器 `metric{k=v}`。
            # 带维度 → 每条序列**单独评估**（各自成事件、各自恢复）；不带 → 老行为不变。
            # 通配与选择器可组合：`disk.*.percent{device=/dev/sda1}`
            base_metric, tag_filter = ring.parse_selector(self.rule.metric)
            wildcard = _has_wildcard(base_metric)
            for h in hosts:
                # D150：通配符（用户需求：一台虚机挂 10 块数据盘，不想配 10 条规则）。
                # 一个模式匹配多个实际指标，**每个实际指标单独成事件**
                # （anomaly 的 metric 用真实名字 → 指纹不同 → 分盘告警/分别恢复）。
                targets = ([base_metric] if not wildcard
                           else await _wildcard_metrics(h.ident, base_metric, *window,
                                                        include_agg=self.rule.window_s > 7200))
                for metric_name in targets:
                    if tag_filter is not None:
                        # 带维度：逐序列评估
                        for series_tags, spts in ring.query_series(
                                h.ident, metric_name, tag_filter, *window):
                            if not spts:
                                continue
                            for _labels, v in detect_mod.detect(
                                    self.rule.algorithm, self.rule.expr, spts):
                                anomalies.append(({"host": h.ident, "metric": metric_name,
                                                   **series_tags}, v))
                        continue
                    pts = ring.query_points(h.ident, metric_name, *window)
                    if self.rule.window_s > 120:
                        # ring 只覆盖最近 maxlen 点（默认 2h）；窗口更长时**前段样本不能丢**
                        # （否则 3σ 基线只按最近一段算，偏差大）。窗口未超 ring 覆盖时不触发
                        # 这次额外查询（绝大多数规则 window ≤ 2h，零成本）。
                        if not pts:
                            pts = await _agg_points(h.ident, metric_name, *window)
                        elif not ring.has_points_before(h.ident, metric_name, window[0]):
                            older = await _agg_points(h.ident, metric_name, window[0], pts[0][0])
                            if older:
                                pts = older + pts
                    for labels, v in detect_mod.detect(self.rule.algorithm, self.rule.expr, pts):
                        anomalies.append(({"host": h.ident, "metric": metric_name}, v))
        elif self.rule.scope == "probe":
            anomalies = await self._eval_probe_once()
        elif self.rule.scope == "service":
            anomalies = await self._eval_service_once()
        elif self.rule.scope == "prom":
            anomalies = await self._eval_prom_once(now)
            if anomalies is None:  # 数据源不可达：本轮跳过 process（不视作指纹消失，避免恢复误报）
                return
        events = self.sm.process(anomalies, now)
        # 本轮成功评估：刷新活跃行的 last_eval_at 并解除 stale（P0 新鲜度）。
        try:
            async with SessionLocal() as db:
                await freshness.touch_rule_evaluated(db, self.rule.id, now)
        except Exception:  # noqa: BLE001 —— 新鲜度维护失败不得影响告警链路
            log.exception("刷新告警新鲜度失败 rule=%s", self.rule.id)
        for ev in events:
            await event_queue.put(ev)

    async def _mark_own_stale(self, reason: str) -> None:
        try:
            async with SessionLocal() as db:
                n = await freshness.mark_rule_stale(db, self.rule.id, reason)
            if n:
                log.info("规则 %s 活跃告警标记陈旧：%s（%s 条）", self.rule.id, reason, n)
        except Exception:  # noqa: BLE001
            log.exception("标记告警陈旧失败 rule=%s", self.rule.id)

    async def _eval_service_once(self) -> list[tuple[dict, float]]:
        """服务域评估（FR-415，scope=service）。

        取值语义（与探活一致的"越大越坏"口径）：`up→0 / degraded→1 / down→2`，
        **unknown 不参与判定**——信息缺失不等于故障，拿它报警会造成假警。
        于是 expr 常见写法：`>0` 任何异常都报 / `>1` 只在不可用时报。

        `rule.metric` 留空 = 该主机的**全部**服务；填服务名则只看这一个。
        防抖交给规则的 `for_duration_s`（例如"持续 60s 仍异常"才触发）——
        对服务尤其重要：一次采集抖动不该叫人。
        """
        from app.models.svc import Service

        values = {"up": 0.0, "degraded": 1.0, "down": 2.0}
        async with SessionLocal() as db:
            q = (select(Service, Host.ident).join(Host, Service.host_id == Host.id)
                 .where(Service.enabled.is_(True)))
            if self.rule.metric:
                q = q.where(Service.name == self.rule.metric)
            rows = (await db.execute(q)).all()
            # D160：同探活域——服务名写错会让规则静默零命中，日志里点明。
            if not rows and self.rule.metric:
                log.warning("规则「%s」(id=%s) 的服务名「%s」在库中不存在 → 本轮零命中，"
                            "该规则不会触发（服务名请与 Agent 上报的服务清单一致，或留空=全部）",
                            getattr(self.rule, "name", ""), getattr(self.rule, "id", "?"), self.rule.metric)
        out: list[tuple[dict, float]] = []
        for svc, ident in rows:
            v = values.get(svc.status)
            if v is None:
                continue
            for labels, val in detect_mod.detect("threshold", self.rule.expr, [(None, v)]):
                out.append(({"host": ident, "metric": svc.name, "service_type": svc.type,
                             "service_status": svc.status, "note": svc.note or ""}, val))
        return out

    async def _eval_probe_once(self) -> list[tuple[dict, float]]:
        """探活域评估（FR-C1 scope=probe）。

        探活状态只落 Probe 表（不进内存环形），故此处直接读表而非读时序。
        取值语义（两条互不混淆的取值空间）：
        - **未指定探活名**（rule.metric 为空）＝"任意探活失败"：down→1 / up→0，expr ">0"。
          此时**排除 https-cert 类**——证书的取值是"剩余天数"，与 0/1 语义冲突
          （会把健康的证书判成异常），必须显式指名。
        - **显式指定探活名**：https-cert → 值 = 证书剩余天数（Agent 写入 last_latency_ms），
          expr "<30" 即"30 天内到期"；其余类型仍为 down→1 / up→0。
        """
        from app.models.collect import Probe

        async with SessionLocal() as db:
            q = select(Probe, Host.ident).join(Host, Probe.host_id == Host.id).where(Probe.enabled.is_(True))
            if self.rule.metric:
                q = q.where(Probe.name == self.rule.metric)
            else:
                q = q.where(Probe.type != "https-cert")  # 证书类须显式指名，避免语义混淆
            rows = (await db.execute(q)).all()
            # D160：探活名写错 → 零行 → 规则永不触发（静默失效最坏）。这里**不改变行为**
            # （避免存量脏数据一上线就爆一屏 S1），但必须让它在日志里看得见。
            if not rows and self.rule.metric:
                log.warning("规则「%s」(id=%s) 的探活名「%s」在库中不存在 → 本轮零命中，"
                            "该规则不会触发（探活名请与 Agent 上报的探活项一致，或留空=全部）",
                            getattr(self.rule, "name", ""), getattr(self.rule, "id", "?"), self.rule.metric)
        out: list[tuple[dict, float]] = []
        for probe, ident in rows:
            if probe.type == "https-cert" and self.rule.metric:
                value = float(probe.last_latency_ms if probe.last_latency_ms is not None else -1)
            else:
                value = 1.0 if probe.last_status == "down" else 0.0
            for labels, v in detect_mod.detect("threshold", self.rule.expr, [(None, value)]):
                out.append(({"host": ident, "metric": probe.name, "probe_type": probe.type,
                             "target": probe.target, "probe_status": probe.last_status}, v))
        return out

    async def _eval_prom_once(self, now: datetime) -> list[tuple[dict, float]] | None:
        """轨 B 评估（03 §1.6）：查 query_range → 同一 detect → 指纹链路。返回 None 表示数据源不可达。"""
        from app.models.prom import PromDatasource
        from app.services import prom as prom_svc

        async with SessionLocal() as db:
            ds = None
            if self.rule.datasource_id:
                ds = await db.get(PromDatasource, self.rule.datasource_id)
            if ds is None:
                ds = (await db.execute(
                    select(PromDatasource).where(PromDatasource.enabled.is_(True), PromDatasource.is_default.is_(True))
                )).scalars().first()
        if ds is None:
            self._note_eval_error("未配置可用 Prometheus 数据源")
            return None
        window = [now - timedelta(seconds=self.rule.window_s), now]
        series: list[tuple[dict, float]] = []
        try:
            for q in (self.rule.queries_json or []):
                series.extend(await prom_svc.query_range(ds, q, *window, step_s=max(self.rule.eval_interval_s, 15)))
        except prom_svc.PromUnreachable as e:
            self._note_eval_error(str(e))
            return None
        self.eval_error_streak = 0
        out: list[tuple[dict, float]] = []
        for labels, v in detect_mod.detect_labeled(self.rule.algorithm, self.rule.expr, series):
            out.append(({"host": labels.get("instance", labels.get("host", "")),
                         "metric": labels.get("__name__", self.rule.metric), **labels}, v))
        return out

    def _note_eval_error(self, detail: str) -> None:
        """03 §1.6：连续 3 次 eval_error → 平台自身告警'Prometheus 数据源不可达'。"""
        self.eval_error_streak += 1
        log.warning("规则 %s prom 评估失败(%s/3): %s", self.rule.id, self.eval_error_streak, detail)
        if self.eval_error_streak == 3:
            log.error("规则 %s：Prometheus 数据源连续 3 次不可达 → 平台自监控告警（detail=%s）", self.rule.id, detail)


def _has_wildcard(metric: str) -> bool:
    """指标名里是否含通配符（* ? [）。空串/普通名字走原快路径，零行为变化。"""
    return any(ch in (metric or "") for ch in "*?[")


def _metric_match(pattern: str, name: str) -> bool:
    """指标名通配匹配：**`*` 只匹配一段（不跨 `.`），`**` 才跨段**。

    为什么不用 fnmatch：`disk.*.percent` 若按 fnmatch 语义会连
    `disk.inode.root.percent`（inode 使用率）一起命中——用户想"所有磁盘使用率"，
    结果把 inode 也报出来，属于误报。段内通配才是指标名的自然语义：
      disk.*.percent    → disk.data.percent / disk.root.percent（不含 inode.*）
      disk.**.percent   → 含 disk.inode.data.percent
      disk.data*.percent → disk.data1.percent 等
    """
    import re

    regex, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                regex += ".*"
                i += 2
                continue
            regex += "[^.]*"
            i += 1
            continue
        if ch == "?":
            regex += "[^.]"
            i += 1
            continue
        regex += re.escape(ch)
        i += 1
    return re.fullmatch(regex, name) is not None


async def _wildcard_metrics(ident: str, pattern: str, start: datetime, end: datetime,
                            include_agg: bool = True) -> list[str]:
    """列出该主机在窗口内**实际存在**且匹配 pattern 的指标名。

    来源两条：内存 ring（最近 2h，绝大多数规则窗口够用）+ metric_agg（更早的历史）。
    `include_agg=False` 时只查 ring——常规窗口（≤2h）必须走这条快路径，
    否则每个通配规则每 30s 对每台主机做一次 DISTINCT 全表扫描，主机会多到拖垮引擎。
    上限 200：`*` 这种过宽模式不应把一次评估变成上千次查询（宁可少报，不能拖垮引擎）。
    """
    names = {m for m in ring.list_metric_names(ident) if _metric_match(pattern, m)}
    if include_agg and len(names) < 200:
        try:
            from sqlalchemy import text

            async with SessionLocal() as db:
                rows = (await db.execute(
                    text("SELECT DISTINCT metric FROM metric_agg WHERE ident=:i AND ts BETWEEN :s AND :e"),
                    {"i": ident, "s": start, "e": end},
                )).all()
            for (m,) in rows:
                if _metric_match(pattern, m):
                    names.add(m)
                if len(names) >= 200:
                    break
        except Exception:  # noqa: BLE001 —— 聚合表不可用时 ring 的名字仍可用
            pass
    return sorted(names)[:200]


async def _agg_points(ident: str, metric: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    from sqlalchemy import text

    async with SessionLocal() as db:
        rows = (await db.execute(
            text("SELECT ts, value_avg FROM metric_agg WHERE ident=:i AND metric=:m AND ts BETWEEN :s AND :e ORDER BY ts"),
            {"i": ident, "m": metric, "s": start, "e": end},
        )).all()
    return [(r[0], float(r[1])) for r in rows]


# ---------------- 对账调度器 ----------------
workers: dict[int, RuleWorker] = {}


async def reconcile() -> None:
    global _last_reconcile, _reconcile_count
    async with SessionLocal() as db:
        rules = (await db.execute(select(AlertRule).where(AlertRule.disabled.is_(False)))).scalars().all()
    expected = {r.id for r in rules}
    for rid in expected - set(workers):
        w = RuleWorker(next(r for r in rules if r.id == rid))
        workers[rid] = w
        await w.start()
        log.info("worker 启动: 规则 %s(%s)", rid, w.rule.name)
    for rid in set(workers) - expected:
        await workers.pop(rid).stop()
        # P0：规则停用/删除后 worker 不再评估，活跃行立即标记陈旧，
        # 不等全局 freshness 扫描（默认 300s），避免界面继续显示 firing。
        try:
            async with SessionLocal() as db:
                n = await freshness.mark_rule_stale(db, rid, "rule_disabled_or_deleted")
            log.info("worker 停止: 规则 %s；活跃行标记陈旧 %s 条", rid, n)
        except Exception:  # noqa: BLE001
            log.exception("停止 worker 后标记陈旧失败 rule=%s", rid)
    # 规则配置变更：重建（简化对账：比对关键字段）
    for r in rules:
        w = workers.get(r.id)
        if w and (w.rule.expr, w.rule.severity, w.rule.metric, w.rule.algorithm, w.rule.eval_interval_s,
                  w.rule.scope, tuple(w.rule.queries_json or []), w.rule.datasource_id) != (
            r.expr, r.severity, r.metric, r.algorithm, r.eval_interval_s,
            r.scope, tuple(r.queries_json or []), r.datasource_id
        ):
            await w.stop()
            nw = RuleWorker(r)
            workers[r.id] = nw
            await nw.start()
            log.info("worker 重建（配置变更）: 规则 %s", r.id)
        elif w:
            w.rule = r  # 引用刷新（通知参数等轻量变更直接生效）
    _last_reconcile = datetime.utcnow()
    _reconcile_count += 1


async def reconcile_loop() -> None:
    global _reconcile_errors
    while True:
        try:
            await reconcile()
            _reconcile_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            _reconcile_errors += 1
            log.exception("对账异常（连续 %s 次）", _reconcile_errors)
        await asyncio.sleep(RECONCILE_S)


def engine_state() -> dict:
    """引擎自检：worker 清单 / 队列 / 对账心跳。运维与排障用（03 §1.6 平台自监控）。"""
    now = datetime.utcnow()
    stale = None if _last_reconcile is None else (now - _last_reconcile).total_seconds()
    return {
        "reconcile": {"last_at": _last_reconcile.isoformat() + "Z" if _last_reconcile else None,
                      "age_s": stale, "count": _reconcile_count, "consecutive_errors": _reconcile_errors,
                      "period_s": RECONCILE_S,
                      "healthy": stale is not None and stale < RECONCILE_S * 3 and _reconcile_errors == 0},
        "workers": [{"rule_id": rid, "rule_name": w.rule.name,
                     "disabled": bool(getattr(w.rule, "disabled", False)),
                     "fires": len(w.sm.fires), "alive": bool(w.task and not w.task.done())}
                    for rid, w in sorted(workers.items())],
        "event_queue_size": event_queue.qsize(),
        "sse_subscribers": len(_sse_subs),
    }


# ---------------- 消费者：持久化 + mute + 通知 + 广播 ----------------
async def consume_one(ev: Ev) -> None:
    async with SessionLocal() as db:
        # mute 判定（恢复事件不判 mute）
        if not ev.is_recovered:
            mutes = (await db.execute(select(AlertMute))).scalars().all()
            m = mute_svc.is_muted(
                [{"disabled": x.disabled, "tag_filters": x.tag_filters, "severities": x.severities,
                  "btime": x.btime, "etime": x.etime, "periodic_mutes": x.periodic_mutes, "mute_type": x.mute_type}
                 for x in mutes],
                {"host": ev.host_ident, "metric": ev.metric, **(ev.tags or {})}, ev.severity,
            )
            if m == 0:  # MutedAll：事件不产生；并让状态机"忘记"该指纹
                # 否则指纹停留在 fires 里，静默解除后会一直走 update 分支、永不重新触发
                # （夜莺同语义：静默期事件不落库，静默结束的下一轮评估重新产生事件）
                w = workers.get(ev.rule_id)
                if w:
                    w.sm.fires.pop(ev.hash, None)
                    w.sm.pendings.pop(ev.hash, None)
                return
            if m == 1:  # NotifyOnly：照常落库，跳过通知
                is_new = await _persist(db, ev, muted_notify=True)
                if is_new:
                    _spawn_ai_summary(ev)
                await broadcast(_ev_kind(ev, is_new), ev)
                return

        rule = await db.get(AlertRule, ev.rule_id)
        rule_cfg = {"name": rule.name if rule else ev.rule_name,
                    "channels_json": (rule.channels_json if rule else []) or [],
                    "channels_by_level": (rule.channels_by_level if rule else None) or None,
                    "runbook_url": rule.runbook_url if rule else "",
                    "notify_repeat_step_min": rule.notify_repeat_step_min if rule else 0,
                    "notify_max_number": rule.notify_max_number if rule else 0,
                    "notify_recovered": rule.notify_recovered if rule else True,
                    "expr": rule.expr if rule else "", "severity": rule.severity if rule else ev.severity}

        st = workers.get(ev.rule_id).sm.fires.get(ev.hash) if ev.rule_id in workers else None
        # D167-1：恢复事件**不再无条件发**——「恢复通知」开关（notify_recovered）
        # 此前被 or 短路绕过（is_recovered=True 时根本不调 should_notify），
        # 用户关掉后恢复通知照发，开关形同虚设。
        if ev.is_recovered:
            do_notify = bool(rule_cfg.get("notify_recovered", True))
        else:
            do_notify = st is not None and notify_svc.should_notify(
                st, rule_cfg["notify_repeat_step_min"], rule_cfg["notify_max_number"],
                rule_cfg["notify_recovered"], ev.is_recovered,
            )
        sent = []
        if do_notify:
            sent = await notify_svc.dispatch(db, ev, st, rule_cfg)
            if st is not None and not ev.is_recovered:
                st.notify_count += 1
                st.last_sent = datetime.utcnow()

        # D140 自动自愈：告警触发后按策略评估（冷却/护栏/三档语义），独立 try 兜底——
        # 自愈出任何问题都不能影响告警主链路（通知/落库/广播）。
        try:
            from app.services import heal_policy
            await heal_policy.evaluate_for_event(db, ev=ev, rule=rule)
        except Exception:  # noqa: BLE001
            log.exception("自愈策略评估异常（event=%s）——已跳过，不影响告警链路", ev.hash)

        is_new = await _persist(db, ev, muted_notify=False, notified=bool(sent))
        if is_new:
            _spawn_ai_summary(ev)
        await broadcast(_ev_kind(ev, is_new), ev)

    # 收敛聚类（FR-D1）：对"新触发"的告警建/并入事件；稳态下的更新与恢复都不触发
    # （更新每个评估周期都会来一次，重复查一次库里已有事件纯属浪费）。
    #
    # 但**不能只看 is_new**（验收实测到的真问题）：is_new 只在告警行**首次插入**时为真，
    # 而 `alert_cur_events` 是持久表——引擎一重启（部署/升级/重启），所有"正在告警"的行
    # 就只会走更新分支，永远不再收敛：用户看到告警在列表里 firing，却**没有事件**，
    # 于是事件→诊断→取证→自愈整条链路对这场故障直接失效。
    # 修法：本进程内**第一次**处理该告警时（含重启后的首次）也补一次收敛。用进程内集合记
    # "已收敛过的指纹"，不用 `st is None`——因为 st 在 process() 生成事件时就已经写进状态机了，
    # 到消费端时它必然不为 None，拿它当判据等于没改（这一步我第一版就写错了）。
    # cluster_alert 按 source_key 幂等（已有开放事件则并入、不会新建），代价是一次事件查询。
    if not ev.is_recovered and (is_new or ev.hash not in _converged_hashes):
        try:
            from app.services import cluster as cluster_svc

            await cluster_svc.cluster_alert({
                "hash": ev.hash, "rule_name": ev.rule_name, "metric": ev.metric,
                "host_ident": ev.host_ident, "severity": ev.severity,
                "trigger_value": ev.trigger_value, "trigger_time": str(ev.trigger_time),
                "ai_summary": "",
            })
            _converged_hashes.add(ev.hash)
            if len(_converged_hashes) > 20000:      # 防长跑膨胀；清空后各补一次收敛（幂等）
                _converged_hashes.clear()
        except Exception:
            log.exception("收敛聚类失败（不影响告警链路）")


# 已收敛过的告警指纹（进程内）：见 consume_one 里"重启后补收敛"的说明。
# 只在本进程生命周期内有效——重启后为空，于是每条"正在告警"的告警都会补一次收敛（幂等）。
_converged_hashes: set[str] = set()

_AI_SUMMARY_CONCURRENCY = 2          # 摘要并发上限（避免打满 LLM 配额与连接）
_ai_summary_sem = asyncio.Semaphore(_AI_SUMMARY_CONCURRENCY)
AI_SUMMARY_SKIP_QUEUE_DEPTH = 50     # 事件队列积压超过该值 → 本轮摘要让路（见下）


def _spawn_ai_summary(ev: Ev) -> None:
    """把 AI 摘要丢到后台任务，**不阻塞告警热路径**（D9 实测修正）。

    起因：摘要原先在 `consume_one` 里 `await`，而它是真调 LLM（数秒级）。
    告警风暴时（100 台主机同时触发）消费者被 LLM 串行拖住，事件队列积压上千条，
    通知与 SSE 推送因此延迟到分钟级——**恰好在最需要通知的时候**。
    现在：落库→通知→广播先行，摘要异步补写；且队列积压时直接放弃摘要（降级）。
    """
    if event_queue.qsize() > AI_SUMMARY_SKIP_QUEUE_DEPTH:
        log.warning("事件队列积压 %s 条，跳过本轮 AI 摘要（让路给告警链路）", event_queue.qsize())
        return
    asyncio.create_task(_maybe_ai_summary(ev), name=f"ai-summary-{ev.hash[:8]}")


async def _maybe_ai_summary(ev: Ev) -> None:
    """FR-C6：事件首次落库时按规则开关触发一句话 AI 摘要（fast_model）。

    失败静默降级——**绝不因 LLM 不可用影响告警链路**（03 §3 降级总表）。
    自管会话（后台任务不能复用请求/消费链路的 session）。
    """
    async with _ai_summary_sem, SessionLocal() as db:
      try:
        from app.core.config import get_settings

        if not get_settings().alert_ai_summary_enabled:
            return
        rule = await db.get(AlertRule, ev.rule_id)
        if rule is not None and not rule.enable_ai_summary:
            return
        from app.services import prompts
        from app.services.llm_gateway import call_llm

        # D149：把规则**触发条件**也交给摘要——否则 7.9% 磁盘告警被写成"可能影响存储可用性"，
        # 用户看不出这是规则阈值写错（expr=>1）导致的，第一反应是平台误报。
        r = await call_llm(db, "summary", prompts.set_summary_prompt(
            {"rule_name": ev.rule_name, "host_ident": ev.host_ident, "metric": ev.metric,
             "trigger_value": ev.trigger_value, "severity": ev.severity,
             "rule_expr": (rule.expr if rule is not None else "")}), model_role="fast")
        text = (r.content or "").strip()[:500]
        if not text:
            return
        cur = await db.get(AlertCurEvent, ev.hash)
        if cur is not None:
            cur.ai_summary = text
            await db.commit()
        log.info("AI 摘要已写入 hash=%s：%s", ev.hash[:8], text[:60])
      except Exception as e:  # noqa: BLE001 —— 摘要失败不影响主流程
          log.warning("AI 摘要降级（不影响告警）：%s: %s", type(e).__name__, str(e)[:120])


def _ev_kind(ev: Ev, is_new: bool) -> str:
    """告警流事件名（FR-C7 规定三类）：首次触发 firing / 恢复 recovered / 活跃更新 updated。

    `updated` 用于"同指纹仍在告警、值或通知次数变了"——前端据此刷新列表而不新增一条。
    """
    if ev.is_recovered:
        return "recovered"
    return "firing" if is_new else "updated"


async def _persist(db, ev: Ev, muted_notify: bool, notified: bool = False) -> bool:
    """落库；返回是否**首次**落库（首次才触发 AI 摘要，控制成本，FR-C6）。"""
    is_new = False
    if ev.is_recovered:
        await db.execute(delete(AlertCurEvent).where(AlertCurEvent.hash == ev.hash))
        db.add(AlertHisEvent(
            hash=ev.hash, rule_id=ev.rule_id, rule_name=ev.rule_name, host_ident=ev.host_ident,
            metric=ev.metric, severity=ev.severity, trigger_time=ev.trigger_time,
            trigger_value=ev.trigger_value, tags=ev.tags,
            annotations=getattr(ev, "annotations", None) or {},   # D154：注解随事件留档
            is_recovered=True, recover_time=ev.recover_time,
        ))
    else:
        cur = await db.get(AlertCurEvent, ev.hash)
        if cur is None:
            is_new = True
            db.add(AlertCurEvent(
                hash=ev.hash, rule_id=ev.rule_id, rule_name=ev.rule_name,
                host_ident=ev.host_ident, metric=ev.metric, severity=ev.severity,
                trigger_time=ev.trigger_time, trigger_value=ev.trigger_value, tags=ev.tags,
                annotations=getattr(ev, "annotations", None) or {},   # D154：注解随事件留档
                first_trigger_time=ev.first_trigger_time, notify_cur_number=1,
                last_eval_at=datetime.utcnow(), stale=False, stale_reason="",
            ))
        else:
            cur.trigger_time = ev.trigger_time
            cur.trigger_value = ev.trigger_value
            cur.last_eval_at = datetime.utcnow()
            cur.stale = False
            cur.stale_reason = ""
            if notified:
                cur.notify_cur_number += 1
        # 账本只在"有意义的状态变化"时追加：首次触发 / 实际发送了通知 / 恢复。
        # 否则每个评估周期（15s×N 主机）都写一行 his，账本会被无意义的更新淹没。
        if is_new or notified:
            db.add(AlertHisEvent(
                hash=ev.hash, rule_id=ev.rule_id, rule_name=ev.rule_name, host_ident=ev.host_ident,
                metric=ev.metric, severity=ev.severity, trigger_time=ev.trigger_time,
                trigger_value=ev.trigger_value, tags=ev.tags, is_recovered=False,
            ))
    await db.commit()
    return is_new


async def consume_loop() -> None:
    while True:
        ev = await event_queue.get()
        try:
            await consume_one(ev)
        except Exception:
            log.exception("事件消费异常 hash=%s", getattr(ev, "hash", "?"))


def start_engine() -> None:
    asyncio.create_task(reconcile_loop(), name="reconcile")
    asyncio.create_task(consume_loop(), name="consumer")
    log.info("告警引擎启动：对账(%ss) + 消费者", RECONCILE_S)


class FireStateLike:  # 类型提示占位
    pass
