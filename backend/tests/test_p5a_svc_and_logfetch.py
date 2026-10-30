"""P5a 回归锁：服务级监控 + 日志取证通道（FR-408/409）。

分三块：
  A. **Agent 侧执行器**（纯函数，无外部依赖）——路径白名单、时间戳解析、时间窗过滤、
     重复折叠、首错优先、堆栈成块、字节封顶、动作注入拦截
  B. **平台侧解析与判阈**——五类服务的指标解析与阈值判定（纯函数）
  C. **通道与服务的 API 契约**——任务鉴权（他机任务必须拒）、并发与限额、僵死回收、
     spec 合并语义（**平台声明优先，Agent 只补空缺**）、脱敏
"""
import importlib.util
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENT_PY = ROOT / "agent" / "agent_main.py"


def _agent():
    """加载 Agent 单文件（安装时就是单文件分发，测试也从单文件加载）。"""
    spec = importlib.util.spec_from_file_location("agent_main_p5", AGENT_PY)
    m = importlib.util.module_from_spec(spec)
    sys.modules["agent_main_p5"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def utc_tz(monkeypatch):
    """把"本机时区"固定成 UTC，让绝对时间断言与开发机所在时区无关。

    为什么需要：Agent 按**本机本地时区**解释日志行里的裸时间戳（日志文件就是这么写的，
    journalctl 也按系统本地时间解释 --since）；平台给的窗口是 UTC。两者口径靠"本地时区"
    这个转换对齐——所以在 UTC 机器上一致、在 UTC+8 的机器上差 8 小时。测试要断言绝对
    时间串，就必须先把"本机"钉死，否则同一个用例在北京红、在 UTC 绿。
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _fix_local_tz(m):
    """让 Agent 模块重新计算本机时区（模块内缓存了 _LOCAL_TZ）。"""
    m._LOCAL_TZ = None
    return m


# ══════════════════════ A. Agent 侧执行器 ══════════════════════
def _mklog(tmp_path, lines):
    p = tmp_path / "error.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_log_path_whitelist_blocks_traversal(tmp_path, monkeypatch, utc_tz):
    """路径白名单：`..` 穿越、软链逃逸、白名单外绝对路径一律拒绝，且不透露目标是否存在。"""
    m = _fix_local_tz(_agent())
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    # 重新加载（新 spec）以让模块级白名单读到新环境变量
    m = _fix_local_tz(_agent())
    for bad in ("/etc/shadow", "/root/.ssh/id_rsa", f"{tmp_path}/../../etc/passwd"):
        r = m.fetch_log({"path": bad, "since": "2026-10-16T10:00:00Z"})
        assert r.get("denied") is True, f"{bad} 应被白名单拒绝"
        assert "不存在" not in str(r.get("error")), "拒绝信息不得透露目标是否存在"
    # 白名单内正常通过
    logs = _mklog(tmp_path, ["2026-10-16 10:05:31 [ERROR] boom"])
    assert m.fetch_log({"path": logs, "since": "2026-10-16T10:00:00Z"})["ok"] is True


def test_timestamp_formats_and_window(tmp_path, monkeypatch, utc_tz):
    """三种时间戳格式都要能解析，且**窗口外的行不得混入**（tail 取法的根本缺陷）。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _fix_local_tz(_agent())
    logs = _mklog(tmp_path, [
        "2026-10-16 09:00:00 [ERROR] 太早（窗口外）",
        "2026-10-16 10:05:30 [ERROR] 窗口内 ISO-space",
        "2026-10-16T10:05:31 [ERROR] 窗口内 ISO-T",
        "2026-10-16 11:00:00 [ERROR] 太晚（窗口外）",
    ])
    r = m.fetch_log({"path": logs, "since": "2026-10-16T10:05:00Z", "until": "2026-10-16T10:06:00Z",
                     "service_type": "app"})
    text = json.dumps(r, ensure_ascii=False)
    assert r["window"]["source"] == "timestamp", "应按行首时间戳过滤（而非关键词兜底）"
    assert "窗口内" in text and "太早" not in text and "太晚" not in text
    assert r["scanned"] == 4 and r["matched"] == 2


def test_dedupe_folds_repeats_by_body(tmp_path, monkeypatch, utc_tz):
    """重复折叠按**正文**（剥掉时间戳）比——按整行比则时间戳不同永远折不掉（实测踩过）。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _fix_local_tz(_agent())
    lines = ["2026-10-16 10:05:3%d [ERROR] Too many connections" % (i % 9) for i in range(50)]
    logs = _mklog(tmp_path, lines)
    r = m.fetch_log({"path": logs, "since": "2026-10-16T10:00:00Z", "service_type": "mysql"})
    assert r["deduped"] >= 45, f"应折叠掉绝大多数重复行，实际 deduped={r['deduped']}"
    kept = sum(len(s["lines"]) for s in r["segments"])
    assert kept <= 5, f"回传行数应被压到极少，实际 {kept}"


def test_first_error_priority_and_stack_intact(tmp_path, monkeypatch, utc_tz):
    """首错优先（同签名只留首次出现在最前）+ 堆栈成块（`at ...` 续行不能丢）。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _fix_local_tz(_agent())
    logs = _mklog(tmp_path, [
        "2026-10-16 10:06:00 [ERROR] connection refused",
        "2026-10-16 10:05:31 [ERROR] connection refused",
        "    at com.demo.Svc.run(Svc.java:12)",
        "    at com.demo.Main.main(Main.java:5)",
    ])
    r = m.fetch_log({"path": logs, "since": "2026-10-16T10:00:00Z", "context": 1, "service_type": "app"})
    seg = r["segments"][0]
    # 首见时间用**本机本地时间**渲染（用户要拿它对着日志看）；UTC 口径另存 first_ts_utc
    assert seg["first_ts"] == "2026-10-16 10:05:31", "首见时间应取最早那条"
    assert seg["first_ts_utc"].startswith("2026-10-16T10:05:31"), "UTC 口径必须保留"
    body = "\n".join(seg["lines"])
    assert "at com.demo.Svc.run" in body and "at com.demo.Main.main" in body, "堆栈续行必须保留"


def test_byte_cap_marks_truncated(tmp_path, monkeypatch, utc_tz):
    """字节封顶：超限时按相关度截断并置 truncated（而不是无限回传）。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _fix_local_tz(_agent())
    monkeypatch.setattr(m, "TASK_MAX_BYTES", 800)
    lines = ["2026-10-16 10:05:%02d [ERROR] unique failure %d" % (i % 60, i) for i in range(200)]
    logs = _mklog(tmp_path, lines)
    r = m.fetch_log({"path": logs, "since": "2026-10-16T10:00:00Z", "service_type": "app"})
    assert r["bytes"] <= 800 + 512
    assert r["truncated"] is True, "超出上限必须标记 truncated（让 AI 知道有截断）"


def test_covering_report_fields(tmp_path, monkeypatch, utc_tz):
    """覆盖报告六项必须齐全——没有它，AI 无法区分"窗口选错"与"内容不够"。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _fix_local_tz(_agent())
    logs = _mklog(tmp_path, ["2026-10-16 10:05:31 [ERROR] x"])
    r = m.fetch_log({"path": logs, "since": "2026-10-16T10:00:00Z"})
    for k in ("window", "scanned", "matched", "dropped_by_level", "deduped", "truncated"):
        assert k in r, f"覆盖报告缺字段 {k}"
    assert set(r["window"]) >= {"since", "until", "source"}


def test_svc_action_whitelist_blocks_injection():
    """动作只认枚举 ID；服务名做字符集校验（`;rm -rf`、`$(...)`、`../` 一律拒）。"""
    m = _fix_local_tz(_agent())
    for bad in ({"action": "reboot", "service": "nginx"},
                {"action": "restart_service", "service": "nginx; rm -rf /"},
                {"action": "restart_service", "service": "nginx$(whoami)"},
                {"action": "restart_service", "service": "../../etc/passwd"},
                {"action": "restart_service", "service": ""}):
        r = m.run_action(bad)
        assert r.get("denied") is True, f"{bad} 必须被拒"
        assert "rm -rf" not in str(r.get("cmd", "")), "拒绝路径不得产出任何命令"


def test_payload_is_json_serializable(tmp_path, monkeypatch, utc_tz):
    """任务载荷必须可 JSON 化（实测：datetime 直接回传导致 TypeError，任务成功却丢结果）。"""
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _fix_local_tz(_agent())
    logs = _mklog(tmp_path, ["2026-10-16 10:05:31 [ERROR] boom"])
    r = m.fetch_log({"path": logs, "since": "2026-10-16T10:00:00Z", "service_type": "app"})
    json.dumps(r)   # 抛 TypeError 即失败


# ══════════════════════ B. 平台侧解析与判阈 ══════════════════════
def test_parse_and_judge_five_types():
    from app.services import svc_monitor as sm

    # MySQL：连接数占比超阈 → degraded
    raw = "Threads_connected\t140\nmax_connections\t151\nInnodb_row_lock_waits\t3\nSlow_queries\t12\n"
    m = sm.parse_metrics("mysql", raw)
    assert m["Threads_connected"] == 140 and m["max_connections"] == 151
    assert m["conn_used_ratio"] > 0.9
    st, note = sm.judge("mysql", m)
    assert st == "degraded" and "连接数" in note and "行锁等待" in note

    # MySQL 正常值 → up
    st2, _ = sm.judge("mysql", sm.parse_metrics("mysql", "Threads_connected\t5\nmax_connections\t151\n"))
    assert st2 == "up"

    # Redis：内存占比 + 命中率
    rr = sm.parse_metrics("redis", "used_memory:900000000\nmaxmemory:1000000000\nkeyspace_hits:100\nkeyspace_misses:900\n")
    assert rr["mem_used_ratio"] == 0.9 and rr["hit_rate"] == 0.1
    st3, note3 = sm.judge("redis", rr)
    assert st3 == "degraded" and "内存" in note3 and "命中率" in note3

    # Nginx stub_status
    ng = sm.parse_metrics("nginx", "Active connections: 100 \n 5 5 10 \nReading: 0 Writing: 1 Waiting: 9")
    assert ng["active_connections"] == 100 and ng["requests"] == 10
    st4, _ = sm.judge("nginx", ng)
    assert st4 == "degraded", "连接/请求比过高应判 degraded"

    # MQ：队列积压
    mq = sm.parse_metrics("mq", json.dumps([{"messages": 20000}, {"messages": 5}]))
    assert mq["max_queue_depth"] == 20000
    st5, note5 = sm.judge("mq", mq)
    assert st5 == "degraded" and "积压" in note5


def test_templates_cover_five_types_with_defaults():
    from app.services import svc_monitor as sm

    for t in ("mysql", "redis", "nginx", "mq", "app"):
        assert t in sm.TEMPLATES, f"模板缺 {t}"
        tpl = sm.TEMPLATES[t]
        for k in ("label", "default_port", "hint"):
            assert k in tpl, f"{t} 模板缺 {k}"


def test_build_spec_fills_defaults():
    from app.services import svc_monitor as sm

    s = sm.build_spec("mysql", "MySQL(3306)")
    assert s["port"] == 3306 and s["unit"] == "mysql" and s["log_path"].endswith("error.log")
    s2 = sm.build_spec("app", "订单服务", port=8081, process="order")
    assert s2["port"] == 8081 and s2["process"] == "order"


# ══════════════════════ C. 通道与服务 API 契约 ══════════════════════
def test_mask_result_hides_credentials():
    from app.routers.tasks import _mask_result

    raw = {"segments": [{"lines": [
        "connect password=SuperSecret123",
        "Authorization: Bearer abcdefghijklmnop",
        "dsn mysql://root:MyPass@127.0.0.1:3306/db",
    ]}]}
    out = json.dumps(_mask_result(raw), ensure_ascii=False)
    for secret in ("SuperSecret123", "abcdefghijklmnop", "MyPass"):
        assert secret not in out, f"凭据 {secret} 未被脱敏"
    assert "***" in out


@pytest.mark.asyncio
async def test_task_channel_contracts():
    """通道契约：他机任务必须拒、并发 1、小时限额、僵死回收、结果脱敏入库。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.main import create_app
    from app.models.host import Host
    from app.models.svc import AgentTask, Service
    from app.services import ingest

    app = create_app()
    async with SessionLocal() as db:
        # 用专有前缀避免误伤真实主机（LIKE 在大小写不敏感排序下会匹配到非测试主机，实测踩到）：
        for fp in ("p5-fp-a", "p5-fp-b"):
            ss = (await db.execute(select(Service).where(Service.host_id.in_(
                select(Host.id).where(Host.name.like("p5test-%")))))).scalars().all()
            for x in ss:
                await db.delete(x)
            await db.execute(delete(Host).where(Host.name.like("p5test-%")))
        await db.commit()
        tok_a = ingest.mint_install_token()
        tok_b = ingest.mint_install_token()
        a = await ingest.register_agent(db, {"fingerprint": "p5-fp-a", "hostname": "p5test-host-a",
                                            "install_token": tok_a})
        b = await ingest.register_agent(db, {"fingerprint": "p5-fp-b", "hostname": "p5test-host-b",
                                            "install_token": tok_b})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        lg = await ac.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": "Bearer " + lg.json()["data"]["access_token"]}
        hdr_a = {"X-Ident": a["ident"], "X-Token": a["token"]}
        hdr_b = {"X-Ident": b["ident"], "X-Token": b["token"]}

        # 无任务 → 204
        assert (await ac.get("/api/ops/agents/tasks", headers=hdr_a)).status_code == 204

        # 下发一次取证
        r = await ac.post("/api/ops/log-fetch", headers=H,
                          json={"ident": a["ident"], "path": "/var/log/x.log",
                                "since": "2026-10-16T10:05:00", "until": "2026-10-16T10:07:00"})
        assert r.status_code == 200, r.text[:200]
        tid = r.json()["data"]["task_id"]

        # 并发 1：再下发应被拒（429）
        r2 = await ac.post("/api/ops/log-fetch", headers=H,
                           json={"ident": a["ident"], "path": "/var/log/x.log"})
        assert r2.status_code == 429, "同主机并发应被限额拦下"

        # 他机领取：B 拿不到 A 的任务
        got = await ac.get("/api/ops/agents/tasks", headers=hdr_b)
        assert got.status_code == 204, "B 不该领到 A 的任务（按主机隔离）"

        # A 领取
        got_a = await ac.get("/api/ops/agents/tasks", headers=hdr_a)
        assert got_a.status_code == 200 and got_a.json()["data"]["task"]["id"] == tid

        # 他机回填：必须 404
        bad = await ac.post(f"/api/ops/agents/tasks/{tid}/result", headers=hdr_b,
                            json={"result": {"ok": True}})
        assert bad.status_code == 404, "不得回填他人的任务"

        # 本机回填 + 脱敏
        ok = await ac.post(f"/api/ops/agents/tasks/{tid}/result", headers=hdr_a,
                           json={"result": {"ok": True, "segments": [
                               {"lines": ["password=TopSecret1"]}]}})
        assert ok.status_code == 200
        async with SessionLocal() as db:
            t = await db.get(AgentTask, tid)
            assert t.status == "done"
            assert "TopSecret1" not in json.dumps(t.result_json), "入库结果必须已脱敏"

        # 二次下发（上一任务已完成）→ 允许（迭代取证需要连续取）
        r3 = await ac.post("/api/ops/log-fetch", headers=H,
                           json={"ident": a["ident"], "path": "/var/log/x.log",
                                 "since": "2026-10-16T10:00:00"})
        assert r3.status_code == 200, "任务完成后应允许继续取（迭代取证）"

        # 僵尸回收：把任务置为 picked 且 picked_at 很旧 → 再下发应被允许
        async with SessionLocal() as db:
            t3 = await db.get(AgentTask, r3.json()["data"]["task_id"])
            t3.status, t3.picked_at = "picked", datetime.utcnow() - timedelta(minutes=30)
            await db.commit()
        r4 = await ac.post("/api/ops/log-fetch", headers=H,
                           json={"ident": a["ident"], "path": "/var/log/x.log",
                                 "since": "2026-10-16T10:00:00"})
        assert r4.status_code == 200, "僵尸 picked 任务不得永久堵死通道（应被回收）"

    # 清理：**只删本次测试主机的任务**（原实现 delete(AgentTask) 清空全表，
    # 会把其它来源的取证证据一起删掉——测试污染生产数据，实测踩到）
    async with SessionLocal() as db:
        hids = select(Host.id).where(Host.name.like("p5test-%"))
        await db.execute(delete(AgentTask).where(AgentTask.host_id.in_(hids)))
        await db.execute(delete(Host).where(Host.name.like("p5test-%")))
        await db.commit()


@pytest.mark.asyncio
async def test_spec_merge_platform_declaration_wins():
    """spec 合并语义：**平台侧声明为准，Agent 只补空缺**。

    实测踩到的坑：若无条件用 Agent 上报的 spec 覆盖，用户在平台上改的"日志路径"
    会在约 1 分钟后被心跳静默还原 → 取证直接失效且毫无提示。
    """
    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.models.svc import Service
    from app.services import svc_monitor

    async with SessionLocal() as db:
        await db.execute(delete(Host).where(Host.name == "p5-svc-host"))
        await db.commit()
        h = Host(ident="p5-svc-ident", name="p5-svc-host", status="up")
        db.add(h)
        await db.commit()
        await db.refresh(h)

        # 首次上报 → 建行（用 Agent 的 spec）
        await svc_monitor.ingest_report(db, h.id, [{
            "type": "mysql", "name": "MySQL(3306)", "ok": True,
            "raw": "Threads_connected\t5\nmax_connections\t151\n",
            "spec": svc_monitor.build_spec("mysql", "MySQL(3306)")}])
        row = (await db.execute(select(Service).where(Service.host_id == h.id))).scalar_one()
        assert row.spec_json["log_path"].endswith("error.log")
        assert row.status == "up"

        # 平台侧改日志路径（用户改声明）
        row.spec_json = {**row.spec_json, "log_path": "/data/mysql/err.log"}
        await db.commit()

        # Agent 再次上报（心跳带自己的 spec）→ 用户改的路径必须保留
        await svc_monitor.ingest_report(db, h.id, [{
            "type": "mysql", "name": "MySQL(3306)", "ok": True,
            "raw": "Threads_connected\t5\nmax_connections\t151\n",
            "spec": svc_monitor.build_spec("mysql", "MySQL(3306)")}])
        row2 = (await db.execute(select(Service).where(Service.host_id == h.id))).scalar_one()
        assert row2.spec_json["log_path"] == "/data/mysql/err.log", "平台声明被 Agent 上报覆盖了"

        # 取不到数据时 → unknown + 人话（不猜 up）
        await svc_monitor.ingest_report(db, h.id, [{
            "type": "mysql", "name": "MySQL(3306)", "ok": False, "raw": "",
            "error": "mysql: [ERROR] Failed to open required defaults file"}])
        row3 = (await db.execute(select(Service).where(Service.host_id == h.id))).scalar_one()
        assert row3.status == "unknown" and "defaults file" in row3.note

        # app 类：结构化检查结果判定（不是解析文本）
        await svc_monitor.ingest_report(db, h.id, [{
            "type": "app", "name": "订单服务", "ok": True, "raw": "",
            "metrics": {"process_ok": 1, "port_ok": 0}, "error": "端口 8080 未监听",
            "spec": {"type": "app", "name": "订单服务", "port": 8080}}])
        row4 = (await db.execute(select(Service).where(
            Service.host_id == h.id, Service.name == "订单服务"))).scalar_one()
        assert row4.status == "degraded" and "端口" in row4.note

        await db.execute(delete(Service).where(Service.host_id == h.id))
        await db.execute(delete(Host).where(Host.ident == "p5-svc-ident"))
        await db.commit()


# ══════════════════════ D. P5b 错误上下文与时间线 ══════════════════════
def test_signature_specificity_ordering():
    """签名必须"具体优先"：通用词 error 不能抢走 too many connections 这类具体签名。

    实测踩到：原实现按列表顺序匹配，所有错误行都被标成同一个簇「error」——
    簇标签失去区分度，"有几类错"这个最有价值的信息就没了。
    """
    m = _fix_local_tz(_agent())
    assert m.task_match_signature("2026-10-16 [ERROR] Too many connections", "mysql") == "too many connections"
    assert m.task_match_signature("2026-10-16 [ERROR] Lock wait timeout exceeded", "mysql") == "lock wait timeout"
    assert m.task_match_signature("2026-10-16 [ERROR] upstream timed out", "nginx") == "upstream timed out"
    # 只有泛化错误时才回落到通用词
    assert m.task_match_signature("2026-10-16 [ERROR] 某处失败", "app") == "error"
    # 与错误无关的行不匹配
    assert m.task_match_signature("2026-10-16 [INFO] health check ok", "app") == ""


def test_cluster_and_stack_fingerprint():
    """错误簇聚类：跨段同签名归并 + 栈指纹提取（同栈即同类错误，可跨实例比对）。"""
    from app.services import error_context as ctx

    segs = [
        {"signature": "too many connections", "count": 40, "first_ts": "2026-10-16T10:05:31+00:00",
         "last_ts": "2026-10-16T10:07:00+00:00", "hosts": ["h1"],
         "lines": ["[ERROR] Too many connections", "    at com.a.B.c(B.java:12)",
                   "    at com.a.D.e(D.java:9)"]},
        {"signature": "too many connections", "count": 4, "first_ts": "2026-10-16T10:06:10+00:00",
         "last_ts": "2026-10-16T10:06:40+00:00", "hosts": ["h2"],
         "lines": ["[ERROR] Too many connections", "    at com.a.B.c(B.java:99)"]},
        {"signature": "lock wait timeout", "count": 3, "first_ts": "2026-10-16T10:04:00+00:00",
         "lines": ["[ERROR] Lock wait timeout exceeded"]},
    ]
    cl = ctx.cluster_segments(segs)
    # 两条"同签名同栈"的段必须归并成一簇（跨主机），栈指纹取前 3 帧
    fake = [c for c in cl if c["signature"] == "too many connections"]
    assert len(fake) == 1, f"同签名同栈应归并为一簇，实际 {len(fake)}"
    assert fake[0]["count"] == 44 and set(fake[0]["hosts"]) == {"h1", "h2"}
    assert "com.a.B.c" in fake[0]["stack"] and "com.a.D.e" in fake[0]["stack"]
    # 首错优先：lock wait 更早 → 排第一
    assert cl[0]["signature"] == "lock wait timeout", "应按首次出现时间排序（首错优先）"


def test_timeline_normalizes_and_exposes_unknown():
    """多源时间线：统一 UTC 排序、保留 raw_time 可核对、无法解析的时间**不猜**（单列计数）。"""
    from app.services import error_context as ctx

    tl = ctx.build_timeline(
        incident={"title": "x", "created_at": "2026-10-16 10:00:00", "resolved_at": ""},
        alerts=[{"first_trigger_time": "2026-10-16 10:05:31", "severity": 1, "rule_name": "r",
                 "metric": "m", "trigger_value": 1, "host_ident": "h"}],
        logs=[{"signature": "too many connections", "count": 3, "first_ts": "2026-10-16T10:06:00+00:00"}],
        tasks=[{"created_at": "2026-10-16 10:00:30", "kind": "log_fetch", "status": "done", "summary": "1 段"},
               {"created_at": "不是时间", "kind": "log_fetch", "status": "done", "summary": "-"}],
    )
    assert tl["total"] == 5 and tl["unknown_time"] == 1, "无法解析的时间必须如实单列，不得猜"
    sources = {x["source"] for x in tl["items"]}
    assert {"事件", "告警", "日志", "取证"} <= sources, f"多源对齐应覆盖各来源，实际 {sources}"
    # 已解析的按时间升序
    known = [x for x in tl["items"] if x["ts"]]
    assert known == sorted(known, key=lambda x: x["ts"])
    assert known[0]["ts"].startswith("2026-10-16T10:00:00"), "事件创建最早"
    # raw_time 保留原始表示（跨系统时区差异靠它核对）
    assert any(x["raw_time"] for x in tl["items"])


def test_error_context_section_is_evidence_oriented():
    """报告段：有证据时引用原文与栈；无证据时**明确说明不作为结论依据**（不许编）。"""
    from app.services import error_context as ctx

    empty = ctx.error_context_section([], None, None)
    assert "未获取到远程日志证据" in empty and "not_checked" in empty, "无证据时必须诚实说明"

    md = ctx.error_context_section(
        [{"signature": "too many connections", "count": 44, "first_ts": "2026-10-16T10:05:31+00:00",
          "hosts": ["h1"], "stack": "com.a.B.c", "sample": ["[ERROR] Too many connections"]}],
        {"items": [{"ts": "2026-10-16T10:05:31+00:00", "source": "日志", "title": "t", "detail": ""}],
         "unknown_time": 0},
        [{"metric": "cpu.percent", "summary": "3 点 · 最大 91.2"}])
    assert "too many connections" in md and "com.a.B.c" in md
    assert "[ERROR] Too many connections" in md, "必须引用日志原文（可核对）"
    assert "cpu.percent" in md and "时间线" in md

def test_line_timestamps_interpreted_in_host_local_time(tmp_path, monkeypatch, utc_tz):
    """非 UTC 主机回归锁（实测缺陷）：平台给的窗口是 **UTC**，而日志文件里的裸时间戳是
    **主机本地时间**。修复前按 UTC 解释 → 在 UTC+8 的机器上窗口整体错 8 小时，
    取证"一条都取不到"，还会提示"请重设窗口"（指错方向）。

    锁两件事：① 本地 11:44 的行能落进 UTC 03:44~03:46 的窗口；
    ② 回传的窗口同时给本地口径与 UTC 口径（用户能拿本地时间对着日志看）。
    """
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
        m = _fix_local_tz(_agent())
        logs = _mklog(tmp_path, [
            "2026-10-16 11:44:17 [ERROR] java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available",
            "    at com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:228)",
        ])
        r = m.fetch_log({"path": logs, "since": "2026-10-16T03:44:00Z",
                         "until": "2026-10-16T03:46:00Z", "context": 1})
        assert r["ok"] is True, r
        assert r["segments"], "本地 11:44 的行必须落进 UTC 03:44 的窗口（时区换算）"
        assert r["matched"] >= 1
        assert r["window"]["since"] == "2026-10-16 11:44:00", r["window"]
        assert r["window"]["since_utc"].startswith("2026-10-16T03:44"), r["window"]
        assert r["window"]["tz_offset"] == "+0800", r["window"]
        assert r["segments"][0]["first_ts"].startswith("2026-10-16 11:44:17"), r["segments"][0]
    finally:
        monkeypatch.undo()
        time.tzset()


# ══════════════════════ D. 平台声明下发（验收时补的缺口） ══════════════════════
def test_svc_cmd_with_port_parameterises_non_default_ports():
    """平台声明的**非默认端口**要能探（如 MySQL 装在 3307）：命令按声明端口参数化。"""
    m = _agent()
    base = m.SVC_PROBES["mysql"]
    same = m._svc_cmd_with_port("mysql", base, 3306, 3306)
    assert same == base, "端口等于默认值时不应改写命令"
    p3307 = m._svc_cmd_with_port("mysql", base, 3307, 3306)
    assert "-P 3307" in " ".join(p3307)
    r = m._svc_cmd_with_port("redis", m.SVC_PROBES["redis"], 6380, 6379)
    assert "6380" in " ".join(r)
    n = m._svc_cmd_with_port("nginx", m.SVC_PROBES["nginx"], 8080, 80)
    assert "127.0.0.1:8080" in " ".join(n)
    # MQ 的 AMQP 端口(5672)与管理 API 端口(15672)不是一个：给 5672 时仍用默认管理口
    mq = m._svc_cmd_with_port("mq", m.SVC_PROBES["mq"], 5672, 15672)
    assert "15672" in " ".join(mq)


def test_probe_services_uses_declared_app_spec(monkeypatch):
    """应用类服务按**平台声明**探测（进程/端口/健康端点），并回传结构化 metrics。

    这是验收时补的真缺口：声明原先只存在于安装命令的 --services，用户在界面上登记一个
    应用类服务后 Agent 根本不去探测，服务永远停在「未知」。
    """
    m = _agent()
    called = {}

    def fake_alive(name):
        called["proc"] = name
        return True

    monkeypatch.setattr(m, "_process_alive", fake_alive)
    monkeypatch.setattr(m, "_port_listening", lambda p: p == 18080)
    monkeypatch.setattr(m, "probe_http", lambda url, timeout=5.0: {"status": "up"})

    out = m.probe_services([{"type": "app", "name": "订单服务(order-service)", "port": 18080,
                             "process": "order_service", "health_url": "http://127.0.0.1:18080/health"}])
    app = [x for x in out if x["type"] == "app" and x["name"] == "订单服务(order-service)"]
    assert app, out
    assert app[0]["metrics"] == {"process_ok": 1, "port_ok": 1, "health_ok": 1}
    assert app[0]["ok"] is True
    # 未声明任何探测方式 → 明确报错而不是静默"未知"
    bare = [x for x in m.probe_services([{"type": "app", "name": "空声明"}]) if x["name"] == "空声明"]
    assert bare and bare[0]["ok"] is False and "未声明探测方式" in bare[0]["error"]


@pytest.mark.asyncio
async def test_agents_config_endpoint_serves_platform_declaration():
    """/agents/config：机器凭证鉴权 + 只给该主机的**启用中**服务（含 spec）。

    这是"界面加了个服务，然后呢"的落点：声明必须能被 Agent 拉到，否则探测无从发生。
    """
    import hashlib

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, select

    from app.main import create_app
    from app.models.host import Host
    from app.models.svc import Service
    from app.db.session import SessionLocal
    from app.services import ingest

    fp = "p5a-declared-svc-fp"
    ident = hashlib.sha256(fp.encode()).hexdigest()[:32]
    async with SessionLocal() as db:
        await db.execute(delete(Service).where(Service.name.like("P5A-声明%")))
        h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
        if h is None:
            tok = ingest.mint_install_token()
            await ingest.register_agent(db, {"fingerprint": fp, "hostname": "p5a-declared",
                                             "agent_version": "0.3.0", "install_token": tok})
            h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        db.add(Service(host_id=h.id, name="P5A-声明-应用", type="app", status="unknown",
                       enabled=True, spec_json={"port": 18080, "process": "order_service"}))
        db.add(Service(host_id=h.id, name="P5A-声明-已停用", type="app", status="unknown",
                       enabled=False, spec_json={"port": 9}))
        await db.commit()
        token = h.agent_token

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/ops/agents/config")).status_code == 401
        r = await c.get("/api/ops/agents/config", headers={"X-Ident": ident, "X-Token": token})
        assert r.status_code == 200, r.text
        svcs = r.json()["data"]["services"]
        names = [s["name"] for s in svcs]
        assert "P5A-声明-应用" in names, svcs
        assert "P5A-声明-已停用" not in names, "停用的服务不应下发给 Agent"
        one = [s for s in svcs if s["name"] == "P5A-声明-应用"][0]
        assert one["port"] == 18080 and one["process"] == "order_service", one

    async with SessionLocal() as db:
        await db.execute(delete(Service).where(Service.name.like("P5A-声明%")))
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


@pytest.mark.asyncio
async def test_ingest_report_zero_checks_means_down_not_unknown():
    """三项结构化检查全不通 = 服务**不可用**（down），不是「未知」。

    验收实测踩到的真问题：Agent 的 ok=False 有两种含义——①探不动（没 metrics）
    ②探到了但三项全不通（服务真挂了）。第一版先判 ok=False → 把②误报成
    「未知/探测未成功」，严重度少报一级。锁住这个语义。
    """
    import hashlib

    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.models.host import Host
    from app.models.svc import Service
    from app.services import ingest, svc_monitor

    fp = "p5a-zero-checks-fp"
    ident = hashlib.sha256(fp.encode()).hexdigest()[:32]
    async with SessionLocal() as db:
        await db.execute(delete(Service).where(Service.name == "P5A-全零服务"))
        h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
        if h is None:
            tok = ingest.mint_install_token()
            await ingest.register_agent(db, {"fingerprint": fp, "hostname": "p5a-zero",
                                             "agent_version": "0.3.0", "install_token": tok})
            h = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one()
        hid = h.id

    async with SessionLocal() as db:
        await svc_monitor.ingest_report(db, hid, [{
            "type": "app", "name": "P5A-全零服务", "ok": False, "raw": "",
            "spec": {"type": "app", "name": "P5A-全零服务", "port": 18080},
            "metrics": {"process_ok": 0, "port_ok": 0, "health_ok": 0}}])
        row = (await db.execute(select(Service).where(
            Service.host_id == hid, Service.name == "P5A-全零服务"))).scalar_one()
        assert row.status == "down", f"三项全不通应判不可用，实际 {row.status}"
        assert "不可用" in row.note
        assert row.metrics_json == {"process_ok": 0, "port_ok": 0, "health_ok": 0}

        # 对照：没有 metrics（探不动）才叫「未知」，并保留原因
        await svc_monitor.ingest_report(db, hid, [{
            "type": "app", "name": "P5A-全零服务", "ok": False, "raw": "",
            "spec": {"type": "app", "name": "P5A-全零服务"},
            "error": "未声明探测方式（进程名 / 端口 / 健康端点至少给一个）"}])
        row = (await db.execute(select(Service).where(
            Service.host_id == hid, Service.name == "P5A-全零服务"))).scalar_one()
        assert row.status == "unknown" and "未声明探测方式" in row.note, row.note

        await db.execute(delete(Service).where(Service.name == "P5A-全零服务"))
        await db.execute(delete(Host).where(Host.ident == ident))
        await db.commit()


def test_declared_log_file_wins_over_unit(tmp_path, monkeypatch, utc_tz):
    """同时声明 unit 与 log_path 时，**日志文件优先**（真机验收实测的反例）。

    反例：平台声明的日志文件里是真正的业务错误，但取证先走 journalctl → 只拿回几行
    systemd 启停（命中 5、丢弃 5、回传 0 字节）→ 调用方以为"这个服务没有错误日志"，
    比取不到更糟。只有文件不可用或窗口内没内容时，才回落到 journald。
    """
    monkeypatch.setenv("AIOPS_LOG_DIRS", str(tmp_path))
    m = _fix_local_tz(_agent())
    # 假造一个 journalctl：返回 systemd 启停行（不该被优先使用）
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = "2026-10-16T12:50:00+0800 host systemd[1]: Started Demo.\n"
            stderr = ""
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    logs = _mklog(tmp_path, [
        "2026-10-16 12:50:10 [ERROR] java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available",
    ])
    # 注意窗口口径：夹具把本机时区钉成 UTC，故日志行的 12:50（本地）就是 12:50 UTC
    r = m.fetch_log({"path": logs, "unit": "aiops-demo-order",
                     "since": "2026-10-16T12:50:00Z", "until": "2026-10-16T12:52:00Z",
                     "context": 1, "service_type": "app"})
    assert r["ok"] is True and r["segments"], r
    assert "HikariPool" in "\n".join(r["segments"][0]["lines"])
    assert r["window"]["source"] in ("timestamp", "keyword"), r["window"]
    assert not calls, "文件可用时不应该调用 journalctl"
    assert "日志文件优先" in (r.get("unit_note") or ""), r.get("unit_note")


@pytest.mark.asyncio
async def test_log_fetch_rejects_bad_path_shape_at_platform():
    """平台侧拦**路径形状**：必须绝对路径、不得含 `..` 穿越。

    分工（验收时定的口径）：形状是通用契约，平台拦（省一次白跑的任务与往返）；
    "哪些目录可读"的白名单归**目标机 Agent**（只有它知道本机 AIOPS_LOG_DIRS，
    平台瞎猜会把客户自定目录误拒）。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tok = (await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()
        tok = tok["data"]["access_token"]
        h = {"Authorization": "Bearer " + tok}
        for bad in ("../../etc/passwd", "/var/log/../../etc/shadow", "etc/passwd", "~/x.log"):
            r = await c.post("/api/ops/log-fetch", headers=h,
                             json={"ident": "whatever", "path": bad, "since": "", "until": ""})
            assert r.status_code == 400, f"{bad} 应被平台以 400 拦下，实际 {r.status_code}"
        # 形状合法但主机不存在 → 404（说明形状校验已通过，继续走正常流程）
        r = await c.post("/api/ops/log-fetch", headers=h,
                         json={"ident": "no-such-host", "path": "/var/log/syslog", "since": "", "until": ""})
        assert r.status_code == 404, r.status_code
