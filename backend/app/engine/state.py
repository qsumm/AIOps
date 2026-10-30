"""指纹状态机（FR-C2；03 §1.6，夜莺 process.go:797 同构）。

纯逻辑层：不碰 DB/asyncio，可独立单测。
- fingerprint = MD5(rule_id | 排序labels | severity | query)
- fires/pendings 内存态；入队前 deepcopy（夜莺踩坑教训）
- 触发/更新/恢复(留观)/重启回放 四态
"""
import copy
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def fingerprint(rule_id: int, labels: dict, severity: int, query: str) -> str:
    sorted_labels = ";".join(f"{k}={labels[k]}" for k in sorted(labels))
    return hashlib.md5(f"{rule_id}|{sorted_labels}|{severity}|{query}".encode()).hexdigest()


# D154：Pipeline 写出的注解（按指纹存，随事件一起落库）。
# 为什么单独存：注解**不参与**指纹（标签才参与）——所以它不能塞进 labels，
# 否则"当前值/影响面"这类会变的值会把同一故障拆成不同事件（夜莺的教训）。
_ANNOTATIONS: dict[str, dict] = {}


def _run_event_pipeline(pipelines, labels: dict) -> tuple[dict, dict, bool]:
    """执行告警规则级 Pipeline。返回 (新标签, 注解, 是否丢弃)。

    这里**不直接 import** services.pipeline（engine 是纯逻辑层、单测不碰 DB），
    故用一个极小的本地实现 + 委托：需要 enrich（查业务组）时才走 DB 路径。
    """
    if not pipelines:
        return labels, {}, False
    try:
        from app.services.pipeline import apply_pipeline
    except Exception:  # noqa: BLE001 —— 导入失败不得让规则评估崩
        return labels, {}, False
    try:
        ev, dropped = apply_pipeline(pipelines, {"tags": dict(labels or {})})
    except Exception:  # noqa: BLE001
        return labels, {}, False
    if dropped:
        return labels, {}, True
    return (ev.get("tags") or labels), (ev.get("annotations") or {}), False


@dataclass
class Ev:
    """告警事件（内存形态；持久化由 consumer 转 ORM）。"""
    hash: str
    rule_id: int
    rule_name: str
    host_ident: str
    metric: str
    severity: int
    trigger_value: float
    labels: dict
    tags: dict
    annotations: dict = field(default_factory=dict)   # D154：注解（不参与匹配，只进消息）
    is_recovered: bool = False
    is_update: bool = False          # 活跃事件的更新（非首次触发）：供消费端决定"是否重发通知"
    first_trigger_time: datetime = field(default_factory=lambda: datetime.utcnow().replace(microsecond=0))
    trigger_time: datetime = field(default_factory=lambda: datetime.utcnow().replace(microsecond=0))
    recover_time: datetime | None = None


@dataclass
class FireState:
    ev: Ev
    first_eval: datetime = field(default_factory=lambda: datetime.utcnow())
    last_eval: datetime = field(default_factory=lambda: datetime.utcnow())
    last_sent: datetime | None = None
    notify_count: int = 1


