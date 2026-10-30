"""D173（FR-427a~d）：技能体系升级 P1 自动路由 / P2 授权命令 / P3 GitHub 导入 / P4 效果度量。

设计依据：《14-技能体系升级-设计与测试》§4.1 的用例表（T-A1~T-A3、T-B1~T-B4、T-C1~T-C3、T-D1~T-D2）。

守住的硬边界（任何一条破掉都算事故）：
  · denylist 优先于一切——技能授权**不能**放行危险命令；
  · 技能授权命令只认**完全相等**，不做前缀匹配（否则可构造逃逸）；
  · 效果指标只从真实流水算；样本 <5 如实报「样本不足」，不硬算比率；
  · GitHub 导入只允许 github.com / raw.githubusercontent.com（SSRF 边界）。
"""
from __future__ import annotations

import pytest

from app.services import skills as skills_svc
from app.services import rca_tools


# ─────────────────────────── P1：自动路由（T-A1~T-A3）───────────────────────────
def test_scope_hit_metric_fnmatch():
    """T-A1：metric 走 fnmatch 通配；规则名走子串。"""
    assert skills_svc._scope_hit(["mem.*"], ["mem.percent"], []) is True
    assert skills_svc._scope_hit(["mem.percent"], ["mem.percent"], []) is True
    assert skills_svc._scope_hit(["cpu.*"], ["mem.percent"], []) is False
    # 规则名常带序号前缀（"1020-磁盘使用率"），模式按子串命中
    assert skills_svc._scope_hit(["磁盘"], [], ["1020-磁盘使用率"]) is True
    assert skills_svc._scope_hit(["*内存*"], [], ["内存使用率过高"]) is True
    assert skills_svc._scope_hit(["磁盘"], [], ["CPU 使用率高"]) is False


def test_scope_hit_empty_patterns_never_hit():
    """T-A3：空范围永不命中（存量用户技能零影响）。"""
    assert skills_svc._scope_hit([], ["mem.percent"], ["内存"]) is False
    assert skills_svc._scope_hit(["", "  "], ["mem.percent"], []) is False


