# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""采集接入：注册/上报/心跳/状态（FR-B1/B5；05 §2 Agent 协议）。"""
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.core.license import new_agent_token
from app.models.collect import Probe
from app.models.host import Host
from app.store import ring

# Agent 上报指标名**契约**：规则 / 巡检 / 大盘 / 压测脚本都按这些名字取值。
# agent/agent_main.py 的 snapshot() 必须与之逐字一致 —— 有契约测试守着（真跑发现的漂移：
# 曾用 `load.load1` 与 `disk{mnt}.percent`，平台读不到，磁盘与负载在 Agent 主机上全空白）。
AGENT_METRIC_CONTRACT = ("cpu.percent", "mem.percent", "disk.percent", "load.1",
                         "net.bytes_sent", "net.bytes_recv")

# P1-12：平台侧最新 Agent 版本。与 agent/agent_main.py 的 AGENT_VERSION 同步发布；
# 主机列表用它计算 version_stale，前端给“可升级”提示与一键重装命令。
AGENT_LATEST_VERSION = "0.3.7"

log = logging.getLogger("ingest")


def _ver_tuple(v: str) -> tuple[int, int, int]:
    try:
        parts = [int(x) for x in str(v or "").split(".")[:3]]
        return tuple((parts + [0, 0, 0])[:3])  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return (0, 0, 0)


def is_agent_version_stale(agent_version: str, agent_code_sha256: str = "") -> bool:
    """版本漂移判定（D121）：**文件指纹优先**——上报的 code_sha256 与平台 serving 的
    agent_main.py 不一致即视为可升级（部署了新代码=全部主机可升级，不再依赖人记版本号）；
    旧 Agent（无 hash 上报）回落到版本号比较（未知/空版本视为需要升级）。"""
    from app.routers.agent_install import agent_code_sha256 as _serving_hash
    serving = _serving_hash()
    if agent_code_sha256:
        return agent_code_sha256 != serving
    # 无指纹上报 = 旧 Agent（不认识 self_upgrade 任务）——它必须先升级一次才能
    # 获得平台侧自升级能力，所以一律显示「可升级」（版本号仅作展示，不再参与判定）。
    return True

INSTALL_TOKENS: dict[str, str] = {}  # 一次性安装令牌（内存态；正式化为设置页生成，03 §1.4）


def mint_install_token() -> str:
    import secrets

    tok = "inst-" + secrets.token_hex(12)
    INSTALL_TOKENS[tok] = "unused"
    return tok


