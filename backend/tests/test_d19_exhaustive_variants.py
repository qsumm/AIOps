"""D19 穷举变体回归：路径参数「非快乐值」暴露的 2 个真缺陷。

背景——用户要求「站在挖掘缺陷的角度穷举测试，挖到几个就报几个，不许凑数」。
我对全部 88 条路由 × 5 种攻击向量（未认证 / demo 写 / 路径参数变异 / 查询参数变异 /
请求体变异）跑了 676 个请求。路径参数变异（V3）**只换 id 取值**就暴露 2 个真缺陷：

  1. 【中间件误杀】`POST /api/ops/inspections/-1/run` → **500 @25s**。
     根因：D10 的 DB 挂起兜底正则 `^/api/ops/inspections/(\\d+/run|run-once)$` 用 `\\d+`
     匹配 id，**负号不匹配** → `-1` 不被豁免 → 被 25s 兜底当"数据库不可达"杀掉。
     注意 `-1` 是**合法 int**（能过 FastAPI 校验进业务逻辑），不是脏输入。
  2. 【静默跑全量】不存在的巡检 id（`0` / `999999999999`）→ **200 @~40s**，
     实际跑了一次**全量巡检**（遍历全部主机 + LLM 润色 + 异常项自动建事件）。
     根因：`run_inspection` 未校验 id 是否存在，`job=None` 使 scope 被当空集处理。
     用户传错 id 不但不报错，还白烧 40 秒 + LLM 额度 + 污染事件看板。

方法论教训（写下来防复发）：上一轮修 D10 误杀时我只验了 `id=1`（存在的 id），
漏了负 id 与不存在的 id ——**"只测主路径，不测降级/边界路径"是我反复犯的错**。
变体测试必须覆盖：存在 / 不存在 / 0 / 负数 / 超大值 / 非数字。
"""
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.shell import User


async def _ac_client():
    app = create_app()
    async with SessionLocal() as db:
        admin = await db.get(User, 1)
    app.dependency_overrides[get_current_user] = lambda: admin
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ── 缺陷 1：负 id 必须被长任务兜底豁免（否则 500） ──
def test_long_task_regex_covers_negative_ids():
    """豁免正则必须覆盖负数 id——`-1` 是合法 int，会进入业务逻辑。

    否则：请求不被豁免 → 25s 兜底杀 → 500，且提示语（"疑似数据库不可达"）完全误导。
    """
    from app.main import _LONG_TASK_RE

    for pid in ("1", "0", "-1", "-999", "999999999999"):
        assert _LONG_TASK_RE.match(f"/api/ops/inspections/{pid}/run"), \
            f"巡检 id={pid} 应豁免长任务兜底（负号必须匹配）"
    assert _LONG_TASK_RE.match("/api/ops/inspections/run-once")

    # 非整数 / 列表 / 详情仍不应豁免（应快速失败，慢就是真问题）
    for path in ("/api/ops/inspections/abc/run", "/api/ops/inspections",
                 "/api/ops/inspections/1", "/api/ops/incidents/1"):
        assert not _LONG_TASK_RE.match(path), f"{path} 不应豁免"


# ── 缺陷 2：不存在的巡检 id → 404（不得静默跑全量） ──
@pytest.mark.asyncio
async def test_nonexistent_inspection_id_returns_404():
    """不存在的 id 必须 404——不得静默执行一次全量巡检。

    危害：每次误传 id 白烧 ~40s + LLM 额度 + 生成若干事件，用户却以为在跑"那个任务"。
    除了状态码，还断言**耗时 < 3s**——这是"没有真的跑全量"的直接证据
    （全量巡检实测 33~48s，若静默执行则不可能 3s 返回）。
    """
    import time
    app, ac = await _ac_client()
    async with ac as c:
        for bad in ("-1", "0", "999999999999"):
            t0 = time.monotonic()
            r = await c.post(f"/api/ops/inspections/{bad}/run", json={})
            el = time.monotonic() - t0
            assert r.status_code == 404, \
                f"id={bad} 应 404，实际 {r.status_code}（若 200 则为静默跑全量）"
            msg = r.json().get("msg", "")
            assert "不存在" in msg or "已被删除" in msg, f"提示语应明确，实际 {msg!r}"
            assert el < 3, f"id={bad} 耗时 {el:.1f}s —— 说明真的去跑全量了（应 404 快速返回）"


# ── 类型错误仍应由 FastAPI 拦在路由前 ──
@pytest.mark.asyncio
async def test_non_integer_inspection_id_returns_422():
    """非整数 id → 422（校验层拦截，不进业务、不触发长任务）。"""
    app, ac = await _ac_client()
    async with ac as c:
        for bad in ("abc", "1e999", "1.5", " "):
            r = await c.post(f"/api/ops/inspections/{bad}/run", json={})
            assert r.status_code == 422, f"id={bad!r} 应 422，实际 {r.status_code}"


# ── 回归：存在的 id 未被误伤 ──
@pytest.mark.asyncio
async def test_existing_inspection_still_runs():
    """存在的巡检 id 仍正常执行（修复不得误伤主路径）。"""
    from app.routers.runbooks import INSPECT_DEADLINE_S
    from app.models.ops import Inspection
    from sqlalchemy import select

    app, ac = await _ac_client()
    async with SessionLocal() as db:
        row = (await db.execute(select(Inspection).order_by(Inspection.id))).scalars().first()
    assert row is not None, "测试库应至少有一条巡检任务"
    async with ac as c:
        r = await c.post(f"/api/ops/inspections/{row.id}/run", json={},
                         timeout=max(INSPECT_DEADLINE_S + 30, 120))
        assert r.status_code == 200, f"存在的 id={row.id} 应 200，实际 {r.status_code}"
        assert r.status_code != 500