class StateMachine:
    """每条规则一个实例（worker 持有）。"""

    def __init__(self, rule_cfg: dict):
        # rule_cfg: {id, severity, for_duration_s, recover_duration_s, query}
        self.cfg = rule_cfg
        self.fires: dict[str, FireState] = {}
        self.pendings: dict[str, datetime] = {}  # hash → first_eval

    # ---- 主入口：本轮异常点列表（labels→value） ----
    def process(self, anomalies: list[tuple[dict, float]], now: datetime | None = None) -> list[Ev]:
        now = now or datetime.utcnow()
        # 一轮内同一指纹可能命中多个采样点：threshold 逐点返回窗口内**所有**越限点
        # （detect.py 的 threshold 分支），而取点窗口是 rule.window_s（默认 300~600s），
        # 于是一轮带进十几到几十个同指纹异常点。逐点入队＝消费端/SSE 收到同样的一批 updated
        # （D9 实测：一个 tick 刷 8~20 条一模一样的 updated，接真渠道就是告警风暴）。
        # 指纹才是规则的评估单位（同指纹即同 severity/query），故先按指纹收敛，保留最后一点
        # （ring 按到达顺序追加 ⇒ 最后一点＝最新取值）。
        merged: dict[str, tuple[dict, float]] = {}
        for labels, value in anomalies:
            # D154（FR-423）：告警规则级事件 Pipeline —— **在算指纹之前**跑。顺序很关键：
            # ① 改标签 → 标签进 fingerprint → 不同维度成**独立事件**（各自触发/恢复）；
            # ② drop   → 该异常点**不产生事件**（不落库、不通知）。
            # 放在这里也保证了 Pipeline 改写的标签能被**后面的屏蔽规则**匹配到
            # （夜莺的顺序铁律，见 ADR-003）。
            lbl, ann, dropped = _run_event_pipeline(self.cfg.get("event_pipelines"), labels)
            if dropped:
                continue
            h = fingerprint(self.cfg["id"], lbl, self.cfg["severity"], self.cfg["query"])
            merged[h] = (lbl, value)
            if ann:
                _ANNOTATIONS[h] = ann
        out: list[Ev] = []
        seen: set[str] = set()
        for h, (labels, value) in merged.items():
            seen.add(h)
            st = self.fires.get(h)
            if st is None:
                # for_duration 确认区
                if self.cfg.get("for_duration_s", 0) > 0:
                    first = self.pendings.setdefault(h, now)
                    if (now - first).total_seconds() < self.cfg["for_duration_s"]:
                        continue
                    self.pendings.pop(h, None)
                ev = Ev(
                    hash=h, rule_id=self.cfg["id"], rule_name=self.cfg.get("name", ""),
                    # 注意：这里用 merged 里的 labels（已含 Pipeline 改写），
                    # 不能用上一轮的 lbl（那是最后一个异常点的标签，会串味）
                    host_ident=labels.get("host", ""), metric=labels.get("metric", ""),
                    severity=self.cfg["severity"], trigger_value=value,
                    labels=labels, tags=dict(labels, **self.cfg.get("append_tags", {})),
                    annotations=dict(_ANNOTATIONS.get(h) or {}),
                    first_trigger_time=now, trigger_time=now,
                )
                self.fires[h] = FireState(ev=ev, first_eval=now, last_eval=now)
                out.append(copy.deepcopy(ev))  # ⚠ deepcopy 入队（夜莺注释坑）
            else:
                st.last_eval = now
                st.ev.trigger_value = value
                st.ev.trigger_time = now
                # 活跃事件也要入队：通知节奏（repeat_step/max_number）与告警流 updated 事件
                # 都在消费端判定，不把更新送进去 = 重复通知永远不会发生（D9 实测发现）。
                upd = copy.deepcopy(st.ev)
                upd.is_update = True
                out.append(upd)
        # 恢复判定：本轮缺失的指纹
        recover_s = self.cfg.get("recover_duration_s", 0)
        for h in [k for k in self.fires if k not in seen]:
            st = self.fires[h]
            if (now - st.last_eval).total_seconds() >= recover_s:
                ev = self.fires.pop(h).ev
                if _ANNOTATIONS.get(h):
                    ev.annotations = dict(_ANNOTATIONS[h])
                _ANNOTATIONS.pop(h, None)      # 内存卫生：事件恢复后不留注解
                ev.is_recovered = True
                ev.recover_time = now
                self.pendings.pop(h, None)
                out.append(copy.deepcopy(ev))
        return out

    # ---- 重启回放：从 cur 表恢复活跃态（不重发通知） ----
    def replay(self, cur_rows: list[dict]) -> None:
        for r in cur_rows:
            ev = Ev(
                hash=r["hash"], rule_id=r["rule_id"], rule_name=r.get("rule_name", ""),
                host_ident=r.get("host_ident", ""), metric=r.get("metric", ""),
                severity=r.get("severity", 2), trigger_value=r.get("trigger_value", 0.0),
                labels=r.get("tags", {}), tags=r.get("tags", {}),
                first_trigger_time=r.get("first_trigger_time", datetime.utcnow()),
                trigger_time=r.get("trigger_time", datetime.utcnow()),
            )
            st = FireState(ev=ev, notify_count=r.get("notify_cur_number", 1))
            st.last_sent = r.get("trigger_time")
            self.fires[ev.hash] = st


def effective_now(segments: list[dict], now: datetime | None = None) -> bool:
    """生效时段判定（FR-C1）：空=全天；段={days:[1-7], start:"09:00", end:"18:00"}。

    约定：`now` 必须是**展示时区的墙上时间**（调用方传入 UTC+8），本函数不做时区换算。
    days 兼容两种写法：含 0 → Python weekday（周一=0）；不含 0 → ISO / UI 写法（周一=1）。
    旧版 UI 写 1..7、后端按 0..6 比较，周日/周一会被错判——这里一次性兼容两种口径。
    跨零点时段（如 22:00~06:00）按「s > e 时跨夜」处理。
    """
    if not segments:
        return True
    now = now or datetime.utcnow()
    for seg in segments:
        days = seg.get("days")
        if days:
            # 口径判定：0 只出现在 Python weekday 写法里（ISO 周一=1）。
            # 旧 UI 写 1..7（ISO）而后端按 0..6 比较，周日/周一整体错位一天——
            # 含 0 → weekday；不含 0 → ISO。两种历史数据都能正确解释。
            days_i = [int(d) for d in days]
            wd = (now.weekday() if 0 in days_i else now.isoweekday())
            if wd not in days_i:
                continue
        h, m = now.hour, now.minute
        try:
            sh, sm = map(int, seg.get("start", "00:00").split(":"))
            eh, em = map(int, seg.get("end", "23:59").split(":"))
        except ValueError:
            continue
        cur, s, e = h * 60 + m, sh * 60 + sm, eh * 60 + em
        if (s <= cur <= e) if s <= e else (cur >= s or cur <= e):
            return True
    return False
