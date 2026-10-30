"""服务级监控：模板 / 解析 / 阈值判定 / 落库（P5a，FR-408；详设 §1.30）。

**分层（这是本模块最重要的设计决定）**：
  · **采集在 Agent 侧**——探测命令必须在目标机上跑（平台跑不了目标机的 `mysql -e "SHOW STATUS"`），
    且**凭据不出目标机**（用目标机自己的 my.cnf / 本地免密）；Agent 只回传**原始输出文本**。
  · **解析与判定在平台侧**——于是"改阈值、加指标、修解析"都是平台侧一次发布的事，
    不需要动目标机上的 Agent；解析逻辑也因此可被 pytest 直接覆盖。

三态语义（与探活一致，不发明新词）：
  up=关键指标正常 / degraded=取到但超阈 / down=探测失败且明确不可用 / unknown=取不到或未声明探法

诚实边界：取不到就说取不到（`unknown` + 人话 note + 原始报错摘要），**绝不猜一个 up**。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select

log = logging.getLogger("svcmon")

# ── 服务模板（可被平台侧调整，无需动 Agent）──────────────────────────
#   probe_key 供 Agent 侧执行（Agent 内置同名命令），here 只放展示/阈值/默认值
TEMPLATES: dict[str, dict] = {
    "mysql": {
        "label": "MySQL", "default_port": 3306, "default_unit": "mysql",
        "default_log": "/var/log/mysql/error.log",
        "hint": "用目标机自己的 my.cnf 连接（口令不出目标机）；取不到时多为本地免密未配置",
        "thresholds": {"conn_used_ratio": 0.80, "lock_waits": 1},
    },
    "redis": {
        "label": "Redis", "default_port": 6379, "default_unit": "redis",
        "default_log": "/var/log/redis/redis-server.log",
        "hint": "本地 redis-cli INFO；未开免密时需在目标机本地配置",
        "thresholds": {"mem_used_ratio": 0.85, "hit_rate_min": 0.5},
    },
    "nginx": {
        "label": "Nginx", "default_port": 80, "default_unit": "nginx",
        "default_log": "/var/log/nginx/error.log",
        "hint": "需在 nginx 配置开启 stub_status（location /stub_status）",
        "thresholds": {"conn_per_req": 0.5},
    },
    "mq": {
        "label": "消息队列", "default_port": 5672, "default_unit": "rabbitmq-server",
        "default_log": "/var/log/rabbitmq/rabbit.log",
        "hint": "RabbitMQ 管理 API（15672）；未开管理插件时取不到（会如实报 unknown）",
        "thresholds": {"max_queue_depth": 10000},
    },
    "app": {
        "label": "应用进程", "default_port": 0, "default_unit": "",
        "default_log": "",
        "hint": "按 spec 声明的 unit / 端口 / 健康端点探测",
        "thresholds": {},
    },
}


def parse_metrics(stype: str, raw: str) -> dict:
    """把 Agent 回传的原始输出解析成指标字典（纯函数，便于单测）。"""
    m: dict = {}
    if not raw:
        return m
    if stype == "mysql":
        kv: dict[str, str] = {}
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                parts = line.split()
            if len(parts) >= 2:
                kv[parts[0].strip()] = parts[1].strip()
        for k in ("Threads_connected", "Threads_running", "Slow_queries", "Aborted_connects",
                  "Innodb_row_lock_waits", "max_connections", "Uptime"):
            if k in kv:
                try:
                    m[k] = int(float(kv[k]))
                except Exception:  # noqa: BLE001
                    pass
        if m.get("max_connections"):
            m["conn_used_ratio"] = round(m.get("Threads_connected", 0) / m["max_connections"], 3)
    elif stype == "redis":
        for line in raw.splitlines():
            if line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k in ("used_memory", "used_memory_rss", "maxmemory", "connected_clients",
                     "instantaneous_ops_per_sec", "keyspace_hits", "keyspace_misses",
                     "expired_keys", "evicted_keys"):
                try:
                    m[k] = int(float(v))
                except Exception:  # noqa: BLE001
                    pass
        if m.get("maxmemory"):
            m["mem_used_ratio"] = round(m.get("used_memory", 0) / m["maxmemory"], 3)
        hits, miss = m.get("keyspace_hits", 0), m.get("keyspace_misses", 0)
        if hits + miss > 0:
            m["hit_rate"] = round(hits / (hits + miss), 3)
    elif stype == "nginx":
        nums: list[int] = []
        for line in raw.splitlines():
            for tok in line.replace(":", " ").split():
                if tok.isdigit():
                    nums.append(int(tok))
        if len(nums) >= 4:
            m["active_connections"], m["accepts"], m["handled"], m["requests"] = nums[:4]
            m["conn_per_req"] = round(m["active_connections"] / max(m["requests"], 1), 4)
    elif stype == "mq":
        try:
            arr = json.loads(raw)
            depth = [int(q.get("messages") or 0) for q in (arr if isinstance(arr, list) else [])
                     if isinstance(q, dict)]
            if depth:
                m["queues"] = len(depth)
                m["max_queue_depth"] = max(depth)
                m["total_messages"] = sum(depth)
        except Exception:  # noqa: BLE001
            pass
    return m


def judge(stype: str, metrics: dict) -> tuple[str, str]:
    """阈值判定 → (status, 人话说明)。阈值来自模板（平台侧可调）。"""
    th = (TEMPLATES.get(stype) or {}).get("thresholds") or {}
    warns: list[str] = []
    if stype == "mysql":
        r = metrics.get("conn_used_ratio")
        if r is not None and r >= float(th.get("conn_used_ratio", 0.8)):
            warns.append(f"连接数已用 {metrics.get('Threads_connected')}/{metrics.get('max_connections')}"
                         f"（{int(r * 100)}%，接近打满）")
        lw = metrics.get("Innodb_row_lock_waits") or 0
        if lw >= int(th.get("lock_waits", 1)):
            warns.append(f"存在行锁等待 {lw} 次")
    elif stype == "redis":
        r = metrics.get("mem_used_ratio")
        if r is not None and r >= float(th.get("mem_used_ratio", 0.85)):
            warns.append(f"内存使用 {int(r * 100)}%（超阈值）")
        hr = metrics.get("hit_rate")
        if hr is not None and hr < float(th.get("hit_rate_min", 0.5)):
            warns.append(f"缓存命中率偏低 {int(hr * 100)}%")
    elif stype == "nginx":
        c = metrics.get("conn_per_req")
        if c is not None and c > float(th.get("conn_per_req", 0.5)):
            warns.append(f"连接/请求比偏高 {c}（可能有慢上游）")
    elif stype == "mq":
        d = metrics.get("max_queue_depth") or 0
        if d >= int(th.get("max_queue_depth", 10000)):
            warns.append(f"队列积压 {d} 条（超阈值）")
    if warns:
        return "degraded", "；".join(warns)
    return "up", "关键指标正常"


def build_spec(stype: str, name: str, **kw) -> dict:
    """按模板补全一个服务 spec（端口/单元/日志默认值）。"""
    tpl = TEMPLATES.get(stype) or TEMPLATES["app"]
    return {"type": stype, "name": name,
            "port": kw.get("port") or tpl["default_port"],
            "unit": kw.get("unit") or tpl["default_unit"],
            "container": kw.get("container") or "",
            "log_path": kw.get("log_path") or tpl["default_log"],
            "process": kw.get("process") or "",
            "health_url": kw.get("health_url") or ""}


def _svc_out(s) -> dict:
    return {"id": s.id, "host_id": s.host_id, "name": s.name, "type": s.type,
            "spec": s.spec_json or {}, "status": s.status, "metrics": s.metrics_json or {},
            "note": s.note, "enabled": s.enabled,
            "last_check_at": str(s.last_check_at or ""),
            "label": (TEMPLATES.get(s.type) or {}).get("label", s.type)}


async def ingest_report(db, host_id: int, reported: list[dict]) -> int:
    """把 Agent 上报的服务探测结果落库（upsert by host+name）。

    `reported` 每项：{type, name, spec{...}, raw, ok, error}
      · `ok=False` 且带 error → 状态 unknown + note 写明原因（不猜 up）
      · 有 raw → 解析 + 判阈
    返回落库条数。
    """
    from app.models.svc import Service

    now = datetime.utcnow()
    n = 0
    for item in reported or []:
        stype = str(item.get("type") or "app").lower()
        name = str(item.get("name") or stype)[:64]
        row = (await db.execute(select(Service).where(
            Service.host_id == host_id, Service.name == name))).scalar_one_or_none()
        agent_spec = item.get("spec") or build_spec(stype, name)
        if row is None:
            row = Service(host_id=host_id, name=name, spec_json=agent_spec)
            db.add(row)
        else:
            # **平台侧声明为准，Agent 只补空缺**（实测踩到的坑：若无条件用 Agent 上报的 spec 覆盖，
            # 用户在平台上改的"日志路径"会在下一次心跳（约 1 分钟）被静默还原，
            # 取证直接失效且毫无提示）。
            cur = dict(row.spec_json or {})
            for k, v in agent_spec.items():
                if not cur.get(k):
                    cur[k] = v
            row.spec_json = cur
        row.type = stype if stype in TEMPLATES else "other"
        # 判定顺序很重要（验收实测）：**先看结构化检查结果，再看 ok**。
        # Agent 的 ok=False 有两种含义：① 探不动（没有 metrics）；② 探到了、三项全不通
        # （服务真的挂了）。第一版先判 ok=False → 把"服务挂了"误报成「未知 / 探测未成功」，
        # 严重度少报一级：用户看到"没测到"而不是"服务不可用"。
        app_metrics = item.get("metrics") if stype == "app" else None
        if app_metrics:
            m = app_metrics
            row.metrics_json = m
            checks = [v for v in m.values()]
            names = {"process_ok": "进程", "port_ok": "端口", "health_ok": "健康端点"}
            if all(checks):
                row.status, row.note = "up", "进程/端口/健康端点均正常"
            elif any(checks):
                bad = "、".join(names.get(k, k) for k, v in m.items() if not v)
                row.status, row.note = "degraded", f"{bad} 未通过"
            else:
                bad = "、".join(names.get(k, k) for k, v in m.items())
                row.status, row.note = "down", f"{bad}均不通——服务不可用"
            if item.get("error"):
                row.note = (row.note + "；" + str(item["error"]))[:255]
        elif not item.get("ok"):
            row.status = "unknown"
            row.metrics_json = {}
            row.note = str(item.get("error") or "探测未成功")[:255]
        else:
            metrics = parse_metrics(stype, str(item.get("raw") or ""))
            if not metrics:
                row.status = "unknown"
                row.note = f"有输出但未解析出关键指标：{str((TEMPLATES.get(stype) or {}).get('hint', ''))[:120]}"
                row.metrics_json = {}
            else:
                row.status, row.note = judge(stype, metrics)
                row.metrics_json = metrics
        row.last_check_at = now
        n += 1
    if n:
        await db.commit()
    return n


async def host_services(db, host_id: int) -> list[dict]:
    from app.models.svc import Service

    rows = (await db.execute(select(Service).where(Service.host_id == host_id)
                             .order_by(Service.id))).scalars().all()
    return [_svc_out(s) for s in rows]


async def _live_services(db) -> list:
    """只返回"主机仍在"的服务行。

    为什么要过滤（实测踩到）：主机行被删（测试清理/库重置）后，服务行会变成孤儿——
    界面上仍然列着，点「取日志」却报"主机不存在"，用户看得到却做不了。孤儿不该出现在
    业务视图里；这里过滤并记一条日志，同时 purge 脚本有对应清理规则。
    """
    from app.models.host import Host
    from app.models.svc import Service

    ids = {h.id for h in (await db.execute(select(Host))).scalars().all()}
    rows = (await db.execute(select(Service).order_by(Service.status, Service.id))).scalars().all()
    live = [r for r in rows if r.host_id in ids]
    if len(live) != len(rows):
        log.warning("过滤掉 %d 条孤儿服务行（主机已不存在）——可用 purge_test_junk 清理",
                    len(rows) - len(live))
    return live


async def overview(db, limit: int = 20) -> dict:
    """大盘用：不健康的服务清单 + 汇总计数。"""
    from app.models.host import Host
    from app.models.svc import Service

    rows = await _live_services(db)
    hosts = {h.id: (h.name or h.ident) for h in (await db.execute(select(Host))).scalars().all()}
    bad = [s for s in rows if s.status in ("down", "degraded")]
    return {
        "total": len(rows),
        "by_status": {k: sum(1 for s in rows if s.status == k)
                      for k in ("up", "degraded", "down", "unknown")},
        "unhealthy": [{**_svc_out(s), "host": hosts.get(s.host_id, str(s.host_id))}
                      for s in bad[:limit]],
    }
