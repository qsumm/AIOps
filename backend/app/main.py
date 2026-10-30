"""FastAPI 应用工厂——D1 版：路由挂载 + 调度器 + 种子管理员。"""
import asyncio
import contextvars
import json
import time
import uuid
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import SessionLocal, dispose_all_engines
from app.models.host import Base  # noqa: F401
from app.models.shell import User  # noqa: F401

import os

# 容器里静态资源单独挂载（/static），本机开发默认取仓库 static/
STATIC_DIR = Path(os.getenv("STATIC_DIR") or (Path(__file__).resolve().parents[2] / "static"))
log = logging.getLogger("app")


# D171（交付规范化）：request_id —— 每请求一个可追溯标识。
# 用户报障带响应头/响应体里的 X-Request-ID，运维 grep 日志即可定位该请求的
# 全部日志（含错误堆栈）。contextvar 跨 async 任务安全。
_request_id_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id_ctx.get()


class _JsonFormatter(logging.Formatter):
    """JSON 日志（FR §2.3「标准库 logging（JSON 格式化）」）。

    不引入第三方依赖；字段固定 ts/level/logger/msg/request_id + 异常栈，
    便于容器日志采集（filebeat/loki）直接解析，不用写 grok。
    """

    def format(self, record: logging.LogRecord) -> str:
        import json as _json

        payload = {
            "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": current_request_id(),
        }
        for k, v in (getattr(record, "extra_fields", None) or {}).items():
            payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-4000:]
        return _json.dumps(payload, ensure_ascii=False)


class _PlainFormatter(logging.Formatter):
    """可读日志（LOG_JSON 未开启时的默认）。

    与 JSON 版字段对齐：时间 / 级别 / logger 名 / request_id / 消息。
    request_id 放在最前——排查时先按它 grep，再按时间读。
    """

    default_time_format = "%Y-%m-%d %H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        rid = current_request_id()
        head = f"{self.formatTime(record, self.datefmt)} {record.levelname:<7} {record.name:<18}"
        if rid and rid != "-":
            head += f"[{rid}] "
        out = f"{head} {record.getMessage()}"
        for k, v in (getattr(record, "extra_fields", None) or {}).items():
            out += f" {k}={v}"
        if record.exc_info:
            out += "\n" + self.formatException(record.exc_info)
        return out


def setup_logging() -> None:
    """配置根日志。

    D176 修（公网实测发现）：此前**只在 LOG_JSON=true 时配置根 logger**，否则根 logger
    没有 handler → 应用自身的 INFO 日志（技能自动路由命中、LLM 降级、向量通道降级、
    告警投递失败……）被 Python **静默丢弃**，容器里只剩 uvicorn 的访问日志——
    运维最需要看的业务日志恰恰看不见（本次排查"技能自动路由命中"没出现，就是它）。

    现在两种模式都配置根 logger：LOG_JSON=true 用 JSON 单行；否则用可读格式。
    uvicorn 的自带 handler 一并摘掉交给根 logger 统一输出（否则访问日志打两遍）。
    """
    json_mode = bool(get_settings().log_json)
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter() if json_mode else _PlainFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # uvicorn 自带 handler 会重复打印 → 交给根 logger 统一输出
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate = [], True


setup_logging()


async def seed_admin() -> None:
    """首次启动种子：admin/admin123（README 注明首登必改）+ demo 用户。"""
    from app.models.shell import User as U

    async with SessionLocal() as db:
        if (await db.execute(select(U).where(U.username == "admin"))).scalar_one_or_none() is None:
            db.add(U(username="admin", password_hash=hash_password("admin123"), role="admin"))
            db.add(U(username="demo", password_hash=hash_password("demo"), role="demo"))
            await db.commit()
            log.warning("已种子用户 admin/admin123 与 demo/demo——请立即修改默认密码")


