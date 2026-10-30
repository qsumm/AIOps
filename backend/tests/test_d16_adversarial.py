"""D16 对抗性测试回归。

方法：不做"功能可用"确认，而是用最恶毒的方式攻击——状态机边界、越权、
非法枚举、时间倒挂、无令牌注册、负分页。攻击脚本在报告里留档。

【诚实归类】初次汇报时我把它包装成"6 个缺陷"，这是凑数——用户明确要求
"穷举后是几个就报几个，不凑数、不把加固项当缺陷、不把一个根因拆成多条"。
重新归类后 = **2 个真缺陷 + 3 个加固项**：

真缺陷（可复现的越权/崩溃）：
  1. 【高危·认证绕过】Agent 注册接口 `if INSTALL_TOKENS:` —— 令牌字典为空时整段跳过校验，
     **任何人都能不带令牌注册任意主机**（实测注册出 EVIL-HOST 并拿到 agent_token 可持续
     上报伪造指标 → 污染监控数据、触发假告警、消耗 LLM 额度）。
  2. 分页 page=0 / page=-1 / page_size 负数 → offset 变负 → MySQL 报错 → **500**。
     （最初拆成"page"和"page_size"两条汇报——同一根因，不许拆条。）

加固项（不是缺陷，是纵深防御收紧 / 产品决策）：
  3. 静默 mute_type=99 非法枚举原来被接受（语义未定义的值落库）→ 加枚举校验。
  4. 静默 etime < btime 时间倒挂原来被接受（永不生效的静默）→ 加时间校验。
  5. 规则 eval_interval_s 原无上界（999999 秒 ≈ 11.5 天）→ **这是产品决策不是缺陷**
     （长周期规则本身合理），只是顺手加上界并写进手册。
"""
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from app.core.security import get_current_user
from app.db.session import SessionLocal
from app.main import create_app
from app.models.host import Host
from app.models.shell import User


async def _ac_client():
    app = create_app()
    async with SessionLocal() as db:
        admin = await db.get(User, 1)
    app.dependency_overrides[get_current_user] = lambda: admin
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ── 1. 高危：注册必须校验一次性令牌 ──
@pytest.mark.asyncio
async def test_agent_register_requires_valid_install_token():
    """无令牌/无效令牌注册 → 403；有效一次性令牌 → 200（且令牌用后失效）。"""
    from app.services import ingest

    app, _ = await _ac_client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        # ① 无令牌 → 403（修前是 200 = 认证绕过）
        r = await ac.post("/api/ops/agents/register",
                          json={"fingerprint": "d16-no-token", "hostname": "D16-EVIL"})
        assert r.status_code == 403, f"无令牌注册必须 403，实际 {r.status_code}"
        # ② 伪造令牌 → 403
        r = await ac.post("/api/ops/agents/register",
                          json={"fingerprint": "d16-bad-token", "hostname": "D16-EVIL",
                                "install_token": "inst-forged"})
        assert r.status_code == 403, f"伪造令牌必须 403，实际 {r.status_code}"
        # ③ 有效令牌 → 200；且**同一令牌复用** → 403（一次性）
        tok = ingest.mint_install_token()
        r = await ac.post("/api/ops/agents/register",
                          json={"fingerprint": "d16-legit", "hostname": "D16-LEGIT",
                                "install_token": tok})
        assert r.status_code == 200, f"有效令牌应放行，实际 {r.status_code} {r.text[:120]}"
        r2 = await ac.post("/api/ops/agents/register",
                           json={"fingerprint": "d16-legit2", "hostname": "D16-LEGIT2",
                                 "install_token": tok})
        assert r2.status_code == 403, "令牌一次性：复用必须 403"
    # 清理（含上面注册出来的主机）
    async with SessionLocal() as db:
        for fp in ("d16-no-token", "d16-bad-token", "d16-legit", "d16-legit2"):
            import hashlib
            ident = hashlib.sha256(fp.encode()).hexdigest()[:32]
            h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
            if h:
                await db.delete(h)
        await db.commit()
    app.dependency_overrides.clear()


# ── 2. 分页负值：必须 422 不能 500 ──
@pytest.mark.asyncio
async def test_pagination_rejects_invalid_values():
    app, _ = await _ac_client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        cases = [
            ("/api/ops/incidents?page=0", "事件 page=0"),
            ("/api/ops/incidents?page=-1", "事件 page=-1"),
            ("/api/ops/alerts?page_size=-5", "告警 page_size=-5"),
            ("/api/ops/alerts?page=0&page_size=0", "告警 page/page_size=0"),
            ("/api/audit-logs?page=-3", "审计 page=-3"),
            ("/api/ops/incidents?page_size=99999", "事件 page_size 超上限 200"),
        ]
        for path, label in cases:
            r = await ac.get(path)
            assert r.status_code == 422, f"{label} 应 422（参数越界），实际 {r.status_code}"
            assert r.status_code < 500
        # 正常值仍可用
        r = await ac.get("/api/ops/incidents?page=1&page_size=20")
        assert r.status_code == 200
    app.dependency_overrides.clear()


