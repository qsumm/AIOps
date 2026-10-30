"""告警路由：规则 CRUD/试运行 + mute CRUD + 告警列表/操作/SSE + 渠道（FR-C1/C4/C5/C7；05 §3）。"""
import asyncio
import json
import re
from datetime import datetime

from fastapi import HTTPException, APIRouter, Depends, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.core.security import audit, require
from app.db.session import get_db
from app.engine import core
from app.engine import detect as detect_mod
from app.models.alert import AlertCurEvent, AlertMute, AlertRule, NotificationChannel, NotificationRecord
from app.models.shell import User
from app.services.notify_format import describe_test
from app.services.notify_transport import send_channel
from app.store import ring
from datetime import timedelta

router = APIRouter(prefix="/api/ops", tags=["ops-alert"])
settings = get_settings()


# ---------------- 规则 ----------------
class RuleIn(BaseModel):
    name: str
    scope: str = "metric"
    metric: str = ""
    queries_json: list = []          # scope=prom：PromQL 列表
    datasource_id: int | None = None  # scope=prom：Prometheus 数据源
    expr: str = ">90"
    severity: int = 2
    eval_interval_s: int = 15
    cron_pattern: str = ""   # 非空则按 cron 评估（FR-C1）
    window_s: int = 300
    for_duration_s: int = 0
    effective_time: list = []
    algorithm: str = "threshold"
    notify_recovered: bool = True
    notify_repeat_step_min: int = 0
    notify_max_number: int = 0
    recover_duration_s: int = 300
    channels_json: list = []
    channels_by_level: dict | None = None   # D140 {"1":[...],"2":[...],"3":[...]}
    append_tags: dict = {}
    enable_ai_summary: bool = True
    runbook_url: str = ""
    disabled: bool = False
    group_id: int | None = None


@router.get("/alert-rules")
async def list_rules(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                     show: str = Query("all", pattern="^(all|enabled|disabled)$"),
                     db=Depends(get_db), _: User = Depends(require("read"))):
    """规则列表。

    D174 修两处**用户可见缺陷**（此前 e2e C7 逮住，用户也会撞上）：
      ① 排序按 id **升序** → 规则数超过一页后，**刚建的规则排在最后一页**，
         用户建完回到列表「找不到刚建的规则」（实测库里 101 条规则时必现）；
         现改为**按 id 倒序**（最新的在最前）。
      ② 「启用中 / 已停用 / 全部」筛选此前是**前端**在当前页数据上过滤 →
         跨页漏数据、计数也不对（全库 101 条时页面只显示前 50 条的计数）；
         现改为**服务端过滤**（show 参数），并回传全量计数 counts。
    """
    q = select(AlertRule)
    if show == "enabled":
        q = q.where(AlertRule.disabled.is_(False))
    elif show == "disabled":
        q = q.where(AlertRule.disabled.is_(True))
    rows = (await db.execute(q.order_by(AlertRule.id.desc()))).scalars().all()
    total = len(rows)                       # D128（过滤后的条数）
    n_enabled = (await db.execute(
        select(func.count()).select_from(AlertRule).where(AlertRule.disabled.is_(False)))).scalar() or 0
    n_disabled = (await db.execute(
        select(func.count()).select_from(AlertRule).where(AlertRule.disabled.is_(True)))).scalar() or 0
    rows = rows[(page - 1) * page_size: page * page_size]
    return {"code": 0, "msg": "ok", "data": {"items": [_rule_out(r) for r in rows], "total": total,
                                             "counts": {"all": n_enabled + n_disabled,
                                                        "enabled": n_enabled, "disabled": n_disabled}}}


def _rule_out(r: AlertRule) -> dict:
    return {c.name: getattr(r, c.name) for c in r.__table__.columns} | {
        "algorithm_effective": detect_mod.algo_available(r.algorithm),
        "created_at": str(r.created_at or ""), "updated_at": str(r.updated_at or ""),
    }


async def _target_exists(db, scope: str, name: str) -> bool:
    """D160：探活/服务规则的目标名是否真实存在（写错会让规则静默永不触发）。"""
    if scope == "probe":
        from app.models.collect import Probe
        q = select(Probe.id).where(Probe.name == name).limit(1)
    else:
        from app.models.svc import Service
        q = select(Service.id).where(Service.name == name).limit(1)
    return (await db.execute(q)).first() is not None