def test_match_skills_respects_limit_and_skips_core(monkeypatch):
    """T-A2：命中数超限只取前 N；core-* 常驻方法论不占自动注入名额。"""
    import pytest_asyncio  # noqa: F401  （下方用 pytest.mark.asyncio 装饰器）

    class _Row:
        def __init__(self, name, scope, prio):
            self.name, self.alerts_scope, self.rca_priority = name, scope, prio
            self.description = name
            self.body_md = f"# {name}"
            self.enabled = True
            self.connection_check = {}

    rows = [_Row("core-investigation", ["*"], 1),
            _Row("a-skill", ["mem.*"], 5),
            _Row("b-skill", ["mem.*"], 6),
            _Row("c-skill", ["mem.*"], 7)]

    class _Scalars:
        def all(self):
            return rows

    class _Result:
        def scalars(self):
            return _Scalars()

    class _DB:
        async def execute(self, *a, **k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(skills_svc, "SessionLocal", lambda: _DB())

    import asyncio
    hits = asyncio.run(skills_svc.match_skills(["mem.percent"], [], limit=2))
    names = [x["name"] for x in hits]
    assert names == ["a-skill", "b-skill"], f"应取优先级前 2 且跳过 core-*，实际 {names}"
    assert all("auto-loaded" in x["wrapped"] for x in hits)


def test_match_skills_skips_empty_scope_and_unhealthy(monkeypatch):
    """T-A3 + fail-closed：空 scope 不参与；连接检查失败的技能不参与。"""
    class _Row:
        def __init__(self, name, scope, cc):
            self.name, self.alerts_scope, self.rca_priority = name, scope, 5
            self.description, self.body_md, self.enabled = name, "", True
            self.connection_check = cc

    rows = [_Row("empty-scope", [], {}),
            _Row("broken", ["mem.*"], {"ok": False}),
            _Row("healthy", ["mem.*"], {"ok": True})]

    class _Scalars:
        def all(self):
            return rows

    class _Result:
        def scalars(self):
            return _Scalars()

    class _DB:
        async def execute(self, *a, **k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(skills_svc, "SessionLocal", lambda: _DB())
    import asyncio
    hits = asyncio.run(skills_svc.match_skills(["mem.percent"], []))
    assert [x["name"] for x in hits] == ["healthy"]


# ─────────────────────────── P2：授权命令（T-B1~T-B4）───────────────────────────
def test_skill_command_allowed_exact_match():
    """T-B1：白名单外的命令，凭技能授权（完全相等）放行。"""
    cmd = 'mysql -e "SHOW FULL PROCESSLIST"'
    ok, reason = rca_tools.authorize_readonly(cmd, allowed=[cmd])
    assert ok is True, reason


def test_skill_command_denylist_still_wins():
    """T-B2：危险命令即使被技能授权也一律拒绝。

    两条不同的拒绝路径都要成立：
      · `rm -rf /`      —— 命中 denylist（根目录删除）；
      · `rm -rf /tmp/x` —— denylist 只拦根目录删除，但**只读形状校验**会拦掉它
        （首词 rm 不在只读工具集）——这正是技能授权路径必须额外过形状校验的原因。
    """
    ok, reason = rca_tools.authorize_readonly("rm -rf /", allowed=["rm -rf /"])
    assert ok is False
    assert "禁用" in reason or "denylist" in reason.lower()

    ok2, reason2 = rca_tools.authorize_readonly("rm -rf /tmp/x", allowed=["rm -rf /tmp/x"])
    assert ok2 is False
    assert "只读" in reason2


def test_skill_command_is_exact_not_prefix():
    """T-B3：技能授权是**完全相等**匹配，不是前缀。

    反例一：授权 `mysql -e "SHOW FULL PROCESSLIST"`，跑 `mysql -e "SHOW FULL PROCESSLIST; rm -rf /"` → 拒
    反例二：授权 `mysql -e "SHOW FULL PROCESSLIST"`，跑 `mysql -e "DROP DATABASE x"` → 拒
    （注：`du -sh /` 这类本身就在只读前缀白名单里的命令不受影响——那是既有行为，
      技能授权路径只决定"白名单外的命令能不能被技能放行"。）
    """
    allowed = ['mysql -e "SHOW FULL PROCESSLIST"']
    ok, _ = rca_tools.authorize_readonly('mysql -e "SHOW FULL PROCESSLIST; rm -rf /"', allowed=allowed)
    assert ok is False
    ok2, _ = rca_tools.authorize_readonly('mysql -e "DROP DATABASE x"', allowed=allowed)
    assert ok2 is False


def test_readonly_prefix_still_allowed_without_skill():
    """回归：没有技能授权时，只读白名单行为与现状完全一致。"""
    ok, _ = rca_tools.authorize_readonly("df -h")
    assert ok is True
    ok2, _ = rca_tools.authorize_readonly("mysql -e 'select 1'")
    assert ok2 is False


def test_agent_side_skill_command(monkeypatch):
    """T-B4：Agent 侧同样支持 allowed_commands，且危险模式仍硬拦。

    Agent 是独立进程（agent/agent_main.py），这里按路径加载其模块做单测。
    """
    import importlib.util
    import pathlib

    agent_py = pathlib.Path(__file__).resolve().parents[2] / "agent" / "agent_main.py"
    assert agent_py.exists(), "找不到 agent/agent_main.py"
    spec = importlib.util.spec_from_file_location("d173_agent_main", agent_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cmd = 'mysql -e "SHOW FULL PROCESSLIST"'
    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda c, **k: calls.append(c) or type("R", (), {"stdout": "ok", "stderr": "", "returncode": 0})())
    r = mod.run_readonly({"cmd": cmd, "allowed_commands": [cmd]})
    assert r.get("ok") is True, r
    assert calls == [cmd]

    # 危险命令：授权也拒
    r2 = mod.run_readonly({"cmd": "rm -rf /tmp/x", "allowed_commands": ["rm -rf /tmp/x"]})
    assert r2.get("ok") is False
    # 精确匹配：授权 mysql 只读语句，另一个 SQL 必须被拒
    r3 = mod.run_readonly({"cmd": 'mysql -e "DROP DATABASE x"',
                           "allowed_commands": ['mysql -e "SHOW FULL PROCESSLIST"']})
    assert r3.get("ok") is False


def test_parse_skill_md_commands_block_list():
    """SKILL.md 块数组解析：命令里的引号/逗号不能被拆坏。"""
    spec = skills_svc.parse_skill_md(
        '---\n'
        'name: demo-skill\n'
        'description: 演示\n'
        'alerts: [disk.*, 磁盘]\n'
        'commands:\n'
        '  - \'mysql -e "SHOW FULL PROCESSLIST, STATUS"\'\n'
        '  - du -sh /var/log/*\n'
        '---\n正文\n')
    assert spec["name"] == "demo-skill"
    assert spec["alerts_scope"] == ["disk.*", "磁盘"]
    assert spec["commands"] == ['mysql -e "SHOW FULL PROCESSLIST, STATUS"', "du -sh /var/log/*"]


# ─────────────────────────── P3：GitHub 导入（T-C1~T-C3）───────────────────────────
def test_github_url_allowlist():
    """T-C2：非 GitHub 域名一律拒绝（SSRF 边界）。"""
    for bad in ["http://192.168.1.1/x", "http://evil.com/repo/tree/main/x",
                "file:///etc/passwd", "https://github.com.evil.com/a/b"]:
        with pytest.raises(ValueError):
            skills_svc._github_raw_url(bad)


def test_github_url_normalize():
    """blob / tree / raw 三种形态都归一到 raw 地址。"""
    assert skills_svc._github_raw_url(
        "https://github.com/o/r/blob/main/skills/x/SKILL.md") == \
        "https://raw.githubusercontent.com/o/r/main/skills/x/SKILL.md"
    assert skills_svc._github_raw_url(
        "https://github.com/o/r/tree/main/skills/x") == \
        "https://raw.githubusercontent.com/o/r/main/skills/x/SKILL.md"
    assert skills_svc._github_raw_url(
        "https://raw.githubusercontent.com/o/r/main/SKILL.md") == \
        "https://raw.githubusercontent.com/o/r/main/SKILL.md"


def test_github_import_bad_frontmatter_not_stored(monkeypatch):
    """T-C3：缺 description 的文件不入库（复用既有必填强校验）。"""
    monkeypatch.setattr(skills_svc, "_fetch_text",
                        lambda url, **k: "---\nname: broken\n---\n正文")
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(skills_svc.import_from_github(
            "https://github.com/o/r/tree/main/skills/x"))


# ─────────────────────────── P4：效果度量（T-D1~T-D2）───────────────────────────
def test_effect_sample_insufficient(monkeypatch):
    """T-D2：样本不足 → insufficient=True（前端显示「样本不足」，不硬算比率）。"""
    import asyncio

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _DB:
        def __init__(self):
            self.calls = 0

        async def execute(self, sql, params=None):
            s = str(sql)
            self.calls += 1
            if "FROM skills" in s:
                return _Result([("demo-skill",)])
            if "tool_name='load_skill'" in s:
                return _Result([("demo-skill", 1), ("demo-skill", 2)])   # 仅 2 个事件
            if "finding_triage" in s:
                return _Result([])
            return _Result([])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(skills_svc, "SessionLocal", lambda: _DB())
    out = asyncio.run(skills_svc.skill_effects())
    eff = out["demo-skill"]
    assert eff["insufficient"] is True
    assert eff["loaded"] == 2
    assert "followup_rate" not in eff


def test_effect_followup_rate_computed(monkeypatch):
    """T-D1：样本 ≥5 时按真实流水算跟进率与基线。"""
    import asyncio

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _DB:
        async def execute(self, sql, params=None):
            s = str(sql)
            if "FROM skills" in s:
                return _Result([("demo-skill",)])
            if "tool_name='load_skill'" in s:
                # 该技能被 6 个事件用到
                return _Result([("demo-skill", i) for i in range(1, 7)])
            if "finding_triage" in s:
                # 全平台 8 个事件有处置：1,2,3,4,5,6,7 → followup；8 → dismissed
                return _Result([(i, "followup") for i in range(1, 8)] + [(8, "dismissed")])
            if "rca_findings" in s:
                return _Result([(i, 100 * i) for i in range(1, 7)])
            return _Result([])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(skills_svc, "SessionLocal", lambda: _DB())
    out = asyncio.run(skills_svc.skill_effects())
    eff = out["demo-skill"]
    assert eff["insufficient"] is False
    assert eff["loaded"] == 6
    assert eff["followup_rate"] == 1.0            # 用到的 6 个事件全部 followup
    assert eff["baseline_rate"] == 0.875          # 7/8
    assert eff["token_median"] is not None


# ─────────────────────── API 层（落库 / 效果端点 / 导入拒绝）───────────────────────
@pytest.mark.asyncio
async def test_api_skill_scope_and_commands_persist():
    """技能编辑后，适用告警模式与授权命令必须**真的入库**（交付规范化要求）。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.main import app
    from app.models.ops import Skill

    name = "d173-test-skill"
    async with SessionLocal() as db:
        await db.execute(delete(Skill).where(Skill.name == name))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        r = await ac.post("/api/ops/skills", headers=hdr, json={
            "name": name, "description": "D173 守卫用例",
            "body_md": "## 何时用\n测试\n",
            "alerts_scope": ["disk.*", " 磁盘 ", "", "disk.*"],
            "commands": ['mysql -e "SHOW FULL PROCESSLIST"', "mysql -e \"SHOW FULL PROCESSLIST\""],
        })
        assert r.status_code == 200, r.text
        sid = r.json()["data"]["id"]
        async with SessionLocal() as db:
            row = (await db.execute(select(Skill).where(Skill.name == name))).scalar_one()
            assert row.alerts_scope == ["disk.*", "磁盘"], row.alerts_scope     # 去空白/去重
            assert row.commands == ['mysql -e "SHOW FULL PROCESSLIST"'], row.commands
            await db.execute(delete(Skill).where(Skill.id == row.id))
            await db.commit()


@pytest.mark.asyncio
async def test_api_skill_effect_endpoint():
    """效果端点返回可复算的计数（样本不足时如实给 insufficient）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        lst = await ac.get("/api/ops/skills", headers=hdr)
        assert lst.status_code == 200
        items = lst.json()["data"]["items"]
        assert items, "技能列表不应为空（内置技能存在）"
        one = items[0]
        assert "effect" in one and "loaded" in one["effect"]
        r = await ac.get(f"/api/ops/skills/{one['id']}/effect", headers=hdr)
        assert r.status_code == 200
        assert r.json()["data"]["skill"] == one["name"]


@pytest.mark.asyncio
async def test_api_import_github_rejects_non_github_host():
    """T-C2（API 层）：非 GitHub 域名返回 400，不发起任何网络请求。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        login = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        hdr = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        r = await ac.post("/api/ops/skills/import-github", headers=hdr,
                          json={"url": "http://192.168.1.1/SKILL.md"})
        assert r.status_code == 400
        assert "github" in r.text.lower()


def test_platform_and_agent_shape_check_agree():
    """双侧同规则：平台与 Agent 的「只读形状校验」判决必须一致。

    两份实现是各自独立写的（Agent 是独立脚本、不能 import 平台代码），
    所以必须用同一组命令**逐条对拍**——否则会出现"平台放行、Agent 拒绝"的诡异不一致。
    """
    import importlib.util
    import pathlib

    from app.services.runbook import skill_command_allowed as platform_check

    agent_py = pathlib.Path(__file__).resolve().parents[2] / "agent" / "agent_main.py"
    spec = importlib.util.spec_from_file_location("d173_agent_main2", agent_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cmds = [
        "du -sh /var/log/*", "mysql -e \"SHOW FULL PROCESSLIST\"", "rm -rf /tmp/x",
        "du -sh /; echo hi", "systemctl restart nginx", "systemctl status nginx",
        "mysql -e \"DROP DATABASE x\"", "find /var/log -type f -delete",
        "cat /etc/nginx/nginx.conf", "curl -X POST http://x", "docker ps",
        "docker rm alpine", "redis-cli GET k", "redis-cli FLUSHALL",
    ]
    for c in cmds:
        p_ok, _ = platform_check(c)
        a_ok, _ = mod.skill_command_allowed(c)
        assert p_ok == a_ok, f"双侧判决不一致：{c!r} 平台={p_ok} Agent={a_ok}"


@pytest.mark.asyncio
async def test_dep_spec_change_invalidates_stale_verdict():
    """依赖声明变更时，上次的检查结论必须作废（不能继续拿旧 verdict 拦技能）。

    实测（公网）：disk-troubleshoot 依赖从 platform_host 放宽为 metrics 后，
    DB 里仍留着 ok=false —— fail-closed 继续把技能挡在自动路由外，用户无感。
    """
    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.models.ops import Skill

    name = "d173-stale-verdict"
    async with SessionLocal() as db:
        await db.execute(delete(Skill).where(Skill.name == name))
        await db.commit()

    # ① 用旧依赖声明同步一次，并塞入一个"失败"的检查结论
    original = dict(skills_svc.CORE_SKILLS.get("disk-troubleshoot") or {})
    assert original, "内置 disk-troubleshoot 缺失"
    old_spec = dict(original)
    old_spec["description"] = name
    old_spec["alerts"] = ["disk.*"]
    old_spec["check"] = {"requires": ["platform_host"]}
    skills_svc.CORE_SKILLS[name] = old_spec
    try:
        await skills_svc.sync_builtin_skills()
        async with SessionLocal() as db:
            row = (await db.execute(select(Skill).where(Skill.name == name))).scalar_one()
            row.connection_check = {"spec": {"requires": ["platform_host"]}, "ok": False,
                                    "note": "未纳管平台自身主机"}
            await db.commit()

        # ② 换新依赖声明再同步 → ok 必须被清掉（回归"未检查，默认放行"）
        new_spec = dict(old_spec)
        new_spec["check"] = {"requires": ["metrics"]}
        skills_svc.CORE_SKILLS[name] = new_spec
        await skills_svc.sync_builtin_skills()
        async with SessionLocal() as db:
            row = (await db.execute(select(Skill).where(Skill.name == name))).scalar_one()
            assert row.connection_check.get("ok") is None, row.connection_check
            assert row.connection_check.get("spec", {}).get("requires") == ["metrics"]
            # fail-closed 语义下"未检查"默认放行 → 能被自动路由匹配
            assert skills_svc._healthy(row) is True
            await db.execute(delete(Skill).where(Skill.id == row.id))
            await db.commit()
    finally:
        skills_svc.CORE_SKILLS.pop(name, None)
        async with SessionLocal() as db:
            await db.execute(delete(Skill).where(Skill.name == name))
            await db.commit()