def _apply_hostinfo(host: Host, info: dict, now: datetime) -> None:
    """D146：吸收 Agent 上报的机器硬件配置（几核几 G / 开机时间）。

    只取自己需要的几项，且**不信任客户端时间**：boot_time 是开机时刻（合理），
    但"这次上报的时间"由平台自己记（hostinfo_at = now）。
    """
    if not isinstance(info, dict):
        return
    # 数值字段：**只在拿到正值时覆盖**（缺失/非法 → 保持库里已有的值，
    # 不许一次脏上报把已知的配置清成 0）
    def _num(key: str) -> int:
        try:
            return int(info.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    cores = _num("cpu_cores")
    if cores > 0:
        host.cpu_cores = cores
    phys = _num("cpu_cores_physical")
    if phys > 0:
        host.cpu_cores_physical = phys
    model = str(info.get("cpu_model") or "").strip()
    # D160-5：云主机常屏蔽 CPU 型号（/proc/cpuinfo 里只有 `model : 0`）——
    # 老 Agent 会把它当型号上报，界面显示成 "model: 0" 看着像故障。平台侧兜底：
    # 命中被遮蔽的形态就当"未暴露"（存空），别把占位符当事实展示。
    _masked = {"0", "unknown", "none", "n/a", "unspecified", "default",
               "model: 0", "model:0", "model name: 0"}
    if (model and model.lower() not in _masked and not model.lower().startswith("model: 0")
            and not model.strip().isdigit()):
        # 纯数字也不是型号（x86 的 `model : 85` 是 family/model 编码）→ 当"未暴露"
        host.cpu_model = model[:160]
    elif model:
        host.cpu_model = ""
    mem = _num("mem_total")
    if mem > 0:
        host.mem_total = mem
    bt = str(info.get("boot_time") or "").strip()
    if bt:
        try:
            parsed = datetime.fromisoformat(bt.replace("Z", ""))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            host.boot_time = parsed
        except ValueError:
            pass
    host.hostinfo_at = now


def _apply_agent_meta(host: Host, body: dict) -> None:
    """吸收 Agent 自监控字段（P0）。

    Agent 每次上报带 agent_meta；平台只取自己需要的几项，**不信任客户端时间**
    （last_success_at 由平台在收到请求时记，避免时钟偏差导致“刚上报却显示过期”）。
    """
    meta = body.get("agent_meta") if isinstance(body, dict) else None
    if not isinstance(meta, dict):
        return
    try:
        host.cache_depth = max(0, int(meta.get("cache_depth") or 0))
    except (TypeError, ValueError):
        host.cache_depth = 0
    err = str(meta.get("last_error") or "").strip()
    host.last_agent_error = err[:255]
    ver = str(meta.get("version") or "").strip()
    if ver:
        host.agent_version = ver[:32]
    sha = str(meta.get("code_sha256") or "").strip()   # D121：代码指纹（升级判定用）
    if sha:
        host.agent_code_sha256 = sha[:64]


async def _mark_auth_ok(host: Host, now: datetime) -> None:
    host.auth_state = "ok"
    host.last_auth_ok_at = now
    host.last_report_ok_at = now


def _pick_ip(cur: str, reported: str, peer: str) -> str:
    """D120 IP 择优：**公网地址 > 私网/保留地址**；同级别时 agent 自报 > 平台看到的对端 > 旧值。

    背景：VM-0-13 是 NAT 后的容器网段机（eth0=172.17.0.13），agent 自报只会是私网；
    平台看到的 TCP 对端（peer）才是真实出口。反过来内网直连部署时 peer 也是内网 IP、
    语义同样正确。三路统一走这一个函数，避免各处口径不一致。"""
    import ipaddress as _ipa

    def rank(ip: str) -> int:
        try:
            a = _ipa.ip_address(ip)
        except ValueError:
            return 2
        return 0 if (not a.is_private and not a.is_loopback and not a.is_link_local) else 1

    cands = sorted((rank(ip), prio, ip) for prio, ip in ((0, reported or ""), (1, peer or ""), (2, cur or "")) if ip)
    return cands[0][2] if cands else ""


async def register_agent(db, payload: dict, peer_ip: str = "") -> dict:
    fp = payload.get("fingerprint", "")
    if not fp:
        raise HTTPException(422, "fingerprint 必填")
    ident = hashlib.sha256(fp.encode()).hexdigest()[:32]
    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()

    # 安装令牌仅约束"首次接触"（评审 #13）：已注册主机的重启/重装免令牌。
    # D16 对抗测试修复（高危认证绕过）：原先写成 `if INSTALL_TOKENS:` ——
    # **令牌字典为空时整段跳过校验**，于是"所有令牌都已用掉/从未发放"时
    # 任何人都能不带令牌注册任意主机（实测：无 token 注册出 EVIL-HOST，
    # 还拿到了 agent_token 可持续上报伪造指标）。改为始终要求有效的一次性令牌。
    # 已移出（removed）的主机视为退役：重新接入需再次签发一次性安装令牌；
    # 普通离线（offline）可自由恢复。避免盗 ident 的 agent 让移出主机静默复活。
    if host is None or (host is not None and host.removed):
        tok = payload.get("install_token", "")
        if tok not in INSTALL_TOKENS or INSTALL_TOKENS[tok] != "unused":
            raise HTTPException(403, "安装令牌无效或已使用（请在平台『主机管理 → 接入新主机』生成）")
        INSTALL_TOKENS[tok] = "used"

    agent_token = new_agent_token()
    if host is None:
        # D56：注册名优先用 Agent 显式上报的 name（install.sh 自动生成的友好名 / --name 指定），
        # 没有（旧版 agent）才回落 hostname。重注册**不改名**——用户可能已在界面改过。
        name = (payload.get("name") or payload.get("hostname") or "").strip()
        host = Host(ident=ident, name=name, ip=(payload.get("ip") or "").strip() or peer_ip,
                    os=payload.get("os", ""), agent_version=payload.get("agent_version", ""))
    host.agent_token = agent_token
    host.status = "up"
    host.offline = False
    host.removed = False   # 重新接入 = 复原（若曾是下线/移出状态）
    # D120：重注册也刷新 IP。此前 ip 只在首次建行写入——老主机（注册早于 Agent
    # 上报 _detect_ip 的版本）升级后重启，IP 依旧是空串。这里按上报非空即刷新。
    new_ip = _pick_ip(host.ip, (payload.get("ip") or "").strip(), peer_ip)
    if new_ip and new_ip != host.ip:
        host.ip = new_ip[:64]
    now = datetime.utcnow()
    host.last_beat = now
    await _mark_auth_ok(host, now)
    _apply_agent_meta(host, payload)
    # D146：重注册也带一次硬件配置（首次接入就能在详情里看到几核几 G）
    if payload.get("hostinfo"):
        _apply_hostinfo(host, payload.get("hostinfo") or {}, now)
    # D63-2：重注册同步版本与系统串（否则 Agent 升级后列表永远显示旧版本，
    # 用户无从知道"重跑安装命令"是否生效；改名语义仍保持不覆盖）
    if payload.get("os"):
        host.os = payload["os"]
    if payload.get("agent_version"):
        host.agent_version = payload["agent_version"]
    db.add(host)
    await db.commit()
    return {"ident": ident, "token": agent_token}


async def auth_agent(db, ident: str, token: str) -> Host:
    host = (await db.execute(select(Host).where(Host.ident == ident))).scalar_one_or_none()
    if host is None or not token or token != host.agent_token:
        # P0：已知主机但凭证失效时，把状态落到主机行上——否则界面只能看到
        # last_beat 变旧，无法区分“机器关机”和“Agent 凭证失效”。
        if host is not None:
            host.auth_state = "auth_failed"
            host.last_agent_error = "Agent 凭证失效（401）；等待 Agent 自动重注册或重新接入"
            try:
                await db.commit()
            except Exception:  # noqa: BLE001 —— 状态写入失败不得改变 401 语义
                await db.rollback()
        raise HTTPException(401, "Agent 身份无效")
    # 手动下线/移出：拒绝继续上报（否则 heartbeat/metrics 会无条件把 status 拉回 up）。
    # 下线/移出的主机可在『主机管理』里恢复（重新开放 / 重新接入）。
    if host.offline or host.removed:
        # D155：这句话 Agent 会原样带给用户（它读 body 的 msg）→ 写清楚"这正常、
        # 数据在缓存里、恢复后会续传"，而不是只丢一个状态码。
        why = "已被移出（退役）" if host.removed else "已被下线"
        raise HTTPException(
            403,
            f"主机{why}，平台暂不接收其上报；这是预期行为，不是故障。"
            "Agent 会把数据缓存在本地，恢复上线后自动续传。",
        )
    return host


async def heartbeat(db, ident: str, token: str, body: dict | None = None, peer_ip: str = "") -> dict:
    """轻量心跳（FR-B1/B5、05 §2 Agent 协议）：只刷新 last_beat，不上报数据。

    与 metrics 共用身份校验；用于"Agent 活着但这一轮没有新数据"（例如间隔对齐、
    指标采集被跳过）时避免被误判 offline。
    """
    host = await auth_agent(db, ident, token)
    now = datetime.utcnow()
    host.last_beat = now
    host.status = "up"
    hb_ip = _pick_ip(host.ip, (body or {}).get("ip", ""), peer_ip)
    if hb_ip and hb_ip != host.ip:
        host.ip = hb_ip[:64]   # D120：公网地址优先，NAT 机的私网自报会被真实出口覆盖
    await _mark_auth_ok(host, now)
    b = body or {}
    if b.get("agent_version"):
        host.agent_version = str(b["agent_version"])[:32]
    _apply_agent_meta(host, b)
    # D155：心跳成功 → Agent 现在是通的 → 之前的失败已过去（见 ingest_metrics 里的说明）
    host.last_agent_error = ""
    await db.commit()
    return {"ident": ident, "server_time": now.isoformat() + "Z"}


async def ingest_metrics(db, ident: str, token: str, body: dict, peer_ip: str = "") -> None:
    host = await auth_agent(db, ident, token)
    now = datetime.utcnow()
    host.last_beat = now
    host.status = "up"
    mi_ip = _pick_ip(host.ip, (body or {}).get("ip", ""), peer_ip)
    if mi_ip and mi_ip != host.ip:
        host.ip = mi_ip[:64]   # D120：公网地址优先，NAT 机的私网自报会被真实出口覆盖
    await _mark_auth_ok(host, now)
    _apply_agent_meta(host, body)
    # D155：本次上报**成功收到** → Agent 现在是通的 → 之前那次失败已经过去了。
    # 语义从「最后一次错误（历史）」改成「**当前仍存在**的错误」——
    # 否则界面会同时显示「状态 在线 / 认证正常 / 最近成功上报 xx:xx / 最近错误 403」，
    # 用户无法判断此刻到底有没有问题（真实场景：主机被下线 10 小时，恢复后界面仍红）。
    # 为什么不需要新字段：Agent 的 meta 是在**发请求之前**构造的，成功那一轮带的仍是
    # 上一轮的旧错误（滞后一轮）；而「这次收到了」本身就是最可靠的恢复证据。
    # 历史错误不丢——审计日志与 Agent 自己的日志里都有。
    host.last_agent_error = ""

    if os.environ.get("AIOPS_DEBUG_INGEST_BODY"):
        log.warning("[debug-ingest] body keys=%s item keys=%s",
                    sorted(body.keys()),
                    sorted((body.get("batch") or [body])[0].keys()))
    batches = body.get("batch") or [body]  # 断网续传批（≤50，05 §2）
    for item in batches[:50]:
        ts = datetime.fromisoformat(item["ts"].replace("Z", "")) if "ts" in item else now
        if ts.tzinfo is not None:  # 统一 naive UTC（04 §1：库内一律 UTC 无时区）
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        for m in item.get("metrics", []):
            ring.append_point(ident, m["metric"], ts, float(m["value"]))
        for p in item.get("probes", []):
            await upsert_probe(db, host.id, p)
        # D146：机器硬件配置（几核几 G）——随心跳上报，落到 hosts 静态列
        if item.get("hostinfo"):
            _apply_hostinfo(host, item["hostinfo"], now)
        # D63：服务发现快照（监听端口 + 高 CPU 进程），只存内存、保最近一份
        if item.get("services"):
            ring.set_services(ident, item["services"])
        # 磁盘分区快照（每个挂载点 df -h 明细），同走内存、保最近一份
        if item.get("disks"):
            ring.set_disks(ident, item["disks"])
        # P5（FR-408）：服务级监控探测结果落库（解析与判阈在平台侧，见 services/svc_monitor.py）
        if item.get("service_probes"):
            from app.services import svc_monitor

            try:
                await svc_monitor.ingest_report(db, host.id, item["service_probes"])
            except Exception:  # noqa: BLE001 —— 服务监控失败不得影响指标入库
                log.exception("服务探测结果入库失败 host=%s", host.id)
        # D57：探活项从配置里删掉后，DB 里的旧记录要跟着删（否则详情页永远显示已删的项）
        sent = {p.get("name", "") for p in item.get("probes", [])}
        stale = (await db.execute(select(Probe).where(Probe.host_id == host.id))).scalars().all()
        for row in stale:
            if row.name not in sent:
                await db.delete(row)
    db.add(host)
    await db.commit()


async def upsert_probe(db, host_id: int, p: dict) -> None:
    row = (
        await db.execute(select(Probe).where(Probe.host_id == host_id, Probe.name == p["name"]))
    ).scalar_one_or_none()
    if row is None:
        row = Probe(host_id=host_id, name=p["name"], type=p.get("type", "http"), target=p.get("target", ""))
        db.add(row)
    row.last_status = p.get("status", "unknown")
    row.last_latency_ms = int(p.get("latency_ms", -1))


async def mark_offline_hosts(db, offline_after_s: int = 45) -> int:
    """心跳超时 3×15s 判 offline（FR-B5 验收）。由调度器周期调用。"""
    threshold = datetime.utcnow() - timedelta(seconds=offline_after_s)
    rows = (
        await db.execute(
            select(Host).where(Host.status == "up", Host.last_beat < threshold)
        )
    ).scalars().all()
    for h in rows:
        h.status = "down"
    if rows:
        await db.commit()
    return len(rows)
