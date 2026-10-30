"""FR-415 回归锁：**服务状态告警**（scope=service）。

背景（为什么要有它）：P5 的「服务监控」能把 MySQL 连接打满、应用进程消失这类问题判成
异常/不可用，但告警引擎原先只认 metric / probe / prom 三种数据来源——**服务红了不会叫人**，
只有正在盯服务页的人才看得到。本模块把服务判定接进既有链路（告警 → 收敛事件 → 诊断 → 取证 → 恢复）。

本文件锁三件事：
  ① 取值语义：up→0 / degraded→1 / down→2，**unknown 不参与**（信息缺失≠故障，拿它报警就是假警）；
  ② 过滤与降级：可按服务名只看一个；停用的服务不参与；expr `>0` 报任何异常、`>1` 只在不可用时报；
  ③ 入参白名单：scope 是 ENUM 列，写入非法值会被**静默截断**（D47-1 同款坑）→ 接口必须显式挡下。
"""
import hashlib
from datetime import datetime

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.engine import core
from app.models.alert import AlertRule
from app.models.host import Host
from app.models.svc import Service
from app.services import ingest


@pytest.fixture
async def svc_host():
    """造一台主机 + 三个服务（up / degraded / down）+ 两个停用/未知，用完清理。"""
    fp = "fr415-svc-scope-fp"
    ident = hashlib.sha256(fp.encode()).hexdigest()[:32]
    async with SessionLocal() as db:
        await db.execute(delete(Service).where(Service.name.like("FR415-%")))
        h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
        if h is None:
            tok = ingest.mint_install_token()
            await ingest.register_agent(db, {"fingerprint": fp, "hostname": "fr415-host",
                                            "agent_version": "0.3.0", "install_token": tok})
            h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        db.add_all([
            Service(host_id=h.id, name="FR415-好服务", type="app", status="up", enabled=True),
            Service(host_id=h.id, name="FR415-降级服务", type="app", status="degraded", enabled=True),
            Service(host_id=h.id, name="FR415-挂了服务", type="app", status="down", enabled=True),
            Service(host_id=h.id, name="FR415-停用服务", type="app", status="down", enabled=False),
            Service(host_id=h.id, name="FR415-未知服务", type="app", status="unknown", enabled=True),
        ])
        await db.commit()
        hid, hident = h.id, h.ident
    yield {"host_id": hid, "ident": hident}
    async with SessionLocal() as db:
        await db.execute(delete(Service).where(Service.name.like("FR415-%")))
        await db.commit()


def _rule(expr: str = ">0", metric: str = ""):
    """构造一条内存里的 service 规则（不落库：引擎只读它的字段）。"""
    return AlertRule(id=990001, name="FR415-测试规则", scope="service", metric=metric,
                     expr=expr, severity=1, eval_interval_s=30, window_s=300,
                     for_duration_s=0, recover_duration_s=60, algorithm="threshold",
                     disabled=False, effective_time=[], queries_json=[], append_tags={})


@pytest.mark.asyncio
async def test_service_scope_mapping_and_filters(svc_host):
    """取值映射 + 停用/未知过滤 + 服务名过滤。"""
    # 只断言本用例造的服务：测试库里还有演示/调试服务（D7 教训：别依赖库现状）
    def mine(out):
        return {labels["metric"]: (val, labels["service_status"])
                for labels, val in out if labels["metric"].startswith("FR415-")}

    w = core.RuleWorker(_rule(">0"))
    got = mine(await w._eval_service_once())
    assert got == {"FR415-降级服务": (1.0, "degraded"), "FR415-挂了服务": (2.0, "down")}, \
        f"expr>0 该命中异常与不可用（up 不该命中），实际 {got}"

    w2 = core.RuleWorker(_rule(">1"))
    got2 = {k: v[0] for k, v in mine(await w2._eval_service_once()).items()}
    assert got2 == {"FR415-挂了服务": 2.0}, f"expr>1 只该命中不可用，实际 {got2}"

    # 停用服务不参与；unknown 不参与（信息缺失不等于故障）
    assert "FR415-停用服务" not in got and "FR415-未知服务" not in got

    # 按服务名只看一个
    w3 = core.RuleWorker(_rule(">=0", metric="FR415-降级服务"))
    got3 = {labels["metric"]: val for labels, val in await w3._eval_service_once()}
    assert got3 == {"FR415-降级服务": 1.0}, got3