async def _seed_d5() -> None:
    """D5 种子：默认巡检任务（每日 09:00）+ 内置核心技能入库（幂等）。"""
    from app.models.ops import Inspection
    from app.services import inspect as inspect_svc
    from app.services import skills as skills_svc

    async with SessionLocal() as db:
        if (await db.execute(select(Inspection).limit(1))).scalar_one_or_none() is None:
            db.add(inspect_svc.default_inspection())
            await db.commit()
            log.warning("已创建默认巡检任务（每日 09:00）")
    try:
        n = await skills_svc.sync_builtin_skills()
        if n:
            log.warning("已同步内置技能 %s 条", n)
    except Exception:  # noqa: BLE001 —— 技能库异常不阻塞启动
        log.exception("内置技能同步失败")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.sched import start_scheduler

    await seed_admin()
    await start_scheduler()
    from app.engine.core import start_engine

    start_engine()
    await _seed_d5()
    yield
    from app.sched import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    await dispose_all_engines()


# ---- 长任务路由表（不再给"豁免"，而是给**各自的兜底时限**）----
#
# 背景：D10 加的"25s DB 挂起兜底"是一刀切的。凡"同步执行外部动作"的接口
# （跑巡检、调 LLM 诊断、执行处置步骤、探活外部依赖）**正常耗时就可能超过 25s**，
# 被兜底当成"数据库不可达"杀掉 → 500，且提示语完全误导。
#
# 教训（D19→D20 连犯两次）：第一版只把**被点名的**巡检路由加了进去，结果
# `POST /api/ops/incidents/{iid}/diagnose` 同样 500 却漏网。所以这里改成**一张表**：
# 想加长任务接口就得在这登记，另有测试守卫（见 test_d19 的 D20 用例）机械核对
# "所有跑外部动作的接口是否都登记了"，防止再犯"只修实例不修根因"。
#
# 值 = 该路由的兜底上限（秒）。语义是"防真挂死"的保险丝，**不是**预期耗时：
# 每个路由内部仍必须有自己的超时/降级（LLM 单次 180s、巡检/诊断自带 deadline）。
LONG_TASK_ROUTES: dict[str, int] = {
    r"^/api/ops/inspections/(-?\d+/run|run-once)$": 600,        # 巡检：主机 × 8 项检查 + LLM 润色
    r"^/api/ops/incidents/-?\d+/diagnose$": 900,                 # AI 诊断：多步 ReAct + LLM
    r"^/api/ops/runbooks/generate/-?\d+$": 300,                  # AI 生成处置方案
    r"^/api/ops/runbooks/-?\d+/(execute|steps/-?\d+/confirm)$": 300,     # 执行/确认处置步骤（真跑动作）
    r"^/api/ops/skills/-?\d+/check$": 300,                       # 技能校验
    r"^/api/llm-configs/-?\d+/test$": 120,                       # LLM 连通性测试（外呼）
    r"^/api/ops/notify-rules/-?\d+/test$": 60,                    # D154：模拟事件试投（走一遍筛选求值）
    r"^/api/ops/prom-datasources/-?\d+/(test|scrape-now)$": 120,  # Prom 抓取（外呼）
    r"^/api/ops/notify-channels/-?\d+/test$": 60,                # 通知渠道测试（外呼）
    # 规则试运行：scope=db 时是本地评估，但 scope=prom 时会**外呼 Prometheus 取序列**
    # （见 routers/alerts.py test_rule）——机械守卫测试把我这条漏登记抓了出来。
    r"^/api/ops/alert-rules/-?\d+/test$": 120,
    # D140 自愈确认：重新评估护栏 + 下发 + 审计，可能耗时
    "^/api/ops/heal-policy-runs/\\d+/confirm$": 40,
}
# 兼容旧引用（测试里断言过 _LONG_TASK_RE）：语义 = "是否属于长任务"
_LONG_TASK_RE = re.compile("|".join(f"(?:{k})" for k in LONG_TASK_ROUTES))


