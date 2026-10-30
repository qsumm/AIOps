"""P5c 回归锁：分级恢复的九条护栏（FR-411）。

设计要点（方案 §4）：**难点不是"敢做"而是"敢不做"**——所以每条护栏都要有
"必须拒"的用例；同时也要有"必须放"的用例，否则护栏会退化成瘫痪（什么都做不了）。
判决逻辑是纯函数（`heal.evaluate_guards`），事实由 `collect_facts` 准备，
因此这里可以脱离数据库穷举各种组合。
"""
import pathlib
import re
from datetime import datetime

import pytest

from app.services import heal


def _v(**kw):
    """默认全绿的一组事实，按需覆盖单个条件。"""
    base = dict(action="restart_service", service=None, guard=None, recent_heals=0,
                host_tasks_last_hour=0, upstream_ok=True, active_runbooks=0, kill_switch=False,
                idle_confirmed=False, dry_run=False)
    base.update(kw)
    return heal.evaluate_guards(**base)


class _Guard:
    def __init__(self, tripped=False, reason=""):
        self.tripped = tripped
        self.tripped_reason = reason


# ── ① 动作白名单 ──────────────────────────────────────────────
def test_guard_action_whitelist():
    assert _v(action="restart_service")["allow"] is True
    for bad in ("reboot", "rm_rf", "restart_service; rm -rf /", "clear_temp_files"):
        v = _v(action=bad)
        assert v["allow"] is False and v["rule"] == "whitelist", f"{bad} 必须被白名单拒"
    # 「清理临时文件」刻意不在首版清单（涉及删除，风险收益比待数据评估）
    assert "clear_temp_files" not in heal.ACTIONS
    src = pathlib.Path(heal.__file__).read_text(encoding="utf-8")
    assert "刻意不在首版清单" in src, "需保留为何不做清理类动作的说明，防后人以为漏了"


# ── ② 限流 ────────────────────────────────────────────────────
def test_guard_rate_limit():
    assert _v(host_tasks_last_hour=heal.HEAL_PER_HOUR - 1)["allow"] is True
    v = _v(host_tasks_last_hour=heal.HEAL_PER_HOUR)
    assert v["allow"] is False and v["rule"] == "rate_limit"
    assert "频繁自愈" in v["reason"], "拒绝原因要解释为什么（不只是说超限）"


# ── ③ 连续失败熔断 ────────────────────────────────────────────
def test_guard_tripped_blocks_until_reset():
    v = _v(guard=_Guard(True, "连续 2 次自愈校验未通过"))
    assert v["allow"] is False and v["rule"] == "tripped"
    assert "重置熔断" in v["reason"], "要告诉用户怎么恢复（重置入口）"
    # 未熔断时可放行
    assert _v(guard=_Guard(False))["allow"] is True


# ── ④ 爆炸半径（单服务；批量禁止在 API 层） ─────────────────
def test_guard_blast_radius_declared():
    v = _v()
    assert any("单次仅对一个服务" in w for w in v["warnings"]), "必须显式声明单服务语义"
    # API 层：不存在批量入口（对抗项：批量的请求应打不到任何路由）
    api = pathlib.Path(heal.__file__).parent.parent.joinpath("routers", "svc.py").read_text(encoding="utf-8")
    assert "heal/bulk" not in api and "batch" not in api.lower().split("def heal_service")[-1][:400], \
        "不得提供批量自愈入口（爆炸半径）"


# ── ⑤ 依赖感知 ────────────────────────────────────────────────
def test_guard_upstream_awareness():
    v = _v(upstream_ok=False)
    assert v["allow"] is False and v["rule"] == "upstream"
    assert "上游" in v["reason"]
    # 演练/仅建议模式下不做依赖阻断（只判决不下发，不产生副作用）
    assert _v(upstream_ok=False, dry_run=True)["allow"] is True


# ── ⑥ 无回滚不执行 ────────────────────────────────────────────
def test_guard_requires_post_check():
    src = pathlib.Path(heal.__file__).read_text(encoding="utf-8")
    assert "未声明后置校验" in src, "缺『无校验不执行』的落地"
    # 每个白名单动作都必须声明后置校验
    for k, v in heal.ACTIONS.items():
        assert v.get("post"), f"动作 {k} 未声明后置校验"


# ── ⑦ 幂等 ────────────────────────────────────────────────────
def test_guard_idempotent_skips_healthy():
    v = _v(idle_confirmed=True)
    assert v["allow"] is False and v["rule"] == "idempotent"
    assert "无需恢复" in v["reason"]


# ── ⑧ 发布窗口互斥 ────────────────────────────────────────────
def test_guard_deploy_window():
    v = _v(active_runbooks=1)
    assert v["allow"] is False and v["rule"] == "deploy_window"
    assert _v(active_runbooks=1, dry_run=True)["allow"] is True, "仅建议不受发布窗口限制"