async def _validate_rule(body: "RuleIn", db, exclude_id: int | None = None) -> None:
    """创建/更新规则的入参校验（夯实轮补）。

    之前只有 `detect_mod._parse_expr(expr)` 预校验，但它抛**裸 ValueError**，落到全局
    兜底变成 500"服务端异常"——实测一次非法表达式就是 500，且坏规则若落库会让引擎
    每轮评估刷异常日志。这里把全部可预判的非法入参拦在写库之前，给可读的 400。
    """
    if not (body.name or "").strip():
        raise HTTPException(400, "规则名称不能为空")
    if len(body.name) > 128:
        raise HTTPException(400, "规则名称过长（≤128 字符）")
    dup = (await db.execute(select(AlertRule).where(AlertRule.name == body.name.strip()))).scalar_one_or_none()
    if dup and dup.id != exclude_id:   # D59：name 是 unique 列，预检给 409 人话（与全局 IntegrityError 409 同口径）
        raise HTTPException(409, f"规则名称「{body.name.strip()}」已存在，请换一个名字")
    if body.severity not in (1, 2, 3):
        raise HTTPException(400, "severity 只允许 1（紧急）/ 2（警告）/ 3（通知）")

    # D140 分级通知校验：结构 + S1 电话兜底（不能静默降级——S1 配置里没电话就拒收，
    # 让用户在保存时就补电话渠道，而不是告警响的时候才发现只有 IM）。
    cbl = body.channels_by_level
    if cbl is not None:
        if not isinstance(cbl, dict):
            raise HTTPException(400, "channels_by_level 必须是对象（键 1/2/3，值为渠道名数组）")
        for k, v in cbl.items():
            if k not in ("1", "2", "3"):
                raise HTTPException(400, f"channels_by_level 的键只允许 1/2/3，收到 {k!r}")
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                raise HTTPException(400, f"channels_by_level[{k}] 必须是渠道名数组")
        s1 = cbl.get("1") or []
        if s1:
            names = {c.name for c in (await db.execute(
                select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))).scalars().all()}
            types = {}
            for ch in (await db.execute(select(NotificationChannel))).scalars().all():
                types[ch.name] = ch.type
            missing = [n for n in s1 if n not in names]
            if missing:
                raise HTTPException(400, f"S1 渠道不存在或未启用：{missing}")
            if not any(types.get(n) in ("phone", "aliyun_voice") for n in s1):
                raise HTTPException(400,
                    "S1（紧急）的分级渠道必须包含一个「电话」类型渠道（电话 / 阿里云语音）——"
                    "紧急告警不能只发 IM（到「系统设置 → 通知渠道」添加电话渠道后重试）")
    # scope 白名单（必须显式校验）：`alert_rules.scope` 是 **ENUM** 列，写入非法值会被
    # MySQL/SeekDB **静默截断**成空串 → 规则看起来建好了、引擎却永远不会评估它
    # （D47-1 同款坑：purpose ENUM 不同步导致功能静默降级）。所以在这里挡下来并说清允许值。
    if body.scope not in ("metric", "probe", "prom", "service"):
        raise HTTPException(400, "scope 只允许 metric（指标）/ probe（探活）/ service（服务状态）/ prom（Prometheus）")
    if body.scope == "metric" and not (body.metric or "").strip():
        raise HTTPException(400, "指标类规则必须填写指标名（探活/服务类可留空：留空=该项全部）")
    if len(body.metric or "") > 64:
        raise HTTPException(400, "指标名过长（≤64 字符）")
    if len(body.runbook_url or "") > 255:
        raise HTTPException(400, "runbook 链接过长（≤255 字符）")
    if body.scope == "prom" and not [q for q in (body.queries_json or []) if str(q).strip()]:
        raise HTTPException(400, "轨 B（Prometheus）规则至少填写一条 PromQL 查询")
    # D160（靶机真机验收发现）：scope=probe / scope=service 时 `metric` 字段的语义是
    # **探活名 / 服务名**（不是指标名）——引擎据此过滤（core.py `_eval_probe_once` /
    # `_eval_service_once`）。前端曾对所有 scope 默认填 `cpu.percent` → 引擎去找一个
    # 不存在的探活/服务名 → 零行 → **规则永不触发**（真机连挂 4 天没报过一条 S1 探活）。
    # 这里把"名字写错"从静默失效变成明确报错：留空=全部；写了必须真实存在。
    if body.scope in ("probe", "service") and (body.metric or "").strip():
        name = body.metric.strip()
        if not await _target_exists(db, body.scope, name):
            kind = "探活项" if body.scope == "probe" else "服务"
            raise HTTPException(400, f"没有叫「{name}」的{kind}（{kind}名来自 Agent 上报的清单）；"
                                     f"留空 = 全部{kind}")
    if int(body.eval_interval_s) < 1:
        raise HTTPException(400, "评估间隔需 ≥ 1 秒（引擎另有 5 秒防抖下限）")
    if int(body.eval_interval_s) > 86400:
        # D16 对抗测试补：原先无上界（实测 999999 秒被接受）——11 天的评估间隔
        # 等于规则静默失效，属误配；上界取 1 天（更长的周期请用 cron_pattern）。
        raise HTTPException(400, "评估间隔上限 86400 秒（1 天）；更长周期请用 cron 表达式")
    if int(body.window_s) > 86400:
        raise HTTPException(400, "评估窗口上限 86400 秒（1 天）")
    if int(body.window_s) < 10:
        raise HTTPException(400, "评估窗口需 ≥ 10 秒")
    try:
        detect_mod._parse_expr(body.expr)
    except ValueError as e:
        raise HTTPException(400, f"条件表达式不合法：{e}")
    cron = (body.cron_pattern or "").strip()
    if cron:
        try:
            from apscheduler.triggers.cron import CronTrigger

            CronTrigger.from_crontab(cron)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"cron 表达式不合法：{e}（示例 '0 9 * * *'）")


