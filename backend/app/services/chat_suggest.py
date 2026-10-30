"""AI 助手建议问题（D47 实时生成 → D49 定时预生成 + 缓存 + 手动刷新）。

用户对交互的要求（原话要点）：「每次点对话框生成会有延迟，慢的话就看不到问题了」
+「后端定时每小时调一次……晚上 11 点到早上 8 点每 3 小时调一次」+「加一个刷新按钮立即更新」。

机制（单进程部署：调度器与 API 在同一个进程，故缓存用进程内字典即可）：
1. **定时预生成**：调度器定时（白天每小时；北京时间 23:00–08:00 只在 23/02/05 点）收集
   平台状态事实 → 调模型生成 4 个时效性问题 → 写入进程内缓存；
2. **读取零等待**：GET 建议接口只读缓存（毫秒级）——用户打开对话框永远不等模型；
3. **手动刷新**：POST /refresh 强制生成一次（30s 冷却，防连点烧 token）；
4. **省 token**：状态事实（含数值，随事实文本一起做指纹）不变时跳过模型调用；
   命中缓存但超过 3 小时则后台重生成（避免措辞僵死）；
5. **兜底**：模型未配置/失败 → 规则版（规则版同样贴实时状态），界面永远有内容。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("chat_suggest")

CACHE_TTL_S = 3 * 3600        # 状态未变时，模型版最多沿用 3 小时（之后后台重生成）
STALE_TTL_S = 24 * 3600       # 超过一天就别再展示旧模型版了（宁可用规则版）
BG_MIN_INTERVAL_S = 300       # 后台补生成最小间隔（多用户同时打开只补一次）
MANUAL_COOLDOWN_S = 30        # 手动刷新冷却
TZ_CN = timezone(timedelta(hours=8))   # 夜间窗口按北京时间（容器是 UTC，不能用 now().hour）

_CACHE: dict[str, dict] = {}         # vis_key → {"sig","items","headline","gen_ts"}
_LAST_BG = 0.0                       # 上次后台补生成时间戳
_LAST_MANUAL = 0.0                   # 上次手动刷新时间戳


def should_tick(now_utc: datetime | None = None) -> bool:
    """定时任务的"该不该跑"判定：白天每小时；夜间（北京 23:00–08:00）每 3 小时。

    夜间打点取 23/02/05 点（hour % 3 == 2），早上 8 点回到每小时。
    """
    now = now_utc or datetime.utcnow()
    h = (now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now).astimezone(TZ_CN).hour
    if 8 <= h <= 22:
        return True
    return h % 3 == 2


def _signature(facts: list[str]) -> str:
    return hashlib.sha1("|".join(facts).encode("utf-8")).hexdigest()[:16]


def _iso(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def headline_of(items: list[dict]) -> str:
    urgent = sum(1 for x in items if x.get("tone") == "urgent")
    warn = sum(1 for x in items if x.get("tone") == "warn")
    if urgent:
        return f"现在有 {urgent} 个紧急情况值得先看"
    if warn:
        return "当前有需要关注的情况，建议先看这些"
    if items and items[0].get("tone") == "ok":
        return "当前没有异常，可以随便问"
    return "这些是值得一看的问题"


async def collect_state(db, user) -> tuple[list[str], list[dict], str]:
    """收集当前平台状态的"候选事实" + 规则版建议。

    返回 (facts, rules, vis_key)。facts 是给模型看的人话状态；rules 是模型不可用时的兜底；
    vis_key 是数据可见域标识（demo 前缀隔离配置下不同用户可见的主机不同，缓存必须分域）。
    """
    from datetime import datetime as _dt
    from sqlalchemy import func, select

    from app.models.alert import AlertCurEvent, AlertHisEvent, AlertRule
    from app.models.event import Incident
    from app.models.host import Host
    from app.services import visibility

    facts: list[str] = []
    sug: list[dict] = []
    vis_key = "*"
    try:
        idents = await visibility.visible_host_idents(db, user)
        if idents is not None:
            vis_key = hashlib.sha1("|".join(sorted(idents)).encode("utf-8")).hexdigest()[:12]

        # ① 紧急/警告告警
        vc_a = await visibility.visible_fingerprints_filter(db, user, AlertCurEvent.host_ident)
        aq = (select(AlertCurEvent.severity, func.count()).group_by(AlertCurEvent.severity)
              .where(AlertCurEvent.stale.is_(False)))
        if vc_a is not None:
            aq = aq.where(vc_a)
        sev = {k: n for k, n in (await db.execute(aq)).all()}
        s1, s2 = sev.get(1, 0), sev.get(2, 0)
        if s1:
            facts.append(f"有 {s1} 条 S1 紧急告警未确认（另有 S2 {s2} 条）")
            sug += [{"q": f"这 {s1} 条紧急告警，哪条该先处理？", "why": f"当前 {s1} 条 S1 未确认", "tone": "urgent"},
                    {"q": "紧急告警集中在哪台主机？", "why": "先看影响面再定处置顺序", "tone": "urgent"}]
        elif s2:
            facts.append(f"有 {s2} 条 S2 警告告警")
            sug.append({"q": f"{s2} 条警告告警里，有需要立刻处理的吗？", "why": f"当前 {s2} 条 S2", "tone": "warn"})

        # ② 主机离线
        vc_h = await visibility.visible_fingerprints_filter(db, user, Host.ident)
        hq = select(Host.ident, Host.name, Host.status)
        if vc_h is not None:
            hq = hq.where(vc_h)
        hosts = (await db.execute(hq)).all()
        down = [n or i[:8] for i, n, st in hosts if st != "up"]
        if down:
            facts.append(f"纳管 {len(hosts)} 台主机，其中 {len(down)} 台离线：" + "、".join(down[:4]))
            sug += [{"q": f"{len(down)} 台主机离线，是哪些？", "why": "离线：" + "、".join(down[:3]), "tone": "warn"},
                    {"q": "这些离线主机是什么时候断的？", "why": "判断同时断还是逐个断", "tone": "warn"}]

        # ③ 逼近/越过阈值
        try:
            from app.engine.detect import _parse_expr
            from app.store import ring
            rules = (await db.execute(select(AlertRule).where(
                AlertRule.scope == "metric", AlertRule.disabled.is_(False)))).scalars().all()
            hot = []
            now = _dt.utcnow()
            for r in rules:
                if (r.algorithm or "threshold") != "threshold":
                    continue
                try:
                    op, thr = _parse_expr(r.expr)
                except Exception:
                    continue
                for ident in [i for i, _, _ in hosts]:
                    pts = ring.query_points(ident, r.metric, now - timedelta(minutes=5), now)
                    if not pts:
                        continue
                    v = pts[-1][1]
                    firing = (v > thr) if op == ">" else (v >= thr) if op == ">=" else (v < thr) if op == "<" else (v <= thr)
                    ratio = (v / thr) if (op in (">", ">=") and thr) else ((thr / v) if v else 0)
                    if firing or ratio >= 0.9:
                        nm = next((n or i[:8] for i, n, _ in hosts if i == ident), ident[:8])
                        hot.append(f"{nm} {r.metric} 现值 {round(v, 1)}（阈值 {op}{int(thr) if thr == int(thr) else thr}）")
            if hot:
                facts.append("指标已越阈或临界：" + "；".join(hot[:4]))
                sug.append({"q": f"{len(hot)} 项指标逼近或越过阈值，最紧的是哪个？",
                            "why": "；".join(hot[:2]), "tone": "warn"})
        except Exception:
            await db.rollback()   # 保持 session 干净，供后续使用

        # ④ 未诊断事件
        vc_i = await visibility.visible_fingerprints_filter(db, user, Incident.host_ident)
        iq = select(func.count()).select_from(Incident).where(Incident.status.in_(("open", "ack")))
        if vc_i is not None:
            iq = iq.where(vc_i)
        open_inc = (await db.execute(iq)).scalar() or 0
        dq = select(func.count()).select_from(Incident).where(
            Incident.ai_report_md == "", Incident.status.in_(("open", "ack")))
        if vc_i is not None:
            dq = dq.where(vc_i)
        undiag = (await db.execute(dq)).scalar() or 0
        if undiag:
            facts.append(f"{undiag} 个事件尚未诊断（共 {open_inc} 个未闭环）")
            sug.append({"q": f"{undiag} 个事件还没诊断，要我看哪个？", "why": "未诊断无法定根因", "tone": "info"})
        elif open_inc:
            facts.append(f"{open_inc} 个事件待处理/已确认")
            sug.append({"q": "还有哪些问题没闭环？", "why": f"{open_inc} 个未闭环", "tone": "info"})

        # ⑤ 近期恢复
        vc_hr = await visibility.visible_fingerprints_filter(db, user, AlertHisEvent.host_ident)
        rq = select(func.count()).select_from(AlertHisEvent).where(
            AlertHisEvent.is_recovered.is_(True), AlertHisEvent.recover_time >= _dt.utcnow() - timedelta(hours=24))
        if vc_hr is not None:
            rq = rq.where(vc_hr)
        rec = (await db.execute(rq)).scalar() or 0
        if rec:
            facts.append(f"过去 24 小时恢复了 {rec} 条告警")
            sug.append({"q": f"过去 24 小时恢复了 {rec} 条告警，最慢的那条为什么？",
                        "why": "已恢复的问题值得复盘", "tone": "info"})

        if not sug:
            facts.append("当前无异常：无未确认告警、无离线主机、无越阈指标、无未闭环事件")
            sug = [{"q": "当前整体状况怎么样？", "why": "需要先看总览", "tone": "ok"},
                   {"q": "今天的告警比昨天多还是少？", "why": "看趋势", "tone": "ok"},
                   {"q": "哪些主机资源用得最满？", "why": "容量视角", "tone": "ok"}]
    except Exception as e:  # noqa: BLE001 —— 状态收集失败不该让助手不可用
        log.warning("建议问题状态收集失败：%s", e)
        try:
            await db.rollback()
        except Exception:
            pass
    return facts, sug, vis_key


_SYS_PROMPT = (
    "你是运维监控平台的助手。下面给你**当前平台的真实状态**，"
    "生成 4 个运维人员此刻最可能想问的问题。\n"
    "要求：1) 必须紧扣给出的状态（带数字/主机名/指标名），不要通用问题；"
    "2) 口吻像运维同事问话，直接具体、问完就能去查；"
    "3) 每条配一句简短依据；"
    '4) 只输出 JSON 数组 [{"q":"问题","why":"依据","tone":"urgent|warn|info"}]，不要其它文字。'
)


async def model_suggestions(db, facts: list[str], quota_role: str = "engine") -> tuple[list[dict], str] | None:
    """调模型把事实转成问题。失败/输出不合规 → None（调用方保留规则版）。"""
    from app.services.llm_gateway import call_llm

    try:
        await db.rollback()   # 清干净此前查询可能留下的失败事务（D47 教训：否则写用量时炸）
        r = await call_llm(db, "suggest", [
            {"role": "system", "content": _SYS_PROMPT},
            {"role": "user", "content": "当前平台状态：\n- " + "\n- ".join(facts)},
        ], model_role="fast", quota_role=quota_role)
        txt = (r.content or "").strip()
        if "```" in txt:
            parts = txt.split("```")
            txt = parts[1].lstrip("json").strip() if len(parts) > 1 else txt
        a, b = txt.find("["), txt.rfind("]")
        if a < 0 or b <= a:
            return None
        raw = json.loads(txt[a:b + 1])
        clean = [{"q": str(x.get("q", "")).strip(), "why": str(x.get("why", "")).strip(),
                  "tone": x.get("tone") if x.get("tone") in ("urgent", "warn", "info", "ok") else "info"}
                 for x in raw if isinstance(x, dict) and x.get("q")]
        if len(clean) < 3:
            return None
        clean = clean[:6]
        return clean, headline_of(clean)
    except Exception as e:  # noqa: BLE001 —— 模型不可用/输出不合规 → 规则版兜底
        import traceback as _tb
        log.warning("建议问题 LLM 生成失败：%s\n%s", e, _tb.format_exc()[-1200:])
        try:
            await db.rollback()
        except Exception:
            pass
        return None


def _store(vis_key: str, facts: list[str], items: list[dict], headline: str) -> None:
    if len(_CACHE) > 16 and vis_key not in _CACHE:      # 可见域数量有限，做个保险上限
        oldest = min(_CACHE, key=lambda k: _CACHE[k]["gen_ts"])
        _CACHE.pop(oldest, None)
    _CACHE[vis_key] = {"sig": _signature(facts), "items": items,
                       "headline": headline, "gen_ts": time.time()}


def _kick_bg(vis_key: str, facts: list[str]) -> None:
    """后台补一次模型版（不阻塞请求；多用户同时打开只补一次）。"""
    global _LAST_BG
    if "PYTEST_CURRENT_TEST" in os.environ:
        return                       # 测试环境不真调模型（省 token、避免拖慢用例）
    now = time.time()
    if now - _LAST_BG < BG_MIN_INTERVAL_S:
        return
    _LAST_BG = now
    asyncio.create_task(_bg_job(vis_key, facts))


async def _bg_job(vis_key: str, facts: list[str]) -> None:
    from app.db.session import SessionLocal

    try:
        async with SessionLocal() as db:
            r = await model_suggestions(db, facts)
            if r:
                _store(vis_key, facts, r[0], r[1])
                log.info("助手建议问题后台补生成完成（%s 条）", len(r[0]))
            else:
                log.info("助手建议问题后台补生成未成功（保留规则版/旧缓存）")
    except Exception:  # noqa: BLE001 —— 后台任务不挂主进程
        log.exception("助手建议问题后台补生成异常")


async def snapshot(db, user) -> dict:
    """GET 用：永远即时返回。命中缓存给模型版；否则给规则版并后台补生成（D49）。"""
    facts, rules, vis_key = await collect_state(db, user)
    sig = _signature(facts)
    hit = _CACHE.get(vis_key)
    now = time.time()
    if hit and hit["sig"] == sig and now - hit["gen_ts"] < STALE_TTL_S:
        if now - hit["gen_ts"] > CACHE_TTL_S:
            _kick_bg(vis_key, facts)          # 状态没变但缓存超 3 小时：后台重生成
        return {"headline": hit["headline"], "suggestions": hit["items"],
                "source": "cache", "generated_at": _iso(hit["gen_ts"])}
    # 冷启动（刚重启）或状态已变：先给规则版（零等待），同时后台补模型版
    _kick_bg(vis_key, facts)
    return {"headline": headline_of(rules), "suggestions": rules[:6],
            "source": "rule", "generated_at": ""}


async def refresh(db, user=None) -> dict:
    """强制生成一次（手动刷新用）：模型失败 → 返回规则版（不污染已有缓存）。"""
    facts, rules, vis_key = await collect_state(db, user)
    r = await model_suggestions(db, facts, quota_role=(getattr(user, "role", "") or "engine"))
    if r:
        _store(vis_key, facts, r[0], r[1])
        return {"headline": r[1], "suggestions": r[0], "source": "model",
                "generated_at": _iso(time.time())}
    return {"headline": headline_of(rules), "suggestions": rules[:6],
            "source": "rule", "generated_at": ""}


async def refresh_manual(db, user) -> dict:
    """手动刷新（带冷却）：冷却期内直接返回当前快照并标记 cooldown。"""
    global _LAST_MANUAL
    now = time.time()
    if now - _LAST_MANUAL < MANUAL_COOLDOWN_S:
        snap = await snapshot(db, user)
        snap["cooldown"] = True
        return snap
    _LAST_MANUAL = now
    return await refresh(db, user=user)


async def scheduler_tick(now_utc: datetime | None = None) -> dict:
    """调度器入口：定时预生成（夜间只在 23/02/05 点跑；状态未变则跳过模型调用）。"""
    if not should_tick(now_utc):
        return {"skipped": True, "reason": "night-window"}
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        facts, _rules, vis_key = await collect_state(db, None)
        sig = _signature(facts)
        hit = _CACHE.get(vis_key)
        if hit and hit["sig"] == sig and time.time() - hit["gen_ts"] < CACHE_TTL_S:
            return {"skipped": True, "reason": "state-unchanged", "facts": len(facts)}
        r = await model_suggestions(db, facts, quota_role="engine")
        if not r:
            return {"skipped": True, "reason": "llm-unavailable", "facts": len(facts)}
        _store(vis_key, facts, r[0], r[1])
        return {"skipped": False, "count": len(r[0]), "facts": len(facts)}


async def job_chat_suggest() -> None:
    try:
        res = await scheduler_tick()
        if res.get("skipped"):
            log.debug("助手建议问题本次跳过：%s", res.get("reason"))
        else:
            log.info("助手建议问题已预生成：%s 条（事实 %s 项）", res.get("count"), res.get("facts"))
    except Exception:  # noqa: BLE001 —— 常驻任务不挂主进程
        log.exception("助手建议问题预生成异常")
