"""调度器：对账式常驻任务宿主（02 §5；03 §1.5）。

- aggregate：每 60s 环形→metric_agg 降采样落库
- offline：每 30s 心跳超时判 offline（3×15s）
- inspection：按巡检任务的 cron_expr 动态注册（对账式同步，FR-D5；03 §1.16）
- skill_check：每 30 分钟重跑领域技能的连接检查（fail-closed 状态自维护，FR §6.8）
- prom_scrape：每 30s 直采 mode=scrape 的 exporter（FR-B3+ node_exporter 零改造接入）
- chat_suggest：每小时预生成 AI 助手建议问题（D49；北京 23:00–08:00 只跑 23/02/05 点，
  状态未变则跳过模型调用，详见 services/chat_suggest.py）
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.db.session import SessionLocal
from app.services import ingest
from app.services import runbook as runbook_svc
from app.store import ring

log = logging.getLogger("sched")
scheduler = AsyncIOScheduler(timezone="UTC")


async def job_aggregate() -> None:
    async with SessionLocal() as db:
        try:
            n = await ring.flush_aggregates(db)
            if n:
                log.info("聚合落库 %s 行", n)
            # D159 / 缺陷 D158-1：带维度序列同刷进 metric_series（同一 60s 节拍、同一会话）
            ns = await ring.flush_series(db)
            if ns:
                log.info("维度序列落库 %s 行", ns)
        except Exception:  # noqa: BLE001 —— 常驻任务不挂主进程
            log.exception("聚合任务异常")


async def job_sweep_stale_runbooks() -> None:
    """D160：收敛超时无活动的变更单（否则永久挡住自愈护栏⑧）。"""
    async with SessionLocal() as db:
        try:
            n = await runbook_svc.sweep_stale_running(db)
            if n:
                log.info("僵死变更单收敛 %s 张 → aborted", n)
        except Exception:  # noqa: BLE001 —— 常驻任务不挂主进程
            log.exception("变更单收敛任务异常")


async def job_offline() -> None:
    async with SessionLocal() as db:
        try:
            n = await ingest.mark_offline_hosts(db)
            if n:
                log.info("标记 offline 主机 %s 台", n)
        except Exception:
            log.exception("offline 判定异常")


async def job_alert_freshness() -> None:
    """P0：每 30s 扫描一次僵尸活跃行（规则评估停了就不再刷新 last_eval_at）。"""
    from app.services import alert_freshness

    async with SessionLocal() as db:
        try:
            n = await alert_freshness.mark_stale_alerts(db)
            if n:
                log.info("标记陈旧活跃告警 %s 条", n)
        except Exception:  # noqa: BLE001
            log.exception("告警新鲜度扫描异常")


async def job_reconcile_inspections() -> None:
    """对账式同步巡检任务：新增/删除/改 cron 都自动生效（与告警规则对账同思路）。"""
    from app.models.ops import Inspection

    async with SessionLocal() as db:
        rows = (await db.execute(select(Inspection).where(Inspection.enabled.is_(True)))).scalars().all()
    wanted = {f"inspect-{r.id}": r for r in rows}
    for job in scheduler.get_jobs():
        if job.id.startswith("inspect-") and job.id not in wanted:
            scheduler.remove_job(job.id)
            log.info("巡检任务移除：%s", job.id)
    for jid, r in wanted.items():
        try:
            trigger = CronTrigger.from_crontab(r.cron_expr, timezone="UTC")
        except Exception as e:  # noqa: BLE001 —— 坏 cron 只跳过该任务
            log.warning("巡检 %s 的 cron 表达式非法（%s）：%s", r.id, r.cron_expr, e)
            continue
        existing = scheduler.get_job(jid)
        if existing is None:
            scheduler.add_job(_run_inspection_job, trigger, args=[r.id], id=jid, max_instances=1)
            log.info("巡检任务注册：%s（%s）", jid, r.cron_expr)


async def job_skill_check() -> None:
    """重跑领域技能的连接检查：让"依赖就绪/未就绪"自动跟上环境变化。

    只在技能声明了 requires 时才检查；核心方法论技能恒可用（不检查不排除）。
    """
    from app.services import skills as skills_svc

    try:
        for name, spec in skills_svc.CORE_SKILLS.items():
            if (spec.get("check") or {}).get("requires"):
                await skills_svc.check_skill(name, persist=True)
    except Exception:  # noqa: BLE001 —— 常驻任务不挂主进程
        log.exception("技能连接检查异常")


async def job_prom_scrape() -> None:
    """直采 exporter（FR-B3+）：mode=scrape 的数据源每 30s 拉一次 /metrics，写环形 + 聚合。"""
    from app.services import prom_scrape

    try:
        n = await prom_scrape.scrape_all_enabled()
        if n:
            log.info("exporter 直采成功 %s 个数据源", n)
    except Exception:  # noqa: BLE001 —— 常驻任务不挂主进程
        log.exception("exporter 直采异常")


async def job_retention() -> None:
    """只增表保留期清理（夯实轮补）：避免 alert_his_events / notification_records /
    audit_logs / ai_chat_sessions / metric_agg 无限增长撑爆磁盘。

    保留期在 config 里以天数控制（设 0 = 不清理）。每天 03:00（UTC）跑一次。
    实际删除走分批 + 限量（单次上限 50k 行），避免长事务卡锁。
    """
    from app.core.config import get_settings
    from sqlalchemy import text

    s = get_settings()
    plans: list[tuple[str, str, int]] = []       # (table, ts_col, days)
    if s.alert_his_retention_days > 0:
        plans.append(("alert_his_events", "trigger_time", s.alert_his_retention_days))
    if s.notification_record_retention_days > 0:
        plans.append(("notification_records", "created_at", s.notification_record_retention_days))
    if s.audit_log_retention_days > 0:
        plans.append(("audit_logs", "created_at", s.audit_log_retention_days))
    if s.chat_session_retention_days > 0:
        plans.append(("ai_chat_sessions", "updated_at", s.chat_session_retention_days))
    if not plans:
        return
    batch = 50_000
    total_deleted = 0
    async with SessionLocal() as db:
        for table, col, days in plans:
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            try:
                while True:
                    r = await db.execute(text(
                        f"DELETE FROM {table} WHERE {col} < :cut LIMIT :lim"
                    ), {"cut": cutoff, "lim": batch})
                    n = r.rowcount
                    await db.commit()
                    total_deleted += n
                    if n < batch:
                        break
            except Exception:  # noqa: BLE001
                log.exception("保留期清理异常：%s", table)
        # 聚合表：按 ts（分钟桶）保留，分辨率 1m。
        # 与上面四个表不同：metric_agg 体积大，分批上限收紧到 20k 行。
        if s.alert_his_retention_days > 0:
            cutoff = (datetime.utcnow() - timedelta(days=s.alert_his_retention_days)).strftime("%Y-%m-%d %H:%M:%S")
            try:
                while True:
                    r = await db.execute(text(
                        "DELETE FROM metric_agg WHERE ts < :cut LIMIT :lim"
                    ), {"cut": cutoff, "lim": 20_000})
                    n = r.rowcount
                    await db.commit()
                    total_deleted += n
                    if n < 20_000:
                        break
            except Exception:
                log.exception("保留期清理异常：metric_agg")
    if total_deleted:
        log.info("保留期清理：删除 %s 行", total_deleted)


async def job_chat_suggest() -> None:
    """预生成 AI 助手建议问题（D49）：白天每小时、夜间每 3 小时，见 services/chat_suggest.py。"""
    from app.services import chat_suggest

    await chat_suggest.job_chat_suggest()


async def _run_inspection_job(inspection_id: int) -> None:
    from app.services import inspect as inspect_svc

    try:
        res = await inspect_svc.run_inspection(inspection_id, quota_role="engine")
        log.info("巡检完成 #%s：主机 %s 台 / 异常 %s 项 / 建事件 %s",
                 inspection_id, res["hosts"], len(res["anomalies"]), res["incidents_created"])
    except Exception:  # noqa: BLE001 —— 常驻任务不挂主进程
        log.exception("巡检任务异常 #%s", inspection_id)


async def start_scheduler() -> None:
    scheduler.add_job(job_aggregate, "interval", seconds=60, id="aggregate", max_instances=1)
    scheduler.add_job(job_offline, "interval", seconds=30, id="offline", max_instances=1)
    scheduler.add_job(job_sweep_stale_runbooks, "interval", seconds=60,
                      id="sweep-stale-runbooks", max_instances=1)
    scheduler.add_job(job_alert_freshness, "interval", seconds=30, id="alert-freshness", max_instances=1)
    scheduler.add_job(job_reconcile_inspections, "interval", seconds=60, id="reconcile-inspections", max_instances=1)
    scheduler.add_job(job_skill_check, "interval", minutes=30, id="skill-check", max_instances=1)
    # D157：30s tick 作为兜底（手写 scrape 调度还会按数据源 scrape_interval_s 单独注册）
    scheduler.add_job(job_prom_scrape, "interval", seconds=30, id="prom-scrape-tick", max_instances=1)
    # 保留期清理：每天 03:00 UTC（用户负载低谷；先与 09:00 巡检错开）
    scheduler.add_job(job_retention, CronTrigger.from_crontab("0 3 * * *", timezone="UTC"),
                      id="retention", max_instances=1)
    # AI 助手建议问题预生成（D49）：每小时 07 分触发；夜间窗口判定在任务内部
    # （should_tick：北京时间 23:00–08:00 只跑 23/02/05 点），与其它整点任务错开。
    scheduler.add_job(job_chat_suggest, CronTrigger.from_crontab("7 * * * *", timezone="UTC"),
                      id="chat-suggest", max_instances=1)
    # D157：每个 mode=scrape 的数据源按其 scrape_interval_s 单独注册任务（首次全量 + 后续变更自动重新注册）
    # D161-4：改 await —— 原同步写法打异步引擎必抛 TypeError，独立任务从未注册
    await _safe_register_all_scrape_jobs()
    # D143 预测型自愈：每 60s 对开启预测的策略做趋势外推（只通知不动手）
    from app.services.heal_policy import predict_loop
    scheduler.add_job(predict_loop, "interval", seconds=60, id="heal-predict", max_instances=1)
    scheduler.start()
    log.info("调度器启动：aggregate(60s) / offline(30s) / 巡检对账(60s) / 技能连接检查(30min) / exporter 直采(30s) / 保留期清理(03:00 UTC) / 助手建议预生成(每小时)")



# 已成功注册独立 scrape 任务的数据源 id —— 全局 tick 据此让位，避免同一源被重复拉取
_SCRAPE_JOB_DS_IDS: set[int] = set()


async def _run_scrape_one_async(ds_id: int) -> None:
    """每个 scrape 数据源的执行体（**协程**：调度器是 AsyncIOScheduler，job 直接 await）。

    D162-1（公网部署后实测发现）：D161-4 只修好了"注册"，执行体仍是坏的——
    原 `_run_scrape_one_sync` 用 **同步 `with SessionLocal()`** 查库，而 SessionLocal 是
    **async_sessionmaker**（AsyncSession 不支持同步上下文管理器）→ 每次执行必抛
    `TypeError: 'AsyncSession' object does not support the context manager protocol`；
    而 APScheduler 对非协程 job 会丢进线程池，线程里再 `get_event_loop()` 更不成立。
    文件底部真正的 `_run_scrape_one_async` 竟是个 `pass` 空壳。

    修：执行体改成协程 + `async with SessionLocal()`；查库后把用到的字段**快照**成
    轻量对象再出会话（不跨会话持有 ORM 实体，避免延迟加载炸掉）。
    """
    from types import SimpleNamespace

    from sqlalchemy import select as _select

    from app.db.session import SessionLocal
    from app.models.prom import PromDatasource

    async with SessionLocal() as db:
        row = (await db.execute(
            _select(PromDatasource).where(PromDatasource.id == ds_id))).scalar_one_or_none()
        if row is None or not row.enabled or row.mode != "scrape":
            return
        cfg = dict(row.scrape_config or {})
        ds = SimpleNamespace(
            id=row.id, name=row.name, base_url=row.base_url or "",
            auth_type=row.auth_type or "", secret_encrypted=row.secret_encrypted,
            username=row.username, timeout_s=row.timeout_s or 10,
            scrape_config=cfg,
        )
    await _scrape_coro(ds, cfg)


async def _scrape_coro(ds, cfg: dict) -> None:
    from app.services import prom_scrape
    await prom_scrape.scrape_once(ds, cfg.get("path", "/metrics"),
                                  cfg.get("metric_keep") or [],
                                  user_map=cfg.get("metric_map") or {},
                                  tag_keep=cfg.get("tag_keep") or None)


async def _safe_register_all_scrape_jobs() -> None:
    """每个 mode=scrape 的数据源按其 scrape_interval_s 注册一个 APScheduler 任务。

    D161-4（靶机真机测试发现）：原实现用 **同步 `with SessionLocal()`** 查库——但
    SessionLocal 是 **async_sessionmaker**（`AsyncSession` 不支持上下文管理器）→
    抛 TypeError → 又被 `except: pass` 吞掉 → **独立任务从未注册过**，
    每源间隔（60s/300s）形同虚设，实际只有 30s 全局 tick 在拉（还会被 D161-2
    的"到期判定"再削一次 → 60s 变成 60~90s 抖动）。

    修：改成 async（用 async with SessionLocal），并把注册失败**记日志**而不是静默吞。
    D162-1：执行体换成协程 `_run_scrape_one_async`（此前是坏掉的同步壳）。
    """
    from sqlalchemy import select as _select
    from app.db.session import SessionLocal
    from app.models.prom import PromDatasource as _PD

    global _SCRAPE_JOB_DS_IDS
    try:
        async with SessionLocal() as db:
            rows = (await db.execute(_select(_PD).where(
                _PD.enabled.is_(True), _PD.mode == "scrape"))).scalars().all()
        registered: set[int] = set()
        for ds in rows:
            every = int(getattr(ds, "scrape_interval_s", 30) or 0)
            if every <= 0:
                continue          # 0 = 不自动拉（用户手动触发）
            scheduler.add_job(
                _run_scrape_one_async, "interval", seconds=every,
                args=[ds.id],
                id=f"prom-scrape-{ds.id}", max_instances=1, replace_existing=True,
            )
            registered.add(ds.id)
        _SCRAPE_JOB_DS_IDS = registered
        log.info("scrape 任务注册：%s 个数据源（按各自 scrape_interval_s），已注册 %s 个",
                 len(rows), len(registered))
    except Exception:  # noqa: BLE001 —— 首次启动 DB 可能未就绪
        _SCRAPE_JOB_DS_IDS = set()
        log.exception("scrape 任务注册失败（直采将退回 30s 全局兜底）")