@router.post("/alert-rules")
async def create_rule(body: RuleIn, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    await _validate_rule(body, db)
    r = AlertRule(**body.model_dump())
    db.add(r)
    await db.commit()
    await audit(db=db, request=request, user=user, action="rule_create", resource=f"rule:{r.id}")
    return {"code": 0, "msg": "ok", "data": {"id": r.id}}


@router.put("/alert-rules/{rid}")
async def update_rule(rid: int, body: RuleIn, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    r = await db.get(AlertRule, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    await _validate_rule(body, db, exclude_id=rid)
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    await db.commit()
    await audit(db=db, request=request, user=user, action="rule_update", resource=f"rule:{rid}")
    return {"code": 0, "msg": "ok", "data": {"id": rid}}


@router.delete("/alert-rules/{rid}")
async def delete_rule(rid: int, request: Request, purge: bool = False,
                      db=Depends(get_db), user: User = Depends(require("write"))):
    """停用（软删，默认）/ 删除（purge=true，硬删）。

    D61：原先只有软删——停用的规则永远留在列表里（用户截图：smoke/回归测试
    留下的十来行「已停用」垃圾规则没法清），而且停用后没有任何启用入口。
    现在：默认仍是软删（保留历史、可一键启用）；`?purge=true` 真删行，
    供「删除」按钮清理废弃规则（事件表冗余了 rule_name，展示不受影响）。
    """
    r = await db.get(AlertRule, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    from app.services import alert_freshness

    if purge:
        await db.delete(r)
        await db.commit()
        await alert_freshness.mark_rule_stale(db, rid, "rule_deleted")
        await audit(db=db, request=request, user=user, action="rule_purge", resource=f"rule:{rid}")
        return {"code": 0, "msg": "ok", "data": {"id": rid, "purged": True}}
    r.disabled = True  # 软删：worker 对账自动停止
    await db.commit()
    await alert_freshness.mark_rule_stale(db, rid, "rule_disabled")
    await audit(db=db, request=request, user=user, action="rule_delete", resource=f"rule:{rid}")
    return {"code": 0, "msg": "ok", "data": {"id": rid}}


@router.post("/alert-rules/{rid}/toggle")
async def toggle_rule(rid: int, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    """启用/停用切换（D61）。

    缺口背景：「停用」是软删（disabled=True），但界面上停用后的规则只剩
    「测试运行/编辑」——用户想重新启用只能进编辑器盲猜（编辑表单里也没有启停开关），
    实际上**没有任何入口能把规则救回来**。给一个显式的双向开关 + 审计。
    """
    r = await db.get(AlertRule, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    r.disabled = not r.disabled
    await db.commit()
    if r.disabled:
        from app.services import alert_freshness

        await alert_freshness.mark_rule_stale(db, rid, "rule_disabled")
    await audit(db=db, request=request, user=user,
                action="rule_disable" if r.disabled else "rule_enable", resource=f"rule:{rid}")
    return {"code": 0, "msg": "ok", "data": {"id": rid, "disabled": r.disabled}}


@router.post("/alert-rules/{rid}/test")
async def test_rule(rid: int, db=Depends(get_db), _: User = Depends(require("diag"))):
    """试运行：单次评估不落事件（FR-C1 验收）。轨 B 走 Prometheus 数据源试算。"""
    r = await db.get(AlertRule, rid)
    if r is None:
        raise HTTPException(404, "资源不存在或已被删除")
    now = datetime.utcnow()
    window = [now - timedelta(seconds=r.window_s), now]
    matched: list[dict] = []
    if r.scope == "prom":
        from app.models.prom import PromDatasource
        from app.services import prom as prom_svc

        ds = await db.get(PromDatasource, r.datasource_id) if r.datasource_id else (
            await db.execute(select(PromDatasource).where(PromDatasource.enabled.is_(True)))).scalars().first()
        if ds is None:
            return {"code": 0, "msg": "ok", "data": {"matched": [], "anomaly_count": 0, "error": "未配置 Prometheus 数据源"}}
        series: list[tuple[dict, float]] = []
        try:
            for q in (r.queries_json or []):
                series.extend(await prom_svc.query_range(ds, q, *window, step_s=max(r.eval_interval_s, 15)))
        except prom_svc.PromUnreachable as e:
            return {"code": 0, "msg": "ok", "data": {"matched": [], "anomaly_count": 0, "error": f"数据源不可达: {e}"}}
        for labels, v in detect_mod.detect_labeled(r.algorithm, r.expr, series):
            matched.append({"host": labels.get("instance", ""), "metric": labels.get("__name__", ""), "value": v})
        return {"code": 0, "msg": "ok", "data": {"matched": matched, "anomaly_count": len(matched),
                                                 "algorithm_effective": detect_mod.algo_available(r.algorithm)}}
    if r.scope == "probe":
        from app.models.collect import Probe
        from app.models.host import Host as H

        q = select(Probe, H.ident).join(H, Probe.host_id == H.id).where(Probe.enabled.is_(True))
        if r.metric:
            q = q.where(Probe.name == r.metric)
        else:
            q = q.where(Probe.type != "https-cert")  # 与引擎一致：证书类须显式指名
        for probe, ident in (await db.execute(q)).all():
            value = float(probe.last_latency_ms if probe.type == "https-cert" and probe.last_latency_ms is not None
                          else (1.0 if probe.last_status == "down" else 0.0))
            for _, v in detect_mod.detect("threshold", r.expr, [(None, value)]):
                matched.append({"host": ident, "metric": probe.name, "value": v, "probe_status": probe.last_status})
        return {"code": 0, "msg": "ok", "data": {"matched": matched, "anomaly_count": len(matched),
                                                 "algorithm_effective": "threshold"}}
    from app.models.host import Host

    # D154：与引擎同口径——支持维度选择器 `metric{k=v}`（逐序列评估）
    base_metric, tag_filter = ring.parse_selector(r.metric)
    hosts = (await db.execute(select(Host))).scalars().all()
    wildcard = core._has_wildcard(base_metric)   # D150：与引擎同口径（测试运行也要能测通配规则）
    for h in hosts:
        names = ([base_metric] if not wildcard
                 else await core._wildcard_metrics(h.ident, base_metric, *window,
                                                   include_agg=r.window_s > 7200))
        for name in names:
            if tag_filter is not None:
                for series_tags, spts in ring.query_series(h.ident, name, tag_filter, *window):
                    if not spts:
                        continue
                    for _, v in detect_mod.detect(r.algorithm, r.expr, spts):
                        matched.append({"host": h.ident, "metric": name,
                                        "tags": series_tags, "value": v})
                continue
            pts = ring.query_points(h.ident, name, *window)
            for _, v in detect_mod.detect(r.algorithm, r.expr, pts):
                matched.append({"host": h.ident, "metric": name, "value": v})
    return {"code": 0, "msg": "ok", "data": {"matched": matched, "anomaly_count": len(matched),
                                             "algorithm_effective": detect_mod.algo_available(r.algorithm)}}


# ---------------- mute ----------------
class MuteIn(BaseModel):
    cause: str = ""
    tag_filters: list = []
    severities: list = []
    btime: str | None = None
    etime: str | None = None
    periodic_mutes: list = []
    mute_type: int = 0
    disabled: bool = False


@router.get("/alert-mutes")
async def list_mutes(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(AlertMute).order_by(AlertMute.id.desc()))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [
        {"id": r.id, "cause": r.cause, "tag_filters": r.tag_filters, "severities": r.severities,
         "btime": str(r.btime or ""), "etime": str(r.etime or ""), "periodic_mutes": r.periodic_mutes,
         "mute_type": r.mute_type, "disabled": r.disabled} for r in rows], "total": len(rows)}}


@router.post("/alert-mutes")
async def create_mute(body: MuteIn, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    # D16 对抗测试修复：
    # ① mute_type 是 0/1 二值语义（0=全屏蔽不产生事件 / 1=只屏蔽通知），
    #    原先任意整数都能写入（实测 mute_type=99 返回 200），落库后语义未定义；
    # ② etime < btime 的时间倒挂静默永远不会生效（实测也返回 200），属逻辑错误数据。
    # D59 补：原因为必填（屏蔽没有"为什么"，事后审计无法回答）；tag_filters 的匹配值
    # 非空（空的 v 恒不匹配，等于一条永不生效的废静默）；全部限长对齐 DB 列宽。
    if body.mute_type not in (0, 1):
        raise HTTPException(400, "mute_type 只允许 0（全屏蔽）或 1（只屏蔽通知）")
    if not (body.cause or "").strip():
        raise HTTPException(400, "屏蔽原因为必填项（1~255 字符），请说明为什么屏蔽")
    if len(body.cause) > 255:
        raise HTTPException(400, "屏蔽原因过长（≤255 字符）")
    for f in body.tag_filters or []:
        if not isinstance(f, dict) or not str(f.get("v") or "").strip():
            raise HTTPException(400, "TagFilter 的匹配值（v）不能为空")
        if len(str(f.get("v"))) > 128:
            raise HTTPException(400, "TagFilter 匹配值过长（≤128 字符）")
    try:
        _bt = datetime.fromisoformat(body.btime) if body.btime else None
        _et = datetime.fromisoformat(body.etime) if body.etime else None
    except ValueError as e:
        raise HTTPException(400, f"时间格式不合法（应为 ISO 8601）：{e}")
    if _bt and _et and _et <= _bt:
        raise HTTPException(400, "静默结束时间必须晚于开始时间")
    r = AlertMute(
        cause=body.cause, tag_filters=body.tag_filters, severities=body.severities,
        btime=_bt, etime=_et,
        periodic_mutes=body.periodic_mutes, mute_type=body.mute_type, disabled=body.disabled,
    )
    db.add(r)
    await db.commit()
    await audit(db=db, request=request, user=user, action="mute_create", resource=f"mute:{r.id}")
    return {"code": 0, "msg": "ok", "data": {"id": r.id}}


@router.delete("/alert-mutes/{mid}")
async def delete_mute(mid: int, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    await db.execute(delete(AlertMute).where(AlertMute.id == mid))
    await db.commit()
    await audit(db=db, request=request, user=user, action="mute_delete", resource=f"mute:{mid}")
    return {"code": 0, "msg": "ok", "data": {"id": mid}}


# ---------------- 告警 ----------------
def _metric_label(ev) -> str:
    """指标中文名（复用 notify_format 的展示口径，失败回退原始指标名）。"""
    from app.services.notify_format import _metric_info

    try:
        label, _ = _metric_info(ev)
        return label or str(getattr(ev, "metric", "") or "")
    except Exception:  # noqa: BLE001 —— 展示层失败不该让列表 500
        return str(getattr(ev, "metric", "") or "")


@router.get("/alerts")
async def list_alerts(
    status: str = "", severity: int = 0, host: str = "", keyword: str = "",
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    db=Depends(get_db), user: User = Depends(require("read")),
):
    from app.services import visibility

    q = select(AlertCurEvent).order_by(AlertCurEvent.severity, AlertCurEvent.trigger_time.desc())
    qc = select(func.count()).select_from(AlertCurEvent)
    vcond = await visibility.visible_fingerprints_filter(db, user, AlertCurEvent.host_ident)
    if vcond is not None:
        q, qc = q.where(vcond), qc.where(vcond)
    if status:
        q, qc = q.where(AlertCurEvent.status == status), qc.where(AlertCurEvent.status == status)
    if severity:
        q, qc = q.where(AlertCurEvent.severity == severity), qc.where(AlertCurEvent.severity == severity)
    if host:
        q, qc = q.where(AlertCurEvent.host_ident == host), qc.where(AlertCurEvent.host_ident == host)
    if keyword.strip():
        kw = f"%{keyword.strip()[:64]}%"
        cond = AlertCurEvent.rule_name.like(kw) | AlertCurEvent.metric.like(kw)
        q, qc = q.where(cond), qc.where(cond)
    total = (await db.execute(qc)).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    # D149：把**规则触发条件**带回列表（expr），告警详情要展示"为什么触发"。
    # 用户实测：磁盘使用率 7.9% 报 S2，详情只写规则名、看不到条件，第一反应是平台误报；
    # 实际是规则 expr 写成了 `>1`。条件摆在详情里，这类配置问题一眼可辨。
    rule_ids = {r.rule_id for r in rows if r.rule_id}
    rule_expr: dict[int, str] = {}
    if rule_ids:
        rr = (await db.execute(select(AlertRule.id, AlertRule.expr).where(AlertRule.id.in_(rule_ids)))).all()
        rule_expr = {rid: (ex or "") for rid, ex in rr}
    return {"code": 0, "msg": "ok", "data": {"total": total, "items": [
        {"hash": r.hash, "rule_id": r.rule_id, "rule_name": r.rule_name, "host_ident": r.host_ident,
         "metric": r.metric,
         # D175-6：列表给指标中文名（含维度）——"disk.percent 18.36" 用户看不出是什么问题，
         # "磁盘使用率 18.36%" 一眼可读（口径与语音播报、飞书卡片一致）
         "metric_label": _metric_label(r),
         "severity": r.severity, "status": r.status, "trigger_value": r.trigger_value,
         "trigger_expr": rule_expr.get(r.rule_id, ""),
         "trigger_time": str(r.trigger_time), "first_trigger_time": str(r.first_trigger_time),
         "notify_cur_number": r.notify_cur_number, "tags": r.tags, "ai_summary": r.ai_summary,
         "last_eval_at": str(r.last_eval_at or ""), "stale": bool(r.stale),
         "stale_reason": r.stale_reason or ""} for r in rows]}}


@router.post("/alerts/{h}/ack")
async def ack_alert(h: str, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    cur = await db.get(AlertCurEvent, h)
    if cur:
        cur.status = "ack"
        await db.commit()
        await audit(db=db, request=request, user=user, action="alert_ack", resource=f"alert:{h[:12]}")
    return {"code": 0, "msg": "ok", "data": {"hash": h}}


@router.post("/alerts/{h}/resolve")
async def resolve_alert(h: str, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    """人工解决：cur → his(recovered)。"""
    cur = await db.get(AlertCurEvent, h)
    if cur:
        from app.models.alert import AlertHisEvent

        db.add(AlertHisEvent(hash=cur.hash, rule_id=cur.rule_id, rule_name=cur.rule_name,
                             host_ident=cur.host_ident, metric=cur.metric, severity=cur.severity,
                             trigger_time=cur.trigger_time, trigger_value=cur.trigger_value,
                             tags=cur.tags, is_recovered=True, recover_time=datetime.utcnow()))
        await db.delete(cur)
        await db.commit()
        await audit(db=db, request=request, user=user, action="alert_resolve", resource=f"alert:{h[:12]}")
    return {"code": 0, "msg": "ok", "data": {"hash": h}}


class BulkIn(BaseModel):
    hashes: list[str]
    action: str  # ack | resolve | suppress


@router.post("/alerts/bulk")
async def bulk_alerts(body: BulkIn, request: Request, db=Depends(get_db), user: User = Depends(require("alert_ops"))):
    """批量确认/解决。

    夯实轮改：此前 resolve 分支逐条调 `resolve_alert()`（内部各自 commit + 写审计），
    2000 条批量 = 2000 次 commit + 2000 行审计。现改为**一次事务、一次审计**：
    批量查 cur → 批量写 his（recovered）→ 批量删 cur → 单次 commit。
    """
    n = 0
    if body.action == "ack":
        rows = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash.in_(body.hashes)))).scalars().all()
        for cur in rows:
            cur.status = "ack"
            n += 1
    elif body.action == "delete":
        if len(body.hashes) > 500:
            raise HTTPException(422, "单次最多删除 500 条——大批量请分批，避免误删不可回滚")
        rows = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash.in_(body.hashes)))).scalars().all()
        for cur in rows:
            await db.delete(cur)
            n += 1
    elif body.action == "resolve":
        from app.models.alert import AlertHisEvent

        rows = (await db.execute(select(AlertCurEvent).where(AlertCurEvent.hash.in_(body.hashes)))).scalars().all()
        now = datetime.utcnow()
        for cur in rows:
            db.add(AlertHisEvent(hash=cur.hash, rule_id=cur.rule_id, rule_name=cur.rule_name,
                                 host_ident=cur.host_ident, metric=cur.metric, severity=cur.severity,
                                 trigger_time=cur.trigger_time, trigger_value=cur.trigger_value,
                                 tags=cur.tags, is_recovered=True, recover_time=now))
            await db.delete(cur)
            n += 1
    await db.commit()
    detail = {"n": n}
    if body.action == "delete":
        detail["hashes"] = list(body.hashes)[:500]   # 彻底删除不可回滚 → 审计留痕可追溯
    await audit(db=db, request=request, user=user, action=f"alert_bulk_{body.action}", detail=detail)
    return {"code": 0, "msg": "ok", "data": {"affected": n}}


@router.get("/alert-history")
async def list_alert_history(
    severity: int = 0, host: str = "", keyword: str = "", hours: int = Query(168, ge=1, le=2160),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    db=Depends(get_db), user: User = Depends(require("read")),
):
    """告警历史（D166）：已恢复/已关闭事件的存档查询。

    用户原话："告警记录支持界面查看历史记录。"——此前 alert_his_events 只有
    AI 工具（alert_history）能查，界面上没有入口；恢复通知过后历史就"消失"了。
    过滤：severity / host（ident 或名称模糊）/ keyword（规则名或指标模糊）/ hours 时间窗
    （默认 7 天，最长 90 天）。按触发时间倒序，分页。
    """
    from datetime import timedelta

    from app.models.alert import AlertHisEvent
    from app.models.host import Host
    from app.services import visibility

    since = datetime.utcnow() - timedelta(hours=hours)
    q = select(AlertHisEvent).where(AlertHisEvent.trigger_time >= since).order_by(
        AlertHisEvent.trigger_time.desc())
    qc = select(func.count()).select_from(AlertHisEvent).where(AlertHisEvent.trigger_time >= since)
    vcond = await visibility.visible_fingerprints_filter(db, user, AlertHisEvent.host_ident)
    if vcond is not None:
        q, qc = q.where(vcond), qc.where(vcond)
    if severity:
        q, qc = q.where(AlertHisEvent.severity == severity), qc.where(AlertHisEvent.severity == severity)
    host_kw = host.strip()
    if host_kw:
        ids = (await db.execute(select(Host.ident).where(Host.name.like(f"%{host_kw[:32]}%")))).scalars().all()
        cond = AlertHisEvent.host_ident == host_kw
        if ids:
            cond = cond | AlertHisEvent.host_ident.in_(ids)
        q, qc = q.where(cond), qc.where(cond)
    if keyword.strip():
        kw = f"%{keyword.strip()[:64]}%"
        cond = AlertHisEvent.rule_name.like(kw) | AlertHisEvent.metric.like(kw)
        q, qc = q.where(cond), qc.where(cond)
    total = (await db.execute(qc)).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"total": total, "items": [
        {"hash": r.hash, "rule_id": r.rule_id, "rule_name": r.rule_name, "host_ident": r.host_ident,
         "metric": r.metric, "severity": r.severity, "trigger_value": r.trigger_value,
         "trigger_time": str(r.trigger_time), "recover_time": str(r.recover_time or ""),
         "is_recovered": r.is_recovered, "tags": r.tags, "ai_summary": r.ai_summary,
         } for r in rows]}}