# ══════════════════════════════════════════════════════════════════════════
# D20：同一根因的第二例 —— "只修被点名的实例，不修那一类"
# ══════════════════════════════════════════════════════════════════════════
# D19 我只把**巡检**路由加进长任务豁免，结果 `POST /api/ops/incidents/{iid}/diagnose`
# 同样是长任务（多步 ReAct + LLM）却被 25s DB 兜底杀掉 → 500 @25.0s。
# LLM 额度恢复后才暴露（额度耗尽时诊断秒失败，反而"看不出"问题）。
#
# 结构性修法：从"路径豁免清单"改成 **LONG_TASK_ROUTES 表（正则 → 各自兜底秒数）**，
# 并加下面这条**机械守卫**：所有"跑外部动作"的接口都必须登记，否则测试红。


# 跑外部动作的接口命名特征（这类接口耗时不可控，必须登记在长任务表里）
_RUNTIME_ACTION_PAT = {
    "diagnose", "execute", "confirm", "scrape-now", "generate",
    "check", "toggle", "test", "run", "run-once",
}

# 命名像"动作"但**确认无外部工作**的例外（纯本地 DB 操作 → 不会超 25s）。
# 每个例外都必须写明依据，避免这张豁免名单变成"为了过测试而放水"。
_LOCAL_ONLY_ACTIONS = {
    # 只是翻转 enabled 标志位 + commit，没有任何外部调用（routers/runbooks.py:368 已核对）
    "/api/ops/skills/{sid}/toggle": "纯 DB 翻标志位，无外部调用",
    # 同上：翻转 disabled 标志位 + commit（routers/alerts.py toggle_rule 已核对，D61）
    "/api/ops/alert-rules/{rid}/toggle": "纯 DB 翻标志位，无外部调用",
}


def test_all_runtime_action_routes_are_registered_as_long_tasks():
    """机械守卫：凡是"同步跑外部动作"的接口，必须登记进 LONG_TASK_ROUTES。

    否则它会被 25s DB 挂起兜底误杀成 500 —— 这正是 D19/D20 连犯两次的根因。
    """
    from app.main import create_app, LONG_TASK_ROUTES, long_task_deadline

    app = create_app()
    spec = app.openapi()
    unregistered = []
    for path in spec["paths"]:
        if not path.startswith("/api"):
            continue
        # 末段是否命中"动作"特征（含 /stream 的流式入口不算长任务：它立刻返回、后续才流）
        tail = path.rstrip("/").split("/")[-1]
        if path in _LOCAL_ONLY_ACTIONS:
            continue  # 命名像动作但确认纯本地（依据见 _LOCAL_ONLY_ACTIONS 注释）
        if tail in _RUNTIME_ACTION_PAT and not path.endswith("/stream"):
            if long_task_deadline(path.replace("{iid}", "1").replace("{rid}", "1")
                                      .replace("{sid}", "1").replace("{cfg_id}", "1")
                                      .replace("{incident_id}", "1").replace("{did}", "1")
                                      .replace("{cid}", "1").replace("{step_idx}", "0")
                                      .replace("{mid}", "1")) is None:
                unregistered.append(path)
    assert not unregistered, (
        "以下接口会同步跑外部动作却没登记进 LONG_TASK_ROUTES，会被 25s 兜底误杀成 500：\n  "
        + "\n  ".join(unregistered)
    )
    # 表本身的形状约束
    assert LONG_TASK_ROUTES, "长任务表不得为空"
    for pat, secs in LONG_TASK_ROUTES.items():
        assert secs > 25, f"{pat} 的兜底 {secs}s 必须 > 默认 25s，否则登记无意义"


@pytest.mark.asyncio
async def test_diagnose_is_not_killed_by_db_hang_guard():
    """AI 诊断不得再被 25s DB 兜底误杀（D20 的核心回归）。

    用真实 LLM 需要额度，故这里只断言：**诊断路由命中了长任务表**，
    且其兜底上限远大于 25s。真正的端到端 LLM 验证见《可验证场景清单》§6。
    """
    from app.main import long_task_deadline

    for iid in ("1000033", "1", "-1"):
        dl = long_task_deadline(f"/api/ops/incidents/{iid}/diagnose")
        assert dl is not None, f"diagnose(iid={iid}) 必须登记为长任务，否则 25s 兜底会误杀"
        assert dl > 60, f"诊断兜底应留有充足余量，实际 {dl}s"

    # 非长任务仍走 25s（别把兜底整体关掉）
    for p in ("/api/ops/incidents", "/api/ops/incidents/1", "/api/ops/alerts",
              "/api/ops/inspections", "/api/ops/dashboard/overview"):
        assert long_task_deadline(p) is None, f"{p} 不应被当成长任务"

    # 巡检/处置/技能/外呼探活 均在表内
    assert long_task_deadline("/api/ops/inspections/1/run") is not None
    assert long_task_deadline("/api/ops/inspections/run-once") is not None
    assert long_task_deadline("/api/ops/runbooks/1/steps/0/confirm") is not None
    assert long_task_deadline("/api/ops/runbooks/1/execute") is not None
    assert long_task_deadline("/api/ops/skills/1/check") is not None
    assert long_task_deadline("/api/llm-configs/1/test") is not None