def long_task_deadline(path: str) -> int | None:
    """命中长任务表 → 返回该路由的兜底上限（秒）；否则 None（走默认 25s）。"""
    for pat, secs in LONG_TASK_ROUTES.items():
        if re.match(pat, path):
            return secs
    return None



def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    # 夯实轮加固：DB 挂起兜底。aiomysql 没有 read_timeout（build 阶段
    # 实测拒绝），MAX_EXECUTION_TIME 在 SeekDB 上不按预期截断，而 pool_pre_ping
    # 只能丢弃"已知坏"的旧连接，拦不住"一个长查询挂住 60s"——所有 API 同
    # 时 hang 到客户端超时。最外层用 asyncio.wait_for 给整条请求套 25s 兜底，
    # 超时 → 抛 TimeoutError → 落进全局未预期异常 handler → 返回 500 + 统一信封。
    # 健康检查、License 激活、静态资源白名单豁免（不要把 health 也超时干掉）。
    # D163-8：请求体解析失败（非 JSON / 二进制垃圾 / 编码错）→ 422 人话。
    # 此前走 Exception 兜底 500 且泄漏 "TypeError: Object of type bytes is not JSON
    # serializable"——请求方发坏数据不是服务端 bug，必须 4xx。放 middleware 层
    # （create_app 作用域、注册时机确定），不依赖 lifespan。
    # D171（交付规范化）：request_id + 统一访问日志。
    # ① 生成（透传上游 X-Request-ID）→ contextvar（所有日志自动带）→ 响应头返回；
    # ② 访问日志：method/path/status/耗时/request_id —— uvicorn access 只有前四样，
    #    没有关联键；报障时"这个 500 的 request_id 是什么"必须有答案。
    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = _request_id_ctx.set(rid)
        t0 = time.time()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("请求异常 %s %s", request.method, request.url.path)
            raise
        finally:
            _request_id_ctx.reset(token)
        resp.headers["X-Request-ID"] = rid
        # 静态资源与健康检查不记访问日志（高频噪音）
        if not (request.url.path.startswith(("/static", "/js", "/css", "/images", "/manual"))
                or request.url.path in ("/api/health", "/favicon.ico")):
            log.info("%s %s -> %s %.0fms",
                     request.method, request.url.path, resp.status_code,
                     (time.time() - t0) * 1000)
        return resp

    @app.middleware("http")
    async def _bad_body_422(request: Request, call_next):
        try:
            return await call_next(request)
        except RequestValidationError:
            return JSONResponse(
                {"code": 422, "msg": "请求参数不合法——请检查字段与取值", "data": None}, status_code=422)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"code": 422, "msg": "请求体不是合法的 JSON——请检查 Content-Type 与内容格式", "data": None},
                status_code=422)
        except TypeError as e:
            if "not JSON serializable" in str(e) and request.method in ("POST", "PUT", "PATCH"):
                return JSONResponse(
                    {"code": 422, "msg": "请求体不是合法的 JSON——请检查 Content-Type 与内容格式", "data": None},
                    status_code=422)
            raise

    @app.middleware("http")
    async def _db_hang_guard(request: Request, call_next):
        path = request.url.path
        # 免超时白名单：健康检查 / 安装件分发 / 静态资源
        if path in ("/api/health", "/api/health/") or path.startswith("/install") or path.startswith("/vendor") or path.startswith("/js") or path.startswith("/css") or path.endswith(".html") or path.endswith(".png") or path.endswith(".svg"):
            return await call_next(request)
        # 长任务豁免（D17 对抗测试修复 · 这是 D10 引入的回归）：
        # 巡检执行会遍历「主机 × 8 项检查 + LLM 润色」，**正常耗时就可能 > 25s**；
        # 统一的 25s DB 挂起兜底把它当"数据库不可达"杀掉 →
        # 手册 §8 承诺的「立即执行」按钮**实际永远 500**，且提示语误导。
        # 长任务自己的超时由内部保证（LLM 单次 180s；巡检整体 deadline 见 services/inspect.py）。
        # 长任务：不再"完全豁免"（那等于没有保险丝），而是换成**该路由自己的兜底时限**。
        # 超时返回 504 + 明确提示（区别于"数据库不可达"的 500 —— 后者会把排查带偏）。
        dl = long_task_deadline(path)
        if dl is not None:
            try:
                return await asyncio.wait_for(call_next(request), timeout=dl)
            except asyncio.TimeoutError:
                log.warning("长任务超时 %ss: %s %s", dl, request.method, path)
                return JSONResponse({"code": 504, "msg": f"长任务执行超时（>{dl}s），已中止；请缩小范围后重试", "data": None}, status_code=504)
        try:
            return await asyncio.wait_for(call_next(request), timeout=25)
        except asyncio.TimeoutError:
            log.warning("请求超时 25s（疑似 DB 挂起）: %s %s", request.method, path)
            return JSONResponse({"code": 500, "msg": "服务端响应超时（疑似数据库不可达，DB 恢复后下个请求会自愈）", "data": None}, status_code=500)

    # ---- 静态资源强制协商缓存（D9 实测问题）----
    # 免构建前端没有"文件名带指纹"这种缓存失效手段（/js/pages/runbooks.js 永远是这个路径），
    # 且 StaticFiles 默认不带 Cache-Control → 升级后浏览器可能继续用**旧 JS**，
    # 表现为"代码改了但界面还是老样子"（实测踩到：改了处置页却仍是旧列）。
    # 这里统一改成 no-cache（每次都协商，命中就走 304），代价极小、语义正确。
    @app.middleware("http")
    async def static_revalidate(request: Request, call_next):
        resp = await call_next(request)
        path = request.url.path
        if (path.startswith(("/js/", "/vendor/")) or path in ("/", "/index.html")
                or path.endswith((".css", ".html"))):
            resp.headers["Cache-Control"] = "no-cache"
        # 手册媒体（视频/海报）同理：同一 URL 会被多次替换成新版本，浏览器对 206 分片
        # 的启发式缓存会让客户继续看**旧视频**（实测：换新成片后仍播旧版，字幕对不上）。
        if path.startswith("/manual/videos/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ---- 统一错误信封（测试阶段修正）----
    # 05-接口设计约定所有响应为 {code,msg,data}，但 FastAPI 默认把 HTTPException 序列化成
    # {"detail": ...}、参数校验错序列化成 {"detail":[...]}。前端只读 msg，于是 403 在界面上
    # 退化成"请求失败：HTTP 403"（D8 实测缺陷）。这里把两类错误统一成同一形状。
    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return JSONResponse(
            {"code": exc.status_code, "msg": str(exc.detail), "data": None},
            status_code=exc.status_code, headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        first = (exc.errors() or [{}])[0]
        loc = ".".join(str(x) for x in first.get("loc", ()) if x not in ("body", "query", "path"))
        msg = first.get("msg", "参数不合法")
        return JSONResponse(
            {"code": 422, "msg": f"请求参数不合法（{loc}: {msg}）" if loc else f"请求参数不合法（{msg}）",
             "data": {"errors": exc.errors()}},
            status_code=422,
        )

    # ---- 数据库异常 → 可读的 4xx（测试阶段修正）----
    # 实测：重名（唯一约束）直接 500 并把 SQL 堆栈打进日志，用户在界面上只看到"HTTP 500"；
    # 字段超长/枚举不匹配（DataError）同样 500。这两类都是**可预期的用户输入问题**，
    # 必须给 409/400 + 人话，而不是 500。
    from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

    @app.exception_handler(IntegrityError)
    async def _integrity_exc(request: Request, exc: IntegrityError):
        log.warning("唯一约束冲突 %s %s: %s", request.method, request.url.path, exc.orig)
        return JSONResponse(
            {"code": 409, "msg": "同名资源已存在或违反唯一约束，请换个名称后重试", "data": None},
            status_code=409)

    @app.exception_handler(DataError)
    async def _data_exc(request: Request, exc: DataError):
        log.warning("字段取值不合法 %s %s: %s", request.method, request.url.path, exc.orig)
        return JSONResponse(
            {"code": 400, "msg": f"字段取值不合法：{str(exc.orig)[:160]}", "data": None},
            status_code=400)

    @app.exception_handler(SQLAlchemyError)
    async def _db_exc(request: Request, exc: SQLAlchemyError):
        log.exception("数据库异常 %s %s", request.method, request.url.path)
        return JSONResponse({"code": 500, "msg": "数据库操作失败，请稍后重试或查看服务端日志", "data": None},
                            status_code=500)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        """兜底：任何未预期异常也返回统一 JSON 信封（不把 HTML/纯文本堆栈丢给前端）。

        D11 加：把异常 message 也带出（"ValueError: deadline exceeded" 这种对操作人有用
        ——知道是哪个值/哪个超时；对攻击者无用，因为我们从不回显堆栈/路径/代码片段）。
        msg 截断到 200 字符防止极端长 message 把响应撑大。
        """
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        # D163-8：body: dict 的端点在收到二进制垃圾时抛 TypeError（bytes 不是可序列化对象）
        # ——这是请求方发坏数据，归 422，不当服务端故障。
        if isinstance(exc, TypeError) and "not JSON serializable" in str(exc) \
                and request.method in ("POST", "PUT", "PATCH"):
            return JSONResponse(
                {"code": 422, "msg": "请求体不是合法的 JSON——请检查 Content-Type 与内容格式", "data": None},
                status_code=422)
        msg = str(exc) or type(exc).__name__
        msg = msg[:200] + ("..." if len(msg) > 200 else "")
        return JSONResponse({"code": 500, "msg": f"服务端异常：{type(exc).__name__}: {msg}",
                             "data": None, "request_id": current_request_id()},
                            status_code=500,
                            headers={"X-Request-ID": current_request_id()})

    from app.routers import (agent_install, alerts, audit, auth, collect, incidents, mcp, metrics,
                             notify_rules, prom, runbooks, shell, tasks, svc)

    app.include_router(agent_install.router)  # /install.sh + /agent_main.py（FR-B2）
    app.include_router(auth.router)
    app.include_router(auth.api_router)  # /api/sse-ticket（05 §8）
    app.include_router(alerts.router)
    app.include_router(incidents.router)
    app.include_router(runbooks.router)
    app.include_router(prom.router)
    app.include_router(tasks.router)   # P5：平台→Agent 任务通道（FR-409/411）
    app.include_router(svc.router)     # P5：服务级监控（FR-408）
    app.include_router(shell.router)
    app.include_router(audit.router)
    app.include_router(collect.router)
    app.include_router(__import__("app.routers.heal_policies", fromlist=["router"]).router)  # D140 自动自愈策略
    app.include_router(notify_rules.router)  # D154：通知规则/订阅/业务组（FR-423）
    app.include_router(metrics.router)      # D154：指标即时查询（FR-424）
    app.include_router(mcp.router)          # D154：MCP 端点（FR-425，默认关闭）
    app.include_router(__import__("app.routers.eval", fromlist=["router"]).router)  # D184：诊断评测（FR-428，admin）

    @app.middleware("http")
    async def license_gate(request, call_next):
        """FR-A4：无效 License 时核心 API 403 + 引导激活；登录/健康/License/静态资源免门禁。"""
        from app.services import license_state as lic

        path = request.url.path
        if not lic.allow_path(path):
            st = await lic.license_state()
            if st["state"] == "locked":
                from fastapi.responses import JSONResponse

                return JSONResponse({"code": 403, "msg": f"LICENSE_LOCKED：{st['reason']}", "data": st},
                                    status_code=403)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict:
        # D66：健康接口带版本（前端左下角版本号 / 版本信息页的唯一数据源）
        from app.core.version import version_payload

        return {"code": 0, "msg": "ok", "data": {"app": settings.app_name, "status": "up", **version_payload()}}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