@router.get("/alerts/stream")
async def alerts_stream(request: Request, ticket: str = "", db=Depends(get_db)):
    """SSE 实时告警流（FR-C7）。鉴权按 03 §1.22：一次性 ticket（消费即焚）或 Bearer。"""
    from app.core.security import get_current_user
    from app.services import sse_ticket as tickets

    role = None
    if ticket:
        rec = tickets.consume(ticket)
        if rec is None:
            return JSONResponse({"code": 401, "msg": "TICKET_INVALID_OR_EXPIRED", "data": None}, status_code=401)
        role = rec["role"]
    else:  # 兼容带 header 的直连（curl 调试）：直接解 Authorization
        import jwt as _jwt

        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth.lower().startswith("bearer ") else ""
        try:
            payload = _jwt.decode(tok, settings.jwt_secret, algorithms=["HS256"])
            role = payload.get("role", "member")
        except Exception:  # noqa: BLE001
            return JSONResponse({"code": 401, "msg": "UNAUTHORIZED", "data": None}, status_code=401)
    if role not in ("admin", "member", "demo"):
        return JSONResponse({"code": 403, "msg": "FORBIDDEN", "data": None}, status_code=403)

    q = core.subscribe()

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {payload.get('kind', 'alert')}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            core.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------- 渠道与台账 ----------------
class ChannelIn(BaseModel):
    name: str
    type: str
    config_json: dict = {}
    enabled: bool = True