@pytest.mark.asyncio
async def test_service_scope_labels_carry_context(svc_host):
    """标签要带主机、服务名、类型与状态——告警详情/报告要靠它说清"哪台机器的哪个服务"。"""
    async with SessionLocal() as db:
        await db.execute(Service.__table__.update().where(
            Service.name == "FR415-挂了服务").values(note="进程、端口、健康端点均不通——服务不可用"))
        await db.commit()
    w = core.RuleWorker(_rule(">1"))
    out = [x for x in await w._eval_service_once() if x[0]["metric"].startswith("FR415-")]
    assert len(out) == 1, out
    labels, val = out[0]
    assert labels["host"] == svc_host["ident"]
    assert labels["metric"] == "FR415-挂了服务"
    assert labels["service_status"] == "down"
    assert "服务不可用" in labels["note"]
    assert val == 2.0


@pytest.mark.asyncio
async def test_rule_api_accepts_service_scope_and_rejects_bogus():
    """接口层：scope=service 必须能建；非法 scope 必须 400（ENUM 会静默截断，不能靠库报错）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tok = (await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()
        tok = tok["data"]["access_token"]
        h = {"Authorization": "Bearer " + tok}

        r = await c.post("/api/ops/alert-rules", headers=h, json={
            "name": "FR415-服务不可用", "scope": "service", "metric": "", "expr": ">1",
            "severity": 1, "eval_interval_s": 30, "window_s": 300})
        assert r.status_code == 200, r.text
        rid = r.json()["data"]["id"]
        async with SessionLocal() as db:
            row = await db.get(AlertRule, rid)
            assert row.scope == "service", f"scope 被截断了（ENUM 未同步）：{row.scope!r}"
            assert row.for_duration_s == 0
        await c.delete(f"/api/ops/alert-rules/{rid}?purge=true", headers=h)

        # 非法 scope：必须显式 400（若靠 ENUM 兜，会被静默截断成空串，规则永不评估）
        r2 = await c.post("/api/ops/alert-rules", headers=h, json={
            "name": "FR415-非法scope", "scope": "svc", "metric": "x", "expr": ">0", "severity": 2})
        assert r2.status_code == 400, r2.text
        assert "metric" in r2.json()["msg"] and "service" in r2.json()["msg"]

        # 指标类仍必须填指标名；探活/服务类允许留空（引擎明确支持留空=全部）
        r3 = await c.post("/api/ops/alert-rules", headers=h, json={
            "name": "FR415-空指标", "scope": "metric", "metric": "", "expr": ">0", "severity": 2})
        assert r3.status_code == 400, r3.text
        r4 = await c.post("/api/ops/alert-rules", headers=h, json={
            "name": "FR415-空探活", "scope": "probe", "metric": "", "expr": ">0", "severity": 2})
        assert r4.status_code == 200, r4.text
        rid4 = r4.json()["data"]["id"]
        await c.delete(f"/api/ops/alert-rules/{rid4}?purge=true", headers=h)


def test_frontend_exposes_service_scope():
    """前端编辑器必须给出「服务状态」选项，且说明文字与后端语义一致（留空=全部服务）。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    js = (root / "static" / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
    assert '"service", "服务状态（服务监控）"' in js, "规则编辑器缺「服务状态」数据来源"
    assert "留空=该主机全部服务" in js, "目标名说明必须写清留空语义（否则用户不敢留空）"
    assert 'f.scope.value === "metric" ? needText(f.metric.value' in js, \
        "只有指标作用域必须填目标名（探活/服务留空是合法用法）"


# ═══════════════════ FR-416 案例库（可看/可改/可停用/可删） ═══════════════════
@pytest.mark.asyncio
async def test_case_card_crud_and_enabled_filtering():
    """案例库页面依赖的四个能力：列表（含搜索/状态筛选）、编辑（重算向量）、停用、删除。

    为什么必须有"停用/删除"（FR-416）：案例卡是平台学到的东西，但它可能是错的——
    错卡会一直参与向量召回、持续污染后续诊断（D69 实测：一张短泛化卡把 Top-1 从 78% 拉到 67%）。
    依赖真实 embedding（D97 补）：LLM 套餐到顶（429）时本测试会假失败——此时诚实 skip，
    配额恢复后恢复执行（非代码缺陷）。
    """
    from httpx import ASGITransport, AsyncClient

    # 先探测 LLM 可用性：不可用 → skip（不假装通过）
    from app.core.config import get_settings
    _ = get_settings()
    from app.db.session import SessionLocal
    from app.services.llm_gateway import call_llm
    try:
        async with SessionLocal() as _db:
            await call_llm(_db, "selftest", [{"role": "user", "content": "回答：ok"}],
                           model_role="fast", quota_role="admin")
    except Exception as e:
        import pytest as _pt
        _pt.skip(f"LLM 暂不可用（{type(e).__name__}），依赖真实 embedding 的用例跳过：{str(e)[:80]}")
        return
    from sqlalchemy import text

    from app.main import create_app
    from app.services import cases as cases_svc

    # 造两张卡：一张正常、一张文本极短（不该有向量）
    cid = await cases_svc.create_case_card(
        None, "FR416-订单提交报连接池耗尽，前端 5xx", "慢查询占满连接池（缺索引）",
        "给 orders(status) 加索引并调大 maxPoolSize", ["mysql", "连接池"])
    cid2 = await cases_svc.create_case_card(None, "FR416-短", "FR416-短", "", [])   # 合计 15 字 < 16 阈值
    assert cid and cid2

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tok = (await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()
        h = {"Authorization": "Bearer " + tok["data"]["access_token"]}

        # 列表 + 关键词搜索 + 状态筛选
        r = await c.get("/api/ops/case-cards", headers=h, params={"q": "FR416", "page_size": 50})
        items = {x["id"]: x for x in r.json()["data"]["items"]}
        assert cid in items and cid2 in items, "关键词搜索应能定位到自己造的卡"
        assert items[cid]["has_embedding"] is True, "正常长度的文本应生成向量"
        assert items[cid2]["has_embedding"] is False, "过短文本必须跳过向量（D69 纪律）"
        assert r.json()["data"]["total"] >= 2

        # 编辑：改症状 → 重算向量；内容变短 → 清空向量
        r = await c.patch(f"/api/ops/case-cards/{cid}", headers=h,
                          json={"symptom": "FR416-改名后的症状描述（足够长以保留向量）"})
        assert r.status_code == 200, r.text
        r = await c.get("/api/ops/case-cards", headers=h, params={"q": "FR416-改名后"})
        assert r.json()["data"]["items"][0]["has_embedding"] is True, "改症状后应重算向量"
        # 症状与根因一起改短 → 合计 5 字 < 阈值 → 向量被清空（与写入时同一套纪律）
        r = await c.patch(f"/api/ops/case-cards/{cid}", headers=h, json={"symptom": "短了", "root_cause": "短因"})
        assert r.status_code == 200
        r = await c.get("/api/ops/case-cards", headers=h, params={"q": "短了"})
        still = [x for x in r.json()["data"]["items"] if x["id"] == cid]
        assert still and still[0]["has_embedding"] is False, "被改短后必须清空向量"

        # 空症状/空根因拒绝（要停用请用 enabled，而不是清空内容）
        assert (await c.patch(f"/api/ops/case-cards/{cid}", headers=h, json={"symptom": "   "})).status_code == 422
        assert (await c.patch(f"/api/ops/case-cards/{cid}", headers=h, json={"root_cause": ""})).status_code == 422

        # 停用 → 不再参与召回；按状态筛选能查到
        r = await c.patch(f"/api/ops/case-cards/{cid}", headers=h, json={"enabled": False})
        assert r.status_code == 200
        r = await c.get("/api/ops/case-cards", headers=h, params={"q": "FR416-改名后", "enabled": "1"})
        assert not [x for x in r.json()["data"]["items"] if x["id"] == cid], "停用后不该出现在「参与检索」筛选里"
        async with SessionLocal() as db:
            kw = "%FR416-改名后%"
            hit = (await db.execute(text(
                "SELECT COUNT(*) FROM case_cards WHERE enabled = 1 AND symptom LIKE :q"), {"q": kw})).scalar()
            assert int(hit) == 0, "停用的卡仍被召回查询选中了（enabled 过滤没生效）"

        # 删除
        assert (await c.delete(f"/api/ops/case-cards/{cid}", headers=h)).status_code == 200
        assert (await c.delete(f"/api/ops/case-cards/{cid}", headers=h)).status_code == 404
        assert (await c.patch(f"/api/ops/case-cards/{99999999}", headers=h, json={"enabled": True})).status_code == 404

        # 权限：demo（只读）不得改/删
        dtok = (await c.post("/api/auth/demo", json={})).json()
        if dtok.get("data", {}).get("access_token"):
            dh = {"Authorization": "Bearer " + dtok["data"]["access_token"]}
            assert (await c.patch(f"/api/ops/case-cards/{cid2}", headers=dh, json={"enabled": False})).status_code == 403
            assert (await c.delete(f"/api/ops/case-cards/{cid2}", headers=dh)).status_code == 403

    await cases_svc.delete_case_card(cid2)


@pytest.mark.asyncio
async def test_case_card_list_returns_tags_as_array():
    """列表接口的 `tags` **必须永远是数组**。

    用户实测报障：案例库「查看 / 编辑」点了没反应。根因是 case_cards 走 raw SQL，
    MySQL 的 JSON 列由驱动返回的是**字符串**（`'["demo","证书"]'`），而前端 `tags.join(...)`
    → TypeError → onclick 静默抛错 → 按钮看起来"坏了"。契约必须锁住。
    """
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from app.main import create_app
    from app.services import cases as cases_svc

    cid = await cases_svc.create_case_card(None, "FR417-契约卡（够长的症状描述）", "FR417-根因描述", "", ["契约", "标签"])
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tok = (await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()
        h = {"Authorization": "Bearer " + tok["data"]["access_token"]}
        r = await c.get("/api/ops/case-cards", headers=h, params={"q": "FR417-契约卡"})
        item = [x for x in r.json()["data"]["items"] if x["id"] == cid][0]
        assert isinstance(item["tags"], list), f"tags 应是数组，实际 {type(item['tags']).__name__}"

    await cases_svc.delete_case_card(cid)

    # 归一函数本身也要覆盖各形态：驱动给什么形状都不能让调用方炸
    # （MySQL 的 JSON 列不会存非法 JSON——实测 UPDATE 非 JSON 文本会被库直接拒绝——
    #   但驱动返回"字符串"这件事是真实的，所以必须容错。）
    from app.routers.runbooks import _as_list
    assert _as_list(["a"]) == ["a"]                    # 已是数组
    assert _as_list('["a", "b"]') == ["a", "b"]        # 驱动给的 JSON 字符串（真实形态）
    assert _as_list("not-json") == []                  # 脏串 → 空数组，不抛
    assert _as_list(None) == [] and _as_list({}) == [] and _as_list("") == []
