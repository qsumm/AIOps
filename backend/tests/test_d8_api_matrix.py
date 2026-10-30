"""D8 交付级全量 API 矩阵测试（测试阶段新增）。

为什么单独一个文件：D1–D7 的测试是**按功能点**写的（引擎、通知、诊断、处置……），
但"接口 × 角色"这个维度一直只被零星覆盖，导致「演示模式点 AI 助手 → 403」这类
**前后端一致性缺陷**没有任何测试能挡住。本文件把三个维度同时锁死：

  1. **路由覆盖**：表里没登记的新路由直接失败（防止加了接口没加测试）；
  2. **角色矩阵**：匿名/demo/member/admin 四种身份对同一路由的期望状态码（唯一权威=03 权限矩阵）；
  3. **错误信封**：401/403/404/422 一律 `{code,msg,data}` 且 msg 非空 —— 前端只认这个形状，
     否则 403 在界面上就退化成"请求失败：HTTP 403"（实测缺陷，见 03 §权限矩阵）。

另含"任何路由都不允许 5xx"的兜底断言：任何未预期异常都会在这里暴露。
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.main import app
from app.models.alert import AlertRule
from app.models.shell import User

MEMBER = "d8-member"
MEMBER_PW = "d8-member-pw"
FAKE_ID = 999999
FAKE_HASH = "d8nosuchhash"
FAKE_INC = 999999

# ---------------------------------------------------------------- 身份

async def _token(ac, username: str, password: str) -> str:
    r = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]


async def _ensure_member() -> None:
    async with SessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == MEMBER))).scalar_one_or_none()
        if u is None:
            db.add(User(username=MEMBER, password_hash=hash_password(MEMBER_PW), role="member"))
            await db.commit()


async def _drop_member() -> None:
    async with SessionLocal() as db:
        await db.execute(delete(User).where(User.username == MEMBER))
        await db.commit()


@pytest.fixture
async def client():
    await _cleanup_d8()      # 上一次运行可能留下 d8-* 行 → 先清，避免"重名 409"干扰矩阵
    await _ensure_member()
    # raise_app_exceptions=False：让 500 以"响应"形式出现而不是把测试直接抛崩，
    # 这样"任何路由不得 5xx"的断言才能把 5xx 连同响应体一起报出来。
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=120) as ac:
        hdrs = {
            "anon": {},
            "demo": {"Authorization": f"Bearer {await _token(ac, 'demo', 'demo')}"},
            "member": {"Authorization": f"Bearer {await _token(ac, MEMBER, MEMBER_PW)}"},
            "admin": {"Authorization": f"Bearer {await _token(ac, 'admin', 'admin123')}"},
        }
        yield ac, hdrs
    await _drop_member()


# ---------------------------------------------------------------- 路由清单
# 期望值：按 03 §权限矩阵填写；用集合表示"可接受的状态码"，避免把 200/201/204 细节写进契约。
OK, DENY = {200, 201, 204, 404}, {403}

# (method, path, json_body, {role: 期望状态码集合}, 说明, 调用模式)
#   调用模式：
#     "full"    —— 真正发起调用并断言状态码
#     "auth"    —— 只断言"拒绝类"角色（anon/demo/member），命中成功的角色跳过：
#                  用于会真跑 LLM / 产生副作用的重接口（巡检立即执行等）
#     "sse"     —— 长连接流式接口（告警流）：**只断言拒绝类角色**，成功路径跳过。
#                  原因：httpx 的 ASGITransport 关不掉无限 SSE 生成器（实测会把测试挂死），
#                  成功路径改由 test_d8_gui/端到端与 test_d7「IT-告警实时（SSE）」覆盖。
#                  ChatOps 流（/api/ops/chat/stream）会自行收敛，故用 "full"。
ROUTES: list[tuple[str, str, dict | None, dict[str, set[int]], str, str]] = [
    # ---- D154 三项能力补齐（FR-422~425）----
    ("GET", "/api/ops/notify-rules", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "通知规则列表（D154）", "full"),
    # severities 留空 = 该条配置停用（四维筛选的规范语义）——矩阵只验 CRUD/权限，
    # **不能**建一条会接管真实 S1 事件的规则（否则 d140/d9 的老路径用例全被劫持）。
    ("POST", "/api/ops/notify-rules", {"name": "d8-nrule", "configs": [{"channel_id": 1, "severities": []}]},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 400, 409}}, "建通知规则（D154，write）", "full"),
    ("PUT", f"/api/ops/notify-rules/{FAKE_ID}", {"name": "d8-nrule-x", "configs": []},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 400, 404}}, "改通知规则（D154，write）", "full"),
    ("POST", f"/api/ops/notify-rules/{FAKE_ID}/test", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "模拟事件试投（D154，write）", "full"),
    ("DELETE", f"/api/ops/notify-rules/{FAKE_ID}", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "删通知规则（D154，write）", "full"),
    ("GET", "/api/ops/alert-subscribes", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "订阅列表（D154）", "full"),
    ("POST", "/api/ops/alert-subscribes", {"rule_id": FAKE_ID},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "建订阅（D154，write）", "full"),
    ("PUT", f"/api/ops/alert-subscribes/{FAKE_ID}", {"rule_id": FAKE_ID},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "改订阅（D154，write）", "full"),
    ("DELETE", f"/api/ops/alert-subscribes/{FAKE_ID}", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "删订阅（D154，write）", "full"),
    ("GET", "/api/ops/busi-groups", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "业务组列表（D154）", "full"),
    ("POST", "/api/ops/busi-groups", {"name": "d8-bgroup"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 400, 409}}, "建业务组（D154，write）", "full"),
    ("PUT", f"/api/ops/busi-groups/{FAKE_ID}", {"name": "d8-bgroup-x"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "改业务组（D154，write）", "full"),
    ("DELETE", f"/api/ops/busi-groups/{FAKE_ID}", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "删业务组（D154，write）", "full"),
    ("POST", f"/api/ops/busi-groups/{FAKE_ID}/members", {"team": "d8-team"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "加业务组成员（D154，write）", "full"),
    ("DELETE", f"/api/ops/busi-groups/{FAKE_ID}/members/{FAKE_ID}", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "删业务组成员（D154，write）", "full"),
    ("GET", "/api/ops/metric/query", None,
     {"anon": {401}, "demo": {200, 400}, "member": {200, 400}, "admin": {200, 400}}, "维度化即时查询（D154；缺 metric 给 400）", "full"),
    ("GET", "/api/ops/metric/series", None,
     {"anon": {401}, "demo": {200, 400}, "member": {200, 400}, "admin": {200, 400}}, "带维度序列清单（D154；缺 host 给 400）", "full"),
    # ---- D117 主机手动下线/移出 + D119 健康诊断/指标发现/告警标记 ----
    ("POST", "/api/ops/hosts/{ident}/offline", {"offline": True},
     {"anon": {401}, "demo": DENY, "member": {403, 404}, "admin": {200, 404}}, "主机手动下线/恢复（D117，write）", "full"),
    ("POST", "/api/ops/hosts/{ident}/remove", {"removed": True},
     {"anon": {401}, "demo": DENY, "member": {403, 404}, "admin": {200, 404}}, "主机移出/撤移出（D117，write）", "full"),
    ("POST", "/api/ops/hosts/{ident}/upgrade", {},
     {"anon": {401}, "demo": DENY, "member": {403, 404}, "admin": {200, 404, 409}}, "平台侧一键升级 Agent（D121，write）", "full"),
    ("POST", "/api/ops/hosts/{ident}/diagnose/health", {"minutes": 60},
     {"anon": {401}, "demo": DENY, "member": {200, 404}, "admin": {200, 404}}, "一键健康诊断（D119，diag）", "full"),
    ("GET", "/api/ops/hosts/{ident}/diagnose/health/reports", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "健康诊断历史报告列表（D119）", "full"),
    ("GET", f"/api/ops/hosts/{{ident}}/diagnose/health/reports/{FAKE_ID}", None,
     {"anon": {401}, "demo": {200, 404}, "member": {200, 404}, "admin": {200, 404}}, "健康诊断报告详情（D119）", "full"),
    ("GET", "/api/ops/hosts/{ident}/alerts/markers", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "主机时间窗告警标记（D119/M4.2）", "full"),
    ("GET", "/api/ops/metrics/list", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "指标名发现（D118 多系列图）", "full"),
    # ---- 认证与壳 ----
    ("GET", "/api/health", None, {"anon": {200}}, "健康检查（免认证）", "full"),
    ("POST", "/api/auth/login", {"username": "admin", "password": "wrong-pw"}, {"anon": {401}}, "登录（密码错误→401）", "full"),
    ("POST", "/api/auth/demo", {}, {"anon": OK, "demo": OK}, "演示登录（开关关闭→403）", "full"),
    ("GET", "/api/auth/me", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "当前用户", "full"),
    # D97：UI 中英文切换——写自己的 preferred_lang，不要求 write 权限；不存在/非法值 → 400/404
    ("PATCH", f"/api/auth/me/lang", {"preferred_lang": "en"}, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "切换 UI 语言（自服务）", "full"),
    ("GET", "/api/auth/perms", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "当前角色能力集（前端按钮可见性唯一来源）", "full"),
    # D18 补：自助改密（手册 §1 承诺"首次登录后请立即修改"）。anon 401；登录态但原密码错 → 400
    ("POST", "/api/auth/password", {"old_password": "definitely-wrong", "new_password": "whatever-123"},
     {"anon": {401}, "demo": {400}, "member": {400}}, "自助改密（原密码错→400）", "deny"),
    ("GET", "/api/audit-logs", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "审计检索", "full"),
    ("GET", "/api/license", None, {"anon": {401}, "demo": OK, "admin": OK}, "License 详情", "full"),
    ("GET", "/api/license/state", None, {"anon": {401}, "demo": OK, "admin": OK}, "License 门禁状态", "full"),
    ("POST", "/api/license/activate", {"license_key": "x"}, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {400, 402, 422}}, "激活（坏密钥→400/422）", "full"),
    ("GET", "/api/sse-ticket", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "SSE 换票", "full"),

    # ---- 主机与指标 ----
    ("GET", "/api/ops/hosts", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "主机列表", "full"),
    ("GET", "/api/ops/hosts/no-such-host", None, {"anon": {401}, "demo": OK, "admin": {404}}, "主机详情（不存在→404）", "full"),
    ("GET", "/api/ops/metrics/query", None, {"anon": {401}, "demo": OK, "admin": OK}, "指标查询", "full"),
    ("GET", "/api/ops/dashboard/overview", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "总览大盘", "full"),
    ("GET", "/api/ops/install-token", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "签发安装令牌（正式路径）", "full"),
    ("GET", "/api/ops/dev/mint-install-token", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "签发安装令牌（兼容别名）", "full"),
    ("POST", "/api/ops/agents/register", {"fingerprint": "d8-fp", "hostname": "d8", "install_token": "bad"}, {"anon": {401, 403}, "admin": {400, 401, 403}}, "Agent 注册（坏令牌→拒绝，且不能 5xx）", "full"),
    ("POST", "/api/ops/agents/metrics", {"ts": "2026-01-01T00:00:00Z", "metrics": []}, {"anon": {401, 403}}, "Agent 上报（无 X-Ident→拒绝）", "full"),

    # ---- 告警规则（写=admin 专属）----
    ("GET", "/api/ops/alert-rules", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "规则列表", "full"),
    ("POST", "/api/ops/alert-rules", {"name": "d8-rule", "scope": "metric", "metric": "cpu.percent", "expr": ">90"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "建规则（admin 专属）", "full"),
    ("PUT", f"/api/ops/alert-rules/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "改规则", "full"),
    ("DELETE", f"/api/ops/alert-rules/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "删规则", "full"),
    ("POST", f"/api/ops/alert-rules/{FAKE_ID}/toggle", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "启停规则（D61）", "full"),
    ("POST", f"/api/ops/alert-rules/{FAKE_ID}/test", {"window": "30m"}, {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "规则试运行", "full"),

    # ---- 告警操作（member 有告警操作权：03 §权限矩阵）----
    ("GET", "/api/ops/alerts", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "告警列表", "full"),
    ("POST", f"/api/ops/alerts/{FAKE_HASH}/ack", None, {"anon": {401}, "demo": DENY, "member": {200, 404}, "admin": {200, 404}}, "认领告警", "full"),
    ("POST", f"/api/ops/alerts/{FAKE_HASH}/resolve", None, {"anon": {401}, "demo": DENY, "member": {200, 404}, "admin": {200, 404}}, "关闭告警", "full"),
    ("POST", "/api/ops/alerts/bulk", {"hashes": [], "action": "ack"}, {"anon": {401}, "demo": DENY, "member": OK, "admin": OK}, "批量告警操作", "full"),
    ("GET", "/api/ops/alerts/stream", None, {"anon": {401}, "demo": OK, "admin": OK}, "告警 SSE（无票→401）", "sse"),
    ("GET", "/api/ops/alert-history", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "告警历史（D166）", "full"),
    ("POST", f"/api/ops/incidents/{FAKE_INC}/findings/{FAKE_INC}/triage", {"action": "observing", "reason": ""}, {"anon": {401}, "demo": DENY, "member": {200, 404}, "admin": {200, 404}}, "诊断报告分诊（D170）", "full"),
    ("GET", "/api/ops/alert-mutes", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "屏蔽列表", "full"),
    ("POST", "/api/ops/alert-mutes", {"cause": "d8", "rule_id": FAKE_ID, "mute_type": 1, "duration_s": 60}, {"anon": {401}, "demo": DENY, "member": OK, "admin": OK}, "建屏蔽", "full"),
    ("DELETE", f"/api/ops/alert-mutes/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": {200, 404}, "admin": {200, 404}}, "删屏蔽", "full"),

    # ---- 通知渠道（admin 专属）----
    ("GET", "/api/ops/notify-channels", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "渠道列表", "full"),
    ("POST", "/api/ops/notify-channels", {"name": "d8-ch", "type": "webhook", "config_json": {"webhook_url": "http://127.0.0.1:1/x"}},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "建渠道（admin 专属）", "full"),
    ("PUT", f"/api/ops/notify-channels/{FAKE_ID}", {"name": "d8-ch-x", "type": "webhook", "config_json": {}},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "改渠道（D59）", "full"),
    ("POST", f"/api/ops/notify-channels/{FAKE_ID}/test", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "测渠道", "full"),
    ("DELETE", f"/api/ops/notify-channels/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "删渠道", "full"),
    ("GET", "/api/ops/notification-records", None, {"anon": {401}, "demo": OK, "admin": OK}, "通知记录", "full"),

    # ---- P5：服务级监控与任务通道（FR-408/409/411）----
    ("GET", "/api/ops/services", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "服务清单", "full"),
    ("GET", "/api/ops/services/overview", None, {"anon": {401}, "demo": OK, "admin": OK}, "服务总览", "full"),
    ("GET", "/api/ops/services/templates", None, {"anon": {401}, "demo": OK, "admin": OK}, "服务模板", "full"),
    ("POST", "/api/ops/services", {"ident": "nope", "type": "app", "name": "x"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "登记服务（write 专属）", "full"),
    ("PATCH", f"/api/ops/services/{FAKE_ID}", {"log_path": "/var/log/x.log"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "改服务声明", "full"),
    ("DELETE", f"/api/ops/services/{FAKE_ID}", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "删服务", "full"),
    ("GET", f"/api/ops/services/{FAKE_ID}/heal-state", None,
     {"anon": {401}, "demo": OK, "admin": OK}, "服务自愈状态", "full"),
    ("POST", f"/api/ops/services/{FAKE_ID}/log-fetch", {"anchor": "2026-10-16T10:00:00"},
     {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "按服务下发取证（diag 级）", "full"),
    ("POST", f"/api/ops/services/{FAKE_ID}/heal-reset", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "重置自愈熔断（write 专属）", "full"),
    ("POST", f"/api/ops/services/{FAKE_ID}/heal", {"action": "restart_service", "mode": "suggest"},
     {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "服务恢复（diag 级；suggest 不下发）", "full"),
    ("POST", f"/api/ops/services/{FAKE_ID}/heal", {"action": "reboot", "mode": "auto"},
     {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "非白名单动作必须拒（404 先于 400）", "full"),
    ("GET", "/api/ops/heal/actions", None, {"anon": {401}, "demo": OK, "admin": OK}, "恢复动作清单", "full"),
    ("GET", f"/api/ops/services/{FAKE_ID}/heal/preview", None,
     {"anon": {401}, "demo": {400, 404}, "admin": {400, 404}}, "恢复护栏预览（只读 dry-run）", "full"),
    # D140 自动自愈策略
    ("GET", "/api/ops/heal-policies", None,
     {"anon": {401}, "demo": OK, "admin": OK}, "自愈策略列表", "full"),
    ("POST", "/api/ops/heal-policies", {"name": "x", "action": "restart_service", "mode": "suggest",
      "trigger_metric": "cpu.percent"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 400, 409}}, "建自愈策略（write）", "full"),
    ("PUT", f"/api/ops/heal-policies/{FAKE_ID}", {"name": "x", "action": "restart_service",
      "mode": "suggest", "trigger_metric": "cpu.percent"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "改自愈策略（write）", "full"),
    ("DELETE", f"/api/ops/heal-policies/{FAKE_ID}", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "删自愈策略（write）", "full"),
    ("POST", "/api/ops/heal-policies/recommend-threshold", {"metric": "cpu.percent"},
     {"anon": {401}, "demo": OK, "admin": {200}}, "AI 阈值推荐（read，统计推断）", "full"),
    ("GET", "/api/ops/heal-policy-runs", None,
     {"anon": {401}, "demo": OK, "admin": OK}, "自愈执行留痕列表", "full"),
    ("POST", f"/api/ops/heal-policy-runs/{FAKE_ID}/confirm", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404, 409}}, "确认执行（write + 状态机校验）", "full"),
    ("POST", f"/api/ops/heal-policies/{FAKE_ID}/dry-run", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404, 409}}, "即时演练（write）", "full"),
    ("GET", "/api/ops/heal/kill-switch", None, {"anon": {401}, "demo": OK, "admin": OK}, "自愈熔断开关状态", "full"),
    ("POST", "/api/ops/heal/kill-switch", {"enabled": False},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "切换自愈熔断（write 专属）", "full"),
    ("GET", "/api/ops/tasks", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "任务列表", "full"),
    ("GET", "/api/ops/tasks/recent", None, {"anon": {401}, "demo": OK, "admin": OK}, "最近任务", "full"),
    ("GET", f"/api/ops/tasks/{FAKE_ID}", None, {"anon": {401}, "demo": OK, "admin": {404}}, "任务详情", "full"),
    ("GET", f"/api/ops/incidents/{FAKE_ID}/context", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": {404}}, "事件错误上下文（P5b）", "full"),
    ("POST", "/api/ops/log-fetch", {"ident": "nope", "path": "/var/log/x.log"},
     {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "下发取证（diag 级）", "full"),
    # Agent 侧通道：无鉴权头一律 401（Agent 身份由 X-Ident/X-Token 承担）
    ("GET", "/api/ops/agents/tasks", None, {"anon": {401}, "demo": {401}, "member": {401}, "admin": {401}},
     "Agent 领取任务（须 X-Ident/X-Token）", "full"),
    ("GET", "/api/ops/agents/config", None, {"anon": {401}, "demo": {401}, "member": {401}, "admin": {401}},
     "Agent 领取平台声明（服务清单；须 X-Ident/X-Token）", "full"),
    ("POST", f"/api/ops/agents/tasks/{FAKE_ID}/result", {"result": {}},
     {"anon": {401}, "demo": {401}, "member": {401}, "admin": {401}}, "Agent 回填结果（须 Agent 身份）", "full"),

    # ---- 事件看板与诊断 ----
    ("GET", "/api/ops/incidents", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "事件列表", "full"),
    ("GET", f"/api/ops/incidents/{FAKE_ID}", None, {"anon": {401}, "demo": OK, "admin": {404}}, "事件详情", "full"),
    ("GET", f"/api/ops/incidents/{FAKE_ID}/findings", None, {"anon": {401}, "demo": OK, "admin": {200, 404}}, "诊断报告（无报告时 data=null）", "full"),
    ("GET", f"/api/ops/incidents/{FAKE_ID}/corrections", None,
     {"anon": {401}, "demo": {404}, "member": {404}, "admin": {404}}, "人工订正回读（不存在→404）", "full"),
    ("POST", f"/api/ops/incidents/{FAKE_ID}/correct", {"root_cause": "d8 订正"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "人工订正（不存在→404）", "full"),
    ("GET", f"/api/ops/incidents/by-alert/{FAKE_HASH}", None,
     {"anon": {401}, "demo": {404}, "member": {404}, "admin": {404}}, "按告警指纹反查事件（不存在→404）", "full"),
    ("GET", "/api/ops/todo-counts", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "大盘待我处理计数", "full"),
    ("POST", f"/api/ops/incidents/{FAKE_ID}/ack", None, {"anon": {401}, "demo": DENY, "member": {200, 404}, "admin": {200, 404}}, "认领事件", "full"),
    ("POST", f"/api/ops/incidents/{FAKE_ID}/close", None, {"anon": {401}, "demo": DENY, "member": {200, 404}, "admin": {200, 404}}, "关闭事件", "full"),
    ("POST", "/api/ops/incidents/merge", {"keep_id": FAKE_ID, "merge_id": FAKE_ID + 1}, {"anon": {401}, "demo": DENY, "member": {400, 404}, "admin": {400, 404}}, "合并事件（不存在→404）", "full"),
    ("POST", f"/api/ops/incidents/{FAKE_ID}/split", {"alert_hash": FAKE_HASH}, {"anon": {401}, "demo": DENY, "member": {400, 404}, "admin": {400, 404}}, "拆分事件（不存在→404）", "full"),
    ("POST", f"/api/ops/incidents/{FAKE_ID}/diagnose", None, {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "触发诊断", "auth"),
    ("GET", f"/api/ops/incidents/{FAKE_ID}/diagnose/stream", None, {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "诊断 SSE", "sse"),

    # ---- ChatOps（demo 允许只读问答：03 §权限矩阵；DEMO_CHAT_ENABLED=false 可关）----
    # D47 补：AI 助手的实时建议问题（状态驱动 + 模型生成；规则版兜底）
    ("GET", "/api/ops/chat/suggestions", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "助手实时建议问题（读缓存，不调模型）", "full"),
    # D49 补：手动刷新建议问题（真跑 LLM，成功路径跳过——"auth" 模式只测拒绝类角色）
    ("POST", "/api/ops/chat/suggestions/refresh", None,
     {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "手动刷新助手建议问题（真调模型）", "auth"),
    # D56 补：主机改名（demo/member 无 alert_ops/write 权限 → 403；不存在的主机 404 也算通过）
    ("POST", f"/api/ops/hosts/{FAKE_ID}/rename", {"name": "x"},
     {"anon": {401}, "demo": {403}, "member": {403, 404}, "admin": {200, 404}}, "主机改名（只改显示名）", "full"),
    ("GET", "/api/ops/chat/sessions", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "会话列表", "full"),
    ("GET", f"/api/ops/chat/sessions/{FAKE_ID}", None, {"anon": {401}, "demo": {200, 404}, "admin": {200, 404}}, "会话详情", "full"),
    ("DELETE", f"/api/ops/chat/sessions/{FAKE_ID}", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "删自己的会话（自服务）", "full"),
    ("POST", f"/api/ops/chat/sessions/{FAKE_ID}/compact", None, {"anon": {401}, "demo": {404}, "member": {404}, "admin": {404}}, "手动压缩会话历史（不存在 → 404；不写坏数据）", "full"),
    ("POST", "/api/ops/chat/stream", {"message": "只回答一个词：ok"}, {"anon": {401}, "demo": OK}, "ChatOps 流式问答（demo 可用只读问答，流会自行收敛）", "full"),

    # ---- 巡检（任务写=admin；触发=member 及以上）----
    ("GET", "/api/ops/inspections", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "巡检列表", "full"),
    ("POST", "/api/ops/inspections", {"name": "d8-insp", "cron_expr": "0 9 * * *"}, {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "建巡检（admin 专属）", "full"),
    ("GET", f"/api/ops/inspections/{FAKE_ID}", None, {"anon": {401}, "demo": OK, "admin": {404}}, "巡检详情", "full"),
    ("POST", f"/api/ops/inspections/{FAKE_ID}/run", None, {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "跑巡检", "auth"),
    # D39 补：巡检任务的编辑与删除（此前只有建/跑，手册 §8 承诺"cron 改了会自动生效"却无入口）
    ("PUT", f"/api/ops/inspections/{FAKE_ID}", {"name": "x", "cron_expr": "0 1 * * *"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "编辑巡检（admin 专属）", "full"),
    ("DELETE", f"/api/ops/inspections/{FAKE_ID}", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "删除巡检（admin 专属）", "full"),
    ("POST", "/api/ops/inspections/run-once", None, {"anon": {401}, "demo": DENY, "member": OK, "admin": OK}, "立即巡检（真跑 LLM，只验鉴权）", "auth"),

    # ---- 处置与变更（处置执行=admin 专属）----
    ("POST", "/api/ops/runbooks", {"name": "d8 手动变更单", "host_ident": "h", "risk_level": "low",
                                    "execution_mode": "manual",
                                    "steps": [{"desc": "看磁盘", "cmd": "df -h", "mode": "readonly"}]},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "手动建变更单（admin）", "full"),
    ("GET", "/api/ops/runbooks", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "处置单列表", "full"),
    ("GET", f"/api/ops/runbooks/{FAKE_ID}", None, {"anon": {401}, "demo": OK, "admin": {404}}, "处置单详情", "full"),
    ("POST", f"/api/ops/runbooks/generate/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "生成处置单", "auth"),
    ("PUT", f"/api/ops/runbooks/{FAKE_ID}", {"steps": []}, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "改处置单（admin 专属）", "full"),
    ("POST", f"/api/ops/runbooks/{FAKE_ID}/execute", {"step_idx": 0, "approved": False}, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {400, 403, 404}}, "执行处置步骤（admin 专属）", "full"),

    # ---- 技能与案例 ----
    ("GET", "/api/ops/skills", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "技能列表", "full"),
    ("GET", f"/api/ops/skills/{FAKE_ID}", None, {"anon": {401}, "demo": OK, "admin": {404}}, "技能详情", "full"),
    ("POST", "/api/ops/skills", {"name": "d8-skill", "description": "x", "body_md": "y"}, {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "建技能（admin 专属）", "full"),
    ("POST", f"/api/ops/skills/{FAKE_ID}/toggle", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "开关技能", "full"),
    ("GET", "/api/ops/case-cards", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "案例卡列表", "full"),
    ("GET", "/api/ops/case-cards/recall", None, {"anon": {401}, "demo": OK, "admin": OK}, "案例召回", "full"),
    ("GET", "/api/ops/case-cards/stats", None, {"anon": {401}, "demo": OK, "admin": OK}, "案例统计", "full"),
    ("POST", "/api/ops/case-cards", {"symptom": "d8 现象", "root_cause": "d8 根因", "resolution_md": "d8 处置"},
     {"anon": {401}, "demo": DENY, "member": DENY}, "案例卡写入（admin 专属；真调 embedding，故只验拒绝路径）", "auth"),
    ("PATCH", f"/api/ops/case-cards/{FAKE_ID}", {"enabled": False},
     {"anon": {401}, "demo": {403}, "member": {403}, "admin": {404}}, "编辑案例卡（FR-416）", "full"),
    ("DELETE", f"/api/ops/case-cards/{FAKE_ID}", None,
     {"anon": {401}, "demo": {403}, "member": {403}, "admin": {404}}, "删除案例卡（FR-416）", "full"),
    ("POST", "/api/ops/skills/import", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK},
     "导入内置技能（admin 专属，幂等）", "full"),
    ("GET", f"/api/ops/skills/{FAKE_ID}/effect", None, {"anon": {401}, "demo": OK, "member": OK, "admin": {404}},
     "技能效果指标（D173/P4：可从真实流水复算）", "full"),
    ("POST", "/api/ops/skills/import-github", {"url": "http://192.168.1.1/SKILL.md"},
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {400}},
     "从 GitHub 导入技能（D173/P3：非 GitHub 域名一律 400）", "full"),

    # ---- Prometheus / 外部数据源（admin 专属）----
    ("GET", "/api/ops/prom-datasources", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "数据源列表", "full"),
    ("POST", "/api/ops/prom-datasources", {"name": "d8-ds", "base_url": "http://127.0.0.1:1"}, {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "建数据源（admin 专属）", "full"),
    ("PUT", f"/api/ops/prom-datasources/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "改数据源", "full"),
    ("DELETE", f"/api/ops/prom-datasources/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {200, 404}}, "删数据源", "full"),
    ("POST", f"/api/ops/prom-datasources/{FAKE_ID}/test", None, {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "测数据源（diag 级）", "full"),
    ("GET", "/api/ops/prom/query", None, {"anon": {401}, "demo": OK, "admin": OK}, "Prom 即时查询", "full"),

    # ---- LLM 配置（admin 专属）----
    ("GET", "/api/llm-configs", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "LLM 配置列表", "full"),
    ("GET", "/api/llm-configs/usage", None, {"anon": {401}, "demo": OK, "admin": OK}, "用量统计", "full"),
    ("POST", "/api/llm-configs", {"name": "d8-llm", "base_url": "http://127.0.0.1:1/v1", "model": "m"}, {"anon": {401}, "demo": DENY, "member": DENY, "admin": OK}, "建 LLM 配置", "full"),
    ("PUT", f"/api/llm-configs/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "改 LLM 配置", "full"),
    ("POST", f"/api/llm-configs/{FAKE_ID}/test", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "测 LLM 配置", "full"),
    ("DELETE", f"/api/llm-configs/{FAKE_ID}", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}}, "删 LLM 配置", "full"),

    # ---- D9 补齐的契约缺口接口 ----
    ("POST", f"/api/ops/prom-datasources/{FAKE_ID}/scrape-now", None,
     {"anon": {401}, "demo": DENY, "member": {404}, "admin": {404}}, "exporter 直采一次（diag 级）", "full"),
    ("GET", "/api/ops/dev/engine-state", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK},
     "引擎自检（对账心跳/worker 清单）", "full"),
    ("GET", "/api/ops/action-allowlist", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK},
     "可自动执行的动作清单", "full"),
    ("POST", f"/api/ops/runbooks/{FAKE_ID}/steps/0/confirm", None,
     {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}},
     "签发执行确认凭据（处置执行级：admin；不存在→404）", "full"),
    ("GET", "/api/ops/action-audit", None, {"anon": {401}, "demo": OK, "member": OK, "admin": OK}, "全局处置动作审计", "full"),
    ("POST", "/api/ops/agents/heartbeat", {"agent_version": "0.1.0"}, {"anon": {401}, "admin": {401}}, "Agent 心跳（无 X-Ident→401）", "full"),
    ("POST", "/api/ops/incidents", {"title": "d8 手动事件", "severity": 2},
     {"anon": {401}, "demo": DENY, "member": OK, "admin": OK}, "手动建事件（告警操作级）", "full"),
    ("POST", f"/api/ops/skills/{FAKE_ID}/check", None, {"anon": {401}, "demo": DENY, "member": DENY, "admin": {404}},
     "技能连接检查（admin）", "full"),

    # ---- Agent 分发（免认证静态产物，FR-B2）----
    ("GET", "/install.sh", None, {"anon": {200}}, "Agent 安装脚本", "full"),
    ("GET", "/agent_main.py", None, {"anon": {200}}, "Agent 主程序", "full"),

    # ---- D154：MCP 端点（FR-425；默认关闭 → 403；anon 401=未带令牌）----
    ("GET", "/mcp", None, {"anon": {200}, "demo": OK, "member": OK, "admin": OK}, "MCP 端点状态（D154）", "full"),
    # D184：诊断评测（admin 专属——demo/member 即使有 write 权限也被 _admin 拒 403）
    ("GET", "/api/ops/eval/cases", None,
     {"anon": {401}, "demo": {403}, "member": {403}, "admin": OK}, "评测 case 池（FR-428d）", "full"),
    ("GET", "/api/ops/eval/runs", None,
     {"anon": {401}, "demo": {403}, "member": {403}, "admin": OK}, "评测历次记录（FR-428d）", "full"),
    ("POST", "/api/ops/eval/runs", {"pool": "regress"},
     {"anon": {401}, "demo": {403}, "member": {403}, "admin": {200}}, "跑一轮评测（FR-428d）", "full"),
    ("PUT", "/api/ops/eval/cases/0", {"expected": "disk_full"},
     {"anon": {401}, "demo": {403}, "member": {403}, "admin": {200, 404}}, "人工标注 case（FR-428c）", "full"),
    ("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
     {"anon": {401, 403}, "demo": {200, 403}, "member": {200, 403}, "admin": {200, 403}}, "MCP JSON-RPC（D154）", "full"),
]

ROLE_NAMES = ("anon", "demo", "member", "admin")
DENIAL_SETS = ({"401"}, DENY, {401, 403}, {401, 403, 400})


def _should_probe(mode: str, expect: set[int]) -> bool:
    """拒绝类角色恒测；重活/长连接接口的成功路径跳过（理由见上面的模式说明）。"""
    if mode not in ("auth", "sse"):
        return True
    return expect in DENIAL_SETS


def _matches(template: str, concrete: str) -> bool:
    """`/api/ops/alert-rules/{rid}` 是否匹配 `/api/ops/alert-rules/999999`。"""
    t = template.strip("/").split("/")
    c = concrete.strip("/").split("/")
    if len(t) != len(c):
        return False
    return all(tp.startswith("{") or tp == cp for tp, cp in zip(t, c))


def test_route_inventory_is_complete():
    """路由覆盖守卫：OpenAPI 契约里出现的每个路由都必须在 ROUTES 表里登记。

    以 OpenAPI 为准而不是遍历 app.routes：本版 FastAPI 把 include_router 的产物留在
    `_IncludedRouter` 对象里，遍历 app.routes 只能看到 1 个 /api/health（实测）。
    `include_in_schema=False` 的 Agent 分发产物手动补进来。
    """
    actual = {(m.upper(), p) for p, ops in SPEC["paths"].items() for m in ops}
    actual |= {("GET", "/install.sh"), ("GET", "/agent_main.py")}

    missing = sorted(f"{m} {p}" for m, p in actual
                     if not any(am == m and _matches(p, cp) for am, cp, *_ in ROUTES))
    assert not missing, "以下路由未纳入矩阵测试（请在 ROUTES 表补登记）：\n" + "\n".join(missing)

    stale = sorted(f"{m} {p}" for m, p, *_ in ROUTES
                   if not any(am == m and _matches(ap, p) for am, ap in actual))
    assert not stale, "ROUTES 表里有已不存在的路由：\n" + "\n".join(stale)


SPEC = app.openapi()


def _resolve(node, spec=SPEC):
    """解开 $ref，同时把 nullable 标记拍平（nullable 字段在样本阶段必须给非 None）。"""
    while isinstance(node, dict) and "$ref" in node:
        node = spec["components"]["schemas"][node["$ref"].split("/")[-1]]
    if not node:
        return {}
    if "anyOf" in node and len(node["anyOf"]) == 2 and {"type": "null"} in [x for x in node["anyOf"] if isinstance(x, dict)]:
        for v in node["anyOf"]:
            if isinstance(v, dict) and v.get("type") and v["type"] != "null":
                node = {**node, **v, "required": node.get("required", True)}
                break
    return node


def _sample(name: str, schema) -> object:
    """按 schema 造一个合法样本值（枚举取首个；字符串取上下文合理值）。"""
    s = _resolve(schema)
    if s.get("enum"):
        return s["enum"][0]
    t = s.get("type")
    if t == "integer":
        return _SAFE_INT.get(name, 1)
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "string":
        # 这些字段在 DB 里是 ENUM，合法取值 pydantic schema 看不出来，必须显式给
        return {"cron_expr": "0 9 * * *", "expr": "> 90", "query": "up", "target_scope": "all",
                "action": "ack", "alert_hash": "d8nosuchhash", "status": "open",
                "algorithm": "threshold", "scope": "metric", "auth_type": "none",
                "provider": "openai", "risk_level": "low", "execution_mode": "manual",
                "status": "open"}.get(name, _SAFE_STR.get(name, "d8"))
    if t == "array":
        return []
    if t == "object":
        return {k: _sample(k, v) for k, v in (s.get("properties") or {}).items() if v.get("type") != "null"}
    return None


# 矩阵表只为测**路由可达性 + 权限**；为这些字段填占位符会**误触**新增的业务校验
# （cron_pattern 必须合法、apprise scheme 必须在白名单等），用 admin 正常 body 跑矩阵
# 时跳过它们 → 走"未提供 = 跳过该校验"的默认路径。
_AUTOFILL_SKIP = {"cron_pattern"}

# 一些整数字段 autofill 默认采样 1，但业务有下限钳制：填 1 会触发 400。
# 这里给每类字段定义一个**对 admin 正常用例无害**的占位值（不在业务边界上）。
_SAFE_INT = {
    "severity": 2,                # S2（合法档位）
    "eval_interval_s": 15,        # 引擎默认 15s
    "window_s": 300,              # 默认 5 分钟
    "for_duration_s": 0,
    "notify_repeat_step_min": 0,
    "notify_max_number": 0,
    "recover_duration_s": 300,
    "datasource_id": 1,
    "mute_type": 0,                # 全屏蔽
}

# 字符串字段里有些是日期/时间戳，"d8" 占位会让 Pydantic / 业务校验抛 ValueError → 500。
# 矩阵只测**路由可达性 + 权限**，给这些字段一个**业务上无害**的默认值。
_SAFE_STR = {
    "btime": "2030-01-01T00:00:00",
    "etime": "2030-01-01T00:00:01",
    "target": "d8-target",
    "host": "d8-host",
    "name": "d8-name",
}


def _route_spec(method: str, concrete: str):
    """按具体路径找回 openapi 里的路由定义（处理 {param} 占位）。"""
    for tpl, ops in SPEC["paths"].items():
        if method.lower() in ops and _matches(tpl, concrete):
            return ops[method.lower()]
    return {}


def _autofill(method: str, path: str, body):
    """把请求体/查询参数补全成合法形状。

    动机：矩阵表手写 body 很容易漏字段，结果测试报的是 422（我自己写错了）而不是
    真正想验的权限/状态码。这里只补**缺失的必填项**，表里显式给的值优先。
    """
    op = _route_spec(method, path)
    params: dict = {}
    for q in op.get("parameters", []):
        if q.get("in") == "query" and q.get("required") and q.get("name") not in (body or {}):
            params[q["name"]] = _sample(q["name"], q.get("schema", {}))

    rb = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
    if rb:
        sch = _resolve(rb)
        for key, prop in (sch.get("properties") or {}).items():
            _resolve(prop)
        data = {k: _sample(k, v) for k, v in (sch.get("properties") or {}).items() if k not in _AUTOFILL_SKIP}
        merged = {**data, **(body or {})}   # 表里给的值覆盖样本
        return merged, params
    return body, params


async def _call(ac, method: str, path: str, body, headers: dict, mode: str):
    """发一次请求（自动补全必填体/查询参数）。

    AIOPS_MATRIX_TRACE=1 时打印每条路由（排查"卡在哪条"用，平时静默）。
    """
    payload, params = _autofill(method, path, body)
    if os.getenv("AIOPS_MATRIX_TRACE"):
        print(f"  → {method} {path} params={params} body={payload} [{mode}]", flush=True)
    r = await ac.request(method, path, json=payload, params=params, headers=headers)
    return r.status_code, r.text[:800]


@pytest.mark.asyncio
async def test_role_matrix(client):
    """四种身份 × 全部路由：状态码必须落在期望集合内，且任何情况都不得 5xx。"""
    ac, hdrs = client
    failures: list[str] = []
    for method, path, body, expect, note, mode in ROUTES:
        for role in ROLE_NAMES:
            if role not in expect or not _should_probe(mode, expect[role]):
                continue
            code, text = await _call(ac, method, path, body, hdrs[role], mode)
            if code >= 500:
                failures.append(f"[5xx] {method} {path} ({role}) → {code} {text[:200]}  # {note}")
                continue
            if code not in expect[role]:
                failures.append(
                    f"[状态码] {method} {path} ({role}) → {code}，期望 {sorted(expect[role])}  # {note}")
    assert not failures, "权限矩阵不符：\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_no_5xx_for_any_role(client):
    """兜底：任何路由 × 任何身份都不允许 5xx（含未在 expect 里声明的角色）。"""
    ac, hdrs = client
    failures: list[str] = []
    for method, path, body, expect, note, mode in ROUTES:
        for role in ROLE_NAMES:
            if not _should_probe(mode, expect.get(role, {200})):
                continue
            code, text = await _call(ac, method, path, body, hdrs[role], mode)
            if code >= 500:
                failures.append(f"{method} {path} ({role}) → {code} {text[:200]}  # {note}")
    assert not failures, "出现 5xx：\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_duplicate_name_returns_409_not_500(client):
    """重名（唯一约束冲突）必须是 409 + 人话，不能是 500。

    D8 实测：`POST /api/ops/alert-rules` 用已存在的 name 建规则 → IntegrityError 未处理，
    直接 500 且把 SQL 堆栈打进日志，界面上只显示"HTTP 500"，用户完全不知道是重名。
    """
    ac, hdrs = client
    # notification_channels.name 有唯一约束（alert_rules.name 无，可重名——另一条观察项）
    body = {"name": "d8-dup-ch", "type": "webhook", "config_json": {"webhook_url": "http://127.0.0.1:1/x"}}
    first = await ac.post("/api/ops/notify-channels", json=body, headers=hdrs["admin"])
    assert first.status_code in (200, 201), first.text
    again = await ac.post("/api/ops/notify-channels", json=body, headers=hdrs["admin"])
    assert again.status_code == 409, f"重名应为 409，实得 {again.status_code}: {again.text[:200]}"
    j = again.json()
    assert j.get("msg") and ("唯一" in j["msg"] or "已存在" in j["msg"]), j


@pytest.mark.asyncio
async def test_error_envelope_is_uniform(client):
    """错误响应必须是 {code,msg,data} 且 msg 非空 —— 前端只认这个形状。

    FastAPI 默认把 HTTPException 序列化成 {"detail": ...}，前端因此拿不到 msg，
    403 在界面上会退化成"请求失败：HTTP 403"（本次实测缺陷）。此断言锁死修复。
    """
    ac, hdrs = client
    cases = [
        ("GET", "/api/ops/hosts", None, hdrs["anon"], 401, "匿名访问受保护资源"),
        ("POST", "/api/ops/alert-rules", {"name": "x"}, hdrs["demo"], 403, "demo 越权写"),
        ("GET", "/api/ops/hosts/no-such-host", None, hdrs["admin"], 404, "资源不存在"),
        ("POST", "/api/ops/alert-rules", {}, hdrs["admin"], 422, "请求体不合法"),
    ]
    bad: list[str] = []
    for method, path, body, headers, want, note in cases:
        r = await ac.request(method, path, json=body, headers=headers)
        if r.status_code != want:
            bad.append(f"{note}: 期望 {want} 实得 {r.status_code}")
            continue
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            bad.append(f"{note}: 非 JSON 响应 {r.text[:120]}")
            continue
        if not isinstance(j, dict) or "code" not in j or "msg" not in j:
            bad.append(f"{note}: 信封不是 {{code,msg,data}} → {j}")
        elif not str(j.get("msg") or "").strip():
            bad.append(f"{note}: msg 为空 → {j}")
        elif "detail" in j:
            bad.append(f"{note}: 仍带 FastAPI 默认 detail 字段 → {j}")
    assert not bad, "错误信封不统一：\n" + "\n".join(bad)


@pytest.mark.asyncio
async def test_perm_denial_is_audited(client):
    """越权被拒必须留痕（FR-A3）：拒绝路径要写审计，否则安全事件无从追溯。"""
    ac, hdrs = client
    r = await ac.post("/api/ops/alert-rules", json={"name": "d8-deny"}, headers=hdrs["demo"])
    assert r.status_code == 403, r.text
    async with SessionLocal() as db:
        got = (await db.execute(text(
            "SELECT user_id, action, resource FROM audit_logs WHERE action = 'perm_denied' "
            "ORDER BY id DESC LIMIT 5"))).all()
    assert any("alert-rules" in (row[2] or "") for row in got), f"越权未被审计：{got}"


@pytest.mark.asyncio
async def test_login_failure_reports_reason_not_session_expiry(client):
    """登录失败必须回"用户名或密码错误"，不能被 401 拦截器误报成"登录已失效"。"""
    ac, _ = client
    r = await ac.post("/api/auth/login", json={"username": "admin", "password": "definitely-wrong"})
    assert r.status_code == 401, r.text
    msg = (r.json().get("msg") or "")
    assert "密码" in msg or "错误" in msg, f"登录失败信息不可读：{r.json()}"


@pytest.mark.asyncio
async def test_chat_tools_accept_host_name_or_ident():
    """ChatOps/诊断工具的主机参数必须 ident 与 name 都能用（D8 实测缺陷）。

    背景：ident 是 Agent 指纹哈希，AI 与人说出来的都是主机名（mock-web-01）。
    实测「mock-db-01 的 CPU 现在多少？」→ query_metrics 按 ident 查 → "主机未注册"，
    用户看到的像是平台坏了，实际是标识口径不一致。
    """
    from app.models.host import Host
    from app.services import rca_tools

    ident, name = "d8toolident0123456789abcdef012345", "d8-tool-host"
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
        db.add(Host(ident=ident, name=name, ip="10.8.8.8", os="test", status="up", agent_token="t"))
        await db.commit()
    try:
        # 打进一个数据点：按**主机名**查询命中的应是同一台（并按规范 ident 取数）
        from app.store import ring

        ring.append_point(ident, "cpu.percent", datetime.utcnow(), 66.6)
        by_name = await rca_tools.query_metrics(name, "cpu.percent", minutes=30)
        assert by_name["status"] == "SUCCESS", by_name
        assert by_name["data"]["host"] == ident, by_name
        assert by_name["data"]["latest"] == 66.6, by_name
        await rca_tools.host_info(name)      # 主机信息同样要能用名字查
        bad = await rca_tools.query_metrics("no-such-host-xyz", "cpu.percent")
        assert bad["status"] == "NO_DATA" and "list_hosts" in bad["note"], bad
    finally:
        from app.store import ring as _ring

        _ring._rings.pop((ident, "cpu.percent"), None)
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM hosts WHERE ident = :i"), {"i": ident})
            await db.commit()


@pytest.mark.asyncio
async def test_cleanup_d8_artifacts():
    """清理本文件落库的 d8-* 产物，保持测试库干净（幂等，可重复运行）。"""
    await _cleanup_d8()


async def _cleanup_d8() -> None:
    from sqlalchemy import text as _text

    async with SessionLocal() as db:
        await db.execute(delete(AlertRule).where(AlertRule.name.like("d8-%")))
        await db.execute(_text("DELETE FROM alert_mutes WHERE cause LIKE 'd8%'"))
        await db.execute(_text("DELETE FROM incidents WHERE cluster_reason = 'manual_create' AND title LIKE 'd8 %'"))
        await db.execute(_text("DELETE FROM runbooks WHERE name LIKE 'd8 %'"))
        for table in ("notification_channels", "llm_configs", "prom_datasources", "inspections", "skills"):
            await db.execute(_text(f"DELETE FROM {table} WHERE name LIKE 'd8-%'"))
        # D154：新表也要清（d8-nrule 若留下会**接管真实 S1 事件**——互斥语义的副作用）
        await db.execute(_text("DELETE FROM notify_rules WHERE name LIKE 'd8-%'"))
        await db.execute(_text("DELETE FROM alert_subscribes WHERE name LIKE 'd8-%'"))
        await db.execute(_text("DELETE FROM busi_group_members WHERE team LIKE 'd8-%'"))
        await db.execute(_text("DELETE FROM busi_groups WHERE name LIKE 'd8-%'"))
        await db.commit()