async def _validate_channel_in(body: ChannelIn, db, exclude_id: int | None = None) -> None:
    """渠道入参校验（D59）。

    缺口背景：name/type 原先完全不校验——空名渠道能落库（列表里一行空白，用户报）；
    name 是 unique 列，重名直接撞 DB 变 500；类型对了但一个投递配置都不填也能存
    （永远发不出去的"死渠道"）。全部拦在写库前，给人话 400。
    """
    from app.services.notify_transport import FRIENDLY_TYPES, validate_channel

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "渠道名称为必填项（1~64 字符）")
    if len(name) > 64:
        raise HTTPException(400, "渠道名称过长（≤64 字符）")
    dup = (await db.execute(select(NotificationChannel).where(NotificationChannel.name == name))).scalar_one_or_none()
    if dup and dup.id != exclude_id:
        # 409 与全局 IntegrityError 处理器同口径（D8/D11 约定：重名=冲突，不是参数错）
        raise HTTPException(409, f"渠道名称「{name}」已存在，请换一个名字")
    bad = validate_channel(body.type, body.config_json)
    if bad:
        raise HTTPException(400, bad)
    if body.type in FRIENDLY_TYPES:
        cfg = body.config_json or {}
        if body.type == "aliyun_voice":
            # D152：阿里云语音是自研直连（非 apprise URL），必填项由
            # validate_channel 统一校验（AK ID/Secret、模板 Code、被叫手机号）。
            # 这里额外校两件事：
            # ① 手机号格式——阿里云要求国内 13800138000 / 国际 +8613800138000，
            #    格式错了是 400 而非到了告警现场才发现"呼叫失败"；
            # ② 播放参数区间（PlayTimes 1~3 / Volume 0~100 / Speed -500~500）——
            #    越界就**明确报错**，而不是在投递时悄悄丢掉（用户以为生效了其实没生效）。
            ph = str(cfg.get("phone") or "").strip()
            if ph and not re.match(r"^\+?[0-9][0-9 \-()]{5,20}$", ph):
                raise HTTPException(400, "阿里云语音的被叫号码格式不对：国内如 13800138000，国际如 +8613800138000")
            for key, label, lo, hi in (("play_times", "播放次数", 1, 3),
                                       ("volume", "音量", 0, 100),
                                       ("speed", "语速", -500, 500)):
                raw = cfg.get(key)
                if raw is None or str(raw).strip() == "":
                    continue
                try:
                    val = int(str(raw).strip())
                except (TypeError, ValueError):
                    raise HTTPException(400, f"阿里云语音的{label}必须是整数（{lo}~{hi}）")
                if not (lo <= val <= hi):
                    raise HTTPException(400, f"阿里云语音的{label}必须在 {lo}~{hi} 之间（收到 {val}）")
            key = str(cfg.get("tts_param_key") or "").strip()
            if key and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise HTTPException(400, "阿里云语音的模板变量名只能是字母/数字/下划线，且不以数字开头")
            out_id = str(cfg.get("out_id") or "").strip()
            if out_id and len(out_id.encode("utf-8")) > 15:
                # 官方文档：OutId 必须是 1~15 字节（超长阿里云会拒）
                raise HTTPException(400, "阿里云语音的外部流水号最多 15 字节（阿里云回执会带回它）")
            return
        has_url = bool(str(cfg.get("webhook_url") or "").strip())
        has_smtp = isinstance(cfg.get("smtp"), dict) and bool(str((cfg.get("smtp") or {}).get("host") or "").strip())
        has_apprise = bool(str(cfg.get("apprise_url") or "").strip())
        if not (has_url or has_smtp or has_apprise):
            raise HTTPException(400, "至少填写一种投递配置：Webhook 地址 / 邮件 SMTP 串 / apprise_url")
        u = str(cfg.get("webhook_url") or "").strip()
        if u and not u.lower().startswith(("http://", "https://")):
            raise HTTPException(400, "Webhook 地址需以 http:// 或 https:// 开头")