# ── ⑨ 全局熔断 + 自愈计数 ────────────────────────────────────
def test_guard_kill_switch_and_frequent_heal_warning():
    v = _v(kill_switch=True)
    assert v["allow"] is False and v["rule"] == "kill_switch"
    assert "重新开启" in v["reason"]
    # 全局熔断下仍允许"仅建议"（只给建议不产生动作）
    assert _v(kill_switch=True, dry_run=True)["allow"] is True
    # 频繁自愈：放行但**必须警告**（频繁自愈是缺陷信号）
    v2 = _v(recent_heals=heal.FREQUENT_TRIP)
    assert v2["allow"] is True
    assert any("频繁自愈" in w for w in v2["warnings"])
    assert any("永远不会被修好" in w or "排查根因" in w for w in v2["warnings"])


def test_guard_all_pass_case_is_actionable():
    """全绿时也要给出可解释的判决（避免"通过但不知道为什么通过"）。"""
    v = _v()
    assert v["allow"] is True and v["rule"] == "ok"
    assert v["reason"] and v["warnings"], "通过时也应带上注意事项（爆炸半径声明）"


# ── 通道侧：动作载荷安全（与 agent 侧同口径） ────────────────
def test_action_payload_uses_enum_only():
    """API 层只接受枚举动作 ID，且服务名取自 spec（不接受用户自由文本命令）。"""
    api = pathlib.Path(heal.__file__).parent.parent.joinpath("routers", "svc.py").read_text(encoding="utf-8")
    assert "if body.action not in heal.ACTIONS" in api, "API 层必须先做动作白名单校验"
    assert "cmd" not in api.split("class HealIn")[-1][:1200], "不得接受用户传入的命令文本"
    # 实际下发的载荷只含动作 ID 与目标标识（目标来自声明，不回落显示名）
    assert '"action": body.action' in api and '"service": target' in api


# ── ①b 动作目标：只认声明里的单元名/容器名（D71 真机实测缺陷）────────
def test_resolve_target_never_falls_back_to_display_name():
    """真机跑出来的坑：曾回落成 `spec.unit or s.name`，而显示名可以是中文（"订单服务"）→
    Agent 侧字符集白名单（防注入）把动作 100% 拒掉，用户只看到"服务名含非法字符"。
    目标必须来自声明；声明缺失就明确拒绝并告诉用户去哪填。
    """
    ok, err = heal.resolve_target("restart_service", {"unit": "order-service"})
    assert (ok, err) == ("order-service", None)

    # 未声明 unit → 拒绝，且理由要说清"去哪填"，不能拿显示名去猜
    t, err = heal.resolve_target("restart_service", {"name": "订单服务"})
    assert t == "" and err and "unit" in err and "改声明" in err

    # 声明里出现非 ASCII / shell 元字符 → 拒绝（与 Agent 侧同一白名单）
    for bad in ("订单服务", "order; rm -rf /", "a b", "x$(id)"):
        t, err = heal.resolve_target("restart_service", {"unit": bad})
        assert t == "" and err and "非法字符" in err, f"{bad} 必须被目标字符集拦下"

    # 容器动作认 container，缺省回落到 unit
    assert heal.resolve_target("restart_container", {"container": "mysql-1"})[0] == "mysql-1"
    assert heal.resolve_target("restart_container", {"unit": "mysql"})[0] == "mysql"
    assert heal.resolve_target("restart_container", {})[1], "容器名缺失必须拒绝"


def test_guard_target_error_denies_before_dispatch():
    v = _v(target_error="该服务未声明 systemd 单元名（unit）")
    assert v["allow"] is False and v["rule"] == "target"
    assert "unit" in v["reason"]


def test_platform_and_agent_action_sets_match():
    """机械守卫：平台下拉里出现的动作，Agent 侧必须真的能执行。

    真机实测缺陷：平台提供了「轮转日志（rotate_logs）」，而 Agent 的 SVC_ACTIONS 里
    根本没有这个动作 → 用户选了必然被拒（"动作不在白名单内"）。能力声明与实现必须同源。
    """
    root = pathlib.Path(heal.__file__).resolve().parents[3]
    agent_src = (root / "agent" / "agent_main.py").read_text(encoding="utf-8")
    block = re.search(r"SVC_ACTIONS: dict\[str, dict\] = \{(.*?)\n\}", agent_src, re.S)
    assert block, "未找到 Agent 侧动作矩阵"
    agent_actions = set(re.findall(r'^    "(\w+)": \{', block.group(1), re.M))
    # 平台服务级下拉只暴露 4 个通用动作；Agent 另有 5 个 L3/L4 配置急救动作
    # （由变更单携带 file/service 参数下发，不适合放进服务通用下拉）。
    # 不变量：平台出现的动作 Agent 必须能执行（子集），不是两边必须完全相等。
    assert set(heal.ACTIONS) <= agent_actions, (
        f"平台动作在 Agent 侧不存在：{sorted(set(heal.ACTIONS) - agent_actions)}")
    # 平台声明的目标类型必须与 Agent 的命令形态对得上
    assert heal.ACTIONS["rotate_logs"]["target"] == "unit"
    assert "HUP" in block.group(0) or "HUP" in agent_src, "rotate_logs 的落地实现（HUP 重开日志）缺失"