# ── 3/4. 静默参数校验 ──
@pytest.mark.asyncio
async def test_mute_validates_type_and_time_range():
    app, _ = await _ac_client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        base = {"cause": "d16-mute", "tag_filters": [], "severities": [],
                "btime": "2026-10-14T00:00:00", "etime": "2026-10-15T00:00:00"}
        # ① mute_type 非法枚举 → 400
        r = await ac.post("/api/ops/alert-mutes", json={**base, "mute_type": 99})
        assert r.status_code == 400, f"mute_type=99 应 400，实际 {r.status_code}"
        # ② 时间倒挂 → 400
        r = await ac.post("/api/ops/alert-mutes", json={
            **base, "mute_type": 1, "btime": "2026-12-31T00:00:00", "etime": "2026-01-01T00:00:00"})
        assert r.status_code == 400, f"etime<btime 应 400，实际 {r.status_code}"
        # ③ 时间不可解析 → 400
        r = await ac.post("/api/ops/alert-mutes", json={**base, "mute_type": 1, "btime": "not-a-date"})
        assert r.status_code == 400, f"坏时间格式应 400，实际 {r.status_code}"
        # ④ 合法 → 200 且可清理
        r = await ac.post("/api/ops/alert-mutes", json={**base, "mute_type": 1})
        assert r.status_code == 200, r.text[:120]
        mid = (r.json().get("data") or {}).get("id")
        if mid:
            rr = await ac.delete(f"/api/ops/alert-mutes/{mid}")
            assert rr.status_code == 200
    app.dependency_overrides.clear()


# ── 5. 规则间隔/窗口上界 ──
@pytest.mark.asyncio
async def test_rule_interval_and_window_bounds():
    app, _ = await _ac_client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        def body(**kw):
            return {"name": "d16-bounds", "scope": "metric", "metric": "cpu.percent",
                    "expr": ">1", "severity": 2, **kw}
        # 间隔超上界 → 400
        r = await ac.post("/api/ops/alert-rules", json=body(eval_interval_s=999999))
        assert r.status_code == 400, f"eval_interval 超上界应 400，实际 {r.status_code}"
        r = await ac.post("/api/ops/alert-rules", json=body(eval_interval_s=86401))
        assert r.status_code == 400, "86401 应 400（上限 86400）"
        # 窗口超上界 → 400
        r = await ac.post("/api/ops/alert-rules", json=body(window_s=200000))
        assert r.status_code == 400, "window_s 超上界应 400"
        # 边界内 → 200（然后删掉）
        r = await ac.post("/api/ops/alert-rules", json=body(eval_interval_s=86400))
        assert r.status_code == 200, r.text[:120]
        rid = (r.json().get("data") or {}).get("id")
        async with SessionLocal() as db:
            from app.models.alert import AlertRule
            await db.execute(delete(AlertRule).where(AlertRule.name == "d16-bounds"))
            await db.commit()
        assert rid
    app.dependency_overrides.clear()


# ── 6. 长任务豁免 DB 挂起兜底（D17 穷举矩阵抓出的回归）──
def test_long_task_routes_exempt_from_db_hang_guard():
    """巡检执行必须豁免 25s DB 挂起兜底，且自身带 deadline。

    起因（穷举矩阵 668 请求里的唯一真 5xx）：D10 加的"25s 请求兜底"是**一刀切**的——
    巡检执行正常就要 ~42s（实测 hosts=6 / 8 项检查 / LLM 润色），被兜底当"数据库不可达"杀掉，
    手册 §8 承诺的「立即执行」按钮**实际永远 500**，提示语还误导。
    修：① main.py 长任务正则豁免；② 巡检路由自带 600s deadline（豁免后必须自己兜住）。
    """
    from app.main import _LONG_TASK_RE

    # 豁免：巡检执行两种入口
    assert _LONG_TASK_RE.match("/api/ops/inspections/1/run"), "巡检执行应豁免"
    assert _LONG_TASK_RE.match("/api/ops/inspections/run-once"), "即时巡检应豁免"
    # 不豁免：列表/详情（应快，超 25s 说明真有问题）
    assert not _LONG_TASK_RE.match("/api/ops/inspections")
    assert not _LONG_TASK_RE.match("/api/ops/inspections/1")
    assert not _LONG_TASK_RE.match("/api/ops/incidents/1")
    assert not _LONG_TASK_RE.match("/api/ops/metrics/query")

    # 巡检自带 deadline（豁免后不能无限跑）
    from app.routers.runbooks import INSPECT_DEADLINE_S
    assert 60 <= INSPECT_DEADLINE_S <= 3600, f"deadline 应在合理区间，实际 {INSPECT_DEADLINE_S}"