@router.get("/notify-channels")
async def list_channels(db=Depends(get_db), _: User = Depends(require("read"))):
    rows = (await db.execute(select(NotificationChannel).order_by(NotificationChannel.id))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [
        {"id": r.id, "name": r.name, "type": r.type, "config_json": _mask(r.config_json),
         "enabled": r.enabled, "last_test_status": r.last_test_status} for r in rows], "total": len(rows)}}


def _mask(cfg: dict) -> dict:
    """渠道配置出参掩码（安全口径与 api_key 一致）。

    D15 审计补：原先只掩了 smtp 密码，**webhook_url / apprise_url 是明文返回**——
    飞书/钉钉机器人 webhook 本身就是凭据（拿到即可往群里发消息），
    明文返回会随界面截图、演示录屏、手册/PDF 一起外泄。
    掩码规则：保留 scheme+host 与路径前缀便于辨认，中段星号，尾 4 位可核对。
    """
    import copy as _c

    def _m(u: str) -> str:
        u = str(u or "")
        if len(u) <= 24:
            return "***" if u else u
        return u[:22] + "…" + u[-4:]

    c = _c.deepcopy(cfg or {})
    if isinstance(c.get("smtp"), dict) and c["smtp"].get("pass"):
        c["smtp"]["pass"] = "***"
    for k in ("webhook_url", "apprise_url"):
        if c.get(k):
            c[k] = _m(c[k])
    # D152：阿里云 AccessKey Secret 是**完整凭据**（拿到即可调用用户账户下的
    # 任意阿里云 API），不能用 _m 的"前 22 位 + 尾 4 位"掩码——那等于半明文
    # 泄露。整段替换成 "***"，与 smtp.pass 同口径。
    if c.get("access_key_secret"):
        c["access_key_secret"] = "***"
    return c