# ── 动态行为：熔断累计与自愈计量（真跑 DB） ──────────────────
@pytest.mark.asyncio
async def test_heal_records_and_trips_after_two_failures():
    """连续 2 次自愈校验失败 → 该服务熔断（行业口径）；成功一次则清零。

    这是"敢不做"的关键机制：重启无效时**必须自动停下并转人工**，不许无限重试
    （行业经典反例：重启风暴把数据库打崩且再也起不来）。
    """
    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.models.svc import AgentTask, HealGuard, Service, ServiceHeal
    from app.routers.tasks import _record_heal

    async with SessionLocal() as db:
        # 幂等：上一次若在断言处失败会残留 → 先清干净再建（否则 unique ident 撞车）
        old_h = (await db.execute(select(Host).where(Host.ident == "p5c-ident"))).scalar_one_or_none()
        if old_h is not None:
            await db.execute(delete(Service).where(Service.host_id == old_h.id))
            await db.execute(delete(AgentTask).where(AgentTask.host_id == old_h.id))
            await db.execute(delete(Host).where(Host.id == old_h.id))
            await db.commit()
        await db.execute(delete(ServiceHeal).where(ServiceHeal.host_ident == "p5c-host"))
        h = Host(ident="p5c-ident", name="p5c-host", status="up")
        db.add(h)
        await db.commit()
        await db.refresh(h)
        svc = Service(host_id=h.id, name="p5c-svc", type="app", status="down")
        db.add(svc)
        await db.commit()
        await db.refresh(svc)
        await db.execute(delete(HealGuard).where(HealGuard.service_id == svc.id))
        await db.commit()

        def mk_task(ok_post: bool):
            t = AgentTask(host_id=h.id, kind="svc_action",
                          payload_json={"action": "restart_service", "service_id": svc.id})
            db.add(t)
            return t

        # ① 第一次：执行成功但后置校验未通过 → fail_streak=1，未熔断
        t1 = mk_task(False)
        await db.commit()
        await db.refresh(t1)
        await _record_heal(db, t1, {"ok": True, "post_check": {"ok": False}})
        g = (await db.execute(select(HealGuard).where(HealGuard.service_id == svc.id))).scalar_one()
        assert g.fail_streak == 1 and not g.tripped

        # ② 第二次失败 → 熔断
        t2 = mk_task(False)
        await db.commit()
        await db.refresh(t2)
        await _record_heal(db, t2, {"ok": True, "post_check": {"ok": False}})
        g = (await db.execute(select(HealGuard).where(HealGuard.service_id == svc.id))).scalar_one()
        assert g.tripped is True and "连续" in g.tripped_reason

        # ③ 熔断后：护栏必须拒绝下发
        v = heal.evaluate_guards(action="restart_service", service=svc, guard=g, recent_heals=2,
                                 host_tasks_last_hour=0, upstream_ok=True, active_runbooks=0,
                                 kill_switch=False, idle_confirmed=False)
        assert v["allow"] is False and v["rule"] == "tripped"

        # ④ 计数：近 7 天已有 2 次 → 警告"频繁自愈"
        n = len((await db.execute(select(ServiceHeal).where(ServiceHeal.service_id == svc.id))).scalars().all())
        assert n == 2
        # 频繁自愈警告的阈值是 heal.FREQUENT_TRIP（3 次/7 天）——2 次还不算频繁
        v2 = heal.evaluate_guards(action="restart_service", service=svc, guard=None,
                                  recent_heals=heal.FREQUENT_TRIP,
                                  host_tasks_last_hour=0, upstream_ok=True, active_runbooks=0,
                                  kill_switch=False, idle_confirmed=False)
        assert any("频繁自愈" in w for w in v2["warnings"])

        # 清理
        await db.execute(delete(ServiceHeal).where(ServiceHeal.service_id == svc.id))
        await db.execute(delete(HealGuard).where(HealGuard.service_id == svc.id))
        await db.execute(delete(AgentTask).where(AgentTask.host_id == h.id))
        await db.execute(delete(Service).where(Service.id == svc.id))
        await db.execute(delete(Host).where(Host.ident == "p5c-ident"))
        await db.commit()


@pytest.mark.asyncio
async def test_kill_switch_blocks_dispatch_but_allows_suggest():
    """全局熔断：下发被拒（409），但"仅建议"仍可用（只判决不动作）。"""
    from fastapi import HTTPException
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, select

    from app.core.config import get_settings
    from app.db.session import SessionLocal
    from app.main import create_app
    from app.models.host import Host
    from app.models.svc import Service

    app = create_app()
    async with SessionLocal() as db:
        old_h = (await db.execute(select(Host).where(Host.ident == "p5c-ks"))).scalar_one_or_none()
        if old_h is not None:
            await db.execute(delete(Service).where(Service.host_id == old_h.id))
            await db.execute(delete(Host).where(Host.id == old_h.id))
            await db.commit()
        h = Host(ident="p5c-ks", name="p5c-ks", status="up")
        db.add(h)
        await db.commit()
        await db.refresh(h)
        svc = Service(host_id=h.id, name="p5c-ks-svc", type="app", status="down")
        db.add(svc)
        await db.commit()
        await db.refresh(svc)
        sid = svc.id

    st = get_settings()
    old = st.heal_kill_switch
    st.heal_kill_switch = True
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            lg = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            H = {"Authorization": "Bearer " + lg.json()["data"]["access_token"]}
            # 仅建议：允许
            r = await ac.post(f"/api/ops/services/{sid}/heal",
                              headers=H, json={"action": "restart_service", "mode": "suggest"})
            assert r.status_code == 200 and r.json()["data"]["dispatched"] is False
            # 真实下发：必须被熔断拒
            r2 = await ac.post(f"/api/ops/services/{sid}/heal",
                               headers=H, json={"action": "restart_service", "mode": "auto"})
            assert r2.status_code == 409 and "kill_switch" in r2.text, "应因全局熔断被拒（并说明原因与解除方式）"
            # 非白名单动作：400
            r3 = await ac.post(f"/api/ops/services/{sid}/heal",
                               headers=H, json={"action": "reboot", "mode": "auto"})
            assert r3.status_code == 400
            # manual 档未确认：400
            r4 = await ac.post(f"/api/ops/services/{sid}/heal",
                               headers=H, json={"action": "restart_service", "mode": "manual"})
            assert r4.status_code in (400, 409), r4.text
    finally:
        st.heal_kill_switch = old
        async with SessionLocal() as db:
            await db.execute(delete(Service).where(Service.host_id == h.id))
            await db.execute(delete(Host).where(Host.ident == "p5c-ks"))
            await db.commit()


@pytest.mark.asyncio
async def test_incident_context_tolerates_historical_alert_members():
    """含**已恢复成员**（历史告警表）的事件，其「错误上下文」也必须能打开。

    验收实测的 500：`first_trigger_time` 只存在于当前告警表，历史告警没有这一列，
    直接取属性 → AttributeError → 整张卡片打不开（前端只看到 500）。
    真实事件几乎一定含已恢复成员，所以这条必须锁死。
    """
    import hashlib

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.main import create_app
    from app.models.alert import AlertHisEvent
    from app.models.event import Incident
    from app.models.host import Host
    from app.services import ingest

    fp = "p5c-his-member-fp"
    ident = hashlib.sha256(fp.encode()).hexdigest()[:32]
    h = "d7hisctx" + ident[:8]
    async with SessionLocal() as db:
        await db.execute(delete(AlertHisEvent).where(AlertHisEvent.hash == h))
        await db.execute(delete(Incident).where(Incident.source_key == ("sk:" + h)[:32]))
        host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
        if host is None:
            tok = ingest.mint_install_token()
            await ingest.register_agent(db, {"fingerprint": fp, "hostname": "p5c-his",
                                            "agent_version": "0.3.0", "install_token": tok})
            host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        db.add(AlertHisEvent(hash=h, rule_id=1, rule_name="演示-探活失败", host_ident=ident,
                             metric="订单服务端口", severity=1, trigger_time=datetime.utcnow(),
                             trigger_value=1.0, tags={}, is_recovered=True, recover_time=datetime.utcnow()))
        inc = Incident(title="P5C-含历史成员事件", host_ident=ident, status="open", severity=1,
                       source_key=("sk:" + h)[:32], alert_hashes=[h])
        db.add(inc)
        await db.commit()
        await db.refresh(inc)
        iid = inc.id

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tok = (await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()
        tok = tok["data"]["access_token"]
        r = await c.get(f"/api/ops/incidents/{iid}/context", headers={"Authorization": "Bearer " + tok})
        assert r.status_code == 200, f"含历史成员的上下文接口不得 500：{r.status_code} {r.text[:200]}"
        tl = r.json()["data"]["timeline"]
        assert tl["total"] >= 1, tl

    async with SessionLocal() as db:
        await db.execute(delete(Incident).where(Incident.id == iid))
        await db.execute(delete(AlertHisEvent).where(AlertHisEvent.hash == h))
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()