@router.post("/notify-channels")
async def create_channel(body: ChannelIn, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    await _validate_channel_in(body, db)
    r = NotificationChannel(**{**body.model_dump(), "name": body.name.strip(), "type": body.type.strip().lower()})
    db.add(r)
    await db.commit()
    await audit(db=db, request=request, user=user, action="channel_create", resource=f"channel:{r.id}")
    return {"code": 0, "msg": "ok", "data": {"id": r.id}}


@router.put("/notify-channels/{cid}")
async def update_channel(cid: int, body: ChannelIn, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    """编辑渠道（D59）。

    缺口背景：渠道原先只能建和删——名字打错、webhook 换了地址都只能删了重建，
    而"测试状态/熔断计数"等历史也一并丢失。配置合并语义：payload 里**出现的键**
    覆盖旧值，**没出现的键**保留旧值——列表出参的 webhook_url/apprise_url 是掩码的
    （D15），编辑框留空 = 不改动，不能把掩码串当真值写回去。
    """
    ch = await db.get(NotificationChannel, cid)
    if ch is None:
        raise HTTPException(404, "资源不存在或已被删除")
    merged = dict(ch.config_json or {})
    for k, v in (body.config_json or {}).items():
        if isinstance(v, str) and not v.strip():
            continue          # 空串 = 保留旧值（编辑框留空不动真实凭据，出参是掩码不能写回）
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    await _validate_channel_in(body.model_copy(update={"config_json": merged}), db, exclude_id=cid)
    ch.name = body.name.strip()
    ch.type = body.type.strip().lower()
    ch.enabled = body.enabled
    ch.config_json = merged
    await db.commit()
    await audit(db=db, request=request, user=user, action="channel_update", resource=f"channel:{cid}")
    return {"code": 0, "msg": "ok", "data": {"id": cid}}


@router.delete("/notify-channels/{cid}")
async def delete_channel(cid: int, request: Request, db=Depends(get_db), user: User = Depends(require("write"))):
    await db.execute(delete(NotificationChannel).where(NotificationChannel.id == cid))
    await db.commit()
    await audit(db=db, request=request, user=user, action="channel_delete", resource=f"channel:{cid}")
    return {"code": 0, "msg": "ok", "data": {"id": cid}}


@router.post("/notify-channels/{cid}/test")
async def test_channel(cid: int, db=Depends(get_db), _: User = Depends(require("write"))):
    ch = await db.get(NotificationChannel, cid)
    if ch is None:
        raise HTTPException(404, "资源不存在或已被删除")
    title, text, card = describe_test(ch.name, settings.public_base_url, channel_type=ch.type)
    ok, detail = await send_channel(ch.type, ch.config_json or {}, title, text, card=card)
    ch.last_test_status = "ok" if ok else "failed"
    await db.commit()
    return {"code": 0, "msg": "ok", "data": {"ok": ok, "detail": detail}}


@router.get("/notification-records")
async def notif_records(event_hash: str = "", page: int = Query(1, ge=1),
                         page_size: int = Query(50, ge=1, le=200),
                         db=Depends(get_db), _: User = Depends(require("read"))):
    q = select(NotificationRecord).order_by(NotificationRecord.id.desc())
    qc = select(func.count()).select_from(NotificationRecord)
    if event_hash:
        q, qc = q.where(NotificationRecord.event_hash == event_hash), qc.where(NotificationRecord.event_hash == event_hash)
    total = (await db.execute(qc)).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"total": total, "items": [
        {"id": r.id, "event_hash": r.event_hash, "channel": r.channel, "status": r.status,
         "error_detail": r.error_detail, "created_at": str(r.created_at)} for r in rows]}}
