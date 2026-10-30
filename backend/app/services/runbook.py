"""修复建议校验（FR-D6；03 §1.15）与 Runbook 生成/执行（FR-E1/E2；03 §1.17/§1.18）。

fixcheck：把诊断报告"建议段"里的每条动作，与预置"验证对"匹配（只读验证命令）；
  有证据 → 标 VALIDATED FIXES（带 impact/risk/rollback/evidence_ref）；无证据 → UNVALIDATED（不进 Runbook）。
runbook：VALIDATED FIXES → 变更单（含前置检查/步骤/回滚/验证），版本化 copy-on-write。
execute：护栏分层 —— 只读分类 → denylist → 白名单（allowlist）→ LLM judge → HITL 审批。
"""
from __future__ import annotations

import json
import logging
import re
import shlex
from datetime import datetime

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.event import Incident, RCAFinding
from app.models.host import Host
from app.models.ops import ActionAudit, Runbook, RunbookVersion
from app.services.findings import _section

log = logging.getLogger("runbook")

# D110：结构化执行动作注册表——AI 只能输出表中的枚举 action，平台据此下发 Agent 任务。
# 渲染出的 cmd 用于展示与 runbook 白名单复核（双层防御）；Agent 端另有枚举校验。
EXEC_ACTIONS: dict[str, dict] = {
    "restart_service":   {"needs": "service", "render": lambda p: f"systemctl restart {p['service']}"},
    "restart_container": {"needs": "service", "render": lambda p: f"docker restart {p['service']}"},
    "reconnect_pool":    {"needs": "service", "render": lambda p: f"systemctl restart {p['service']}"},
    "rotate_logs":       {"needs": "service", "render": lambda p: f"systemctl kill -s HUP {p['service']}"},
    "unlink_nginx_conf": {"needs": "file",    "render": lambda p: f"rm -f /etc/nginx/conf.d/{p['file']}",
                          "guard": r"^[A-Za-z0-9._-]+\.conf$"},
    "unlink_mariadb_cnf":{"needs": "file",    "render": lambda p: f"rm -f /etc/my.cnf.d/{p['file']}",
                          "guard": r"^[A-Za-z0-9._-]+\.cnf$"},
    "nginx_reload_safe": {"needs": None,      "render": lambda p: "nginx -t && nginx -s reload"},
    "nginx_fix_bad_conf":  {"needs": None,    "render": lambda p: "nginx -t || 隔离坏配置; systemctl restart nginx"},
    "mariadb_fix_bad_cnf": {"needs": None,    "render": lambda p: "systemctl restart mariadb（坏配置自动隔离）"},
}


def parse_exec_line(text: str) -> dict | None:
    """解析建议里的结构化 EXEC 行：EXEC: <action> [service=x|file=y]。
    返回 {action, params, cmd} 或 None；未知 action / 参数缺失 / 文件名非法 → None（降级只读）。"""
    # 严格：从 EXEC: 起到**行尾**只允许「动作 [+ 单个参数]」+ 空白。
    # 反例必须整体拒绝：`EXEC: restart_service service=nginx; rm -rf /`（行尾夹带其它命令）
    # EXEC 必须是**独立的一行**：逐行扫描、整行 fullmatch。
    # 允许整行被反引号包裹（LLM 习惯）；行尾夹带其它文字/命令 → 该行不匹配、整体拒绝。
    m = None
    for line in (text or "").splitlines():
        m = re.fullmatch(r"`?\s*EXEC:\s*([a-z_]+)(?:\s+(service|file)=([A-Za-z0-9_.@:\-]+))?\s*`?",
                         line.strip(), re.I)
        if m:
            break
    if not m:
        return None
    action, key, val = m.group(1).lower(), m.group(2), m.group(3)
    spec = EXEC_ACTIONS.get(action)
    if spec is None:
        return None
    need = spec["needs"]
    params = {}
    if need:
        if not key or key.lower() != need or not val:
            return None
        params[need] = val
        if spec.get("guard") and not re.fullmatch(spec["guard"], val):
            return None
    try:
        cmd = spec["render"](params)
    except KeyError:
        return None
    return {"action": action, "params": params, "cmd": cmd}


# 预置"验证对"：建议类型 → 只读验证命令（03 §1.15）
VERIFY_PAIRS = [
    # 顺序即优先级：越具体的建议类型越靠前（"将 CPU 阈值调高"应命中 threshold 而非 cpu）
    {"match": r"阈值|threshold|告警规则", "check": "读取当前规则阈值（规则页/接口）", "kind": "threshold"},
    {"match": r"证书|cert|tls", "check": "openssl s_client -connect <host>:443 -servername <host> </dev/null | openssl x509 -noout -dates", "kind": "cert"},
    {"match": r"日志|log|轮转|rotate", "check": "ls -lh <logdir>", "kind": "log"},
    {"match": r"重启|restart|reload", "check": "systemctl is-active <svc>", "kind": "restart"},
    {"match": r"连接|连接数|connection|pool", "check": "ss -s", "kind": "conn"},
    {"match": r"磁盘|disk|空间|inode", "check": "df -h", "kind": "disk"},
    {"match": r"内存|memory|OOM", "check": "free -m", "kind": "memory"},
    {"match": r"CPU|负载|load", "check": "top -bn1 | head -20", "kind": "cpu"},
    # 探活/health **放最后**：它是"对象词"而非"动作词"，会出现在任何句子里
    # （"重启 mock-service 后复查探活"必须归到 restart，而不能被探活截胡）。
    {"match": r"探活|健康检查|health|probe", "check": "curl -I http://<host>:<port>/health", "kind": "probe"},
]

# 只读命令白名单前缀（护栏第一层：只读分类）——命中即无需审批
# ⚠️ 前缀匹配**必须叠加**写重定向检查（见 _is_readonly）：否则 `cat > /etc/hosts` 会因为
#    "cat" 命中前缀而被判只读，白名单直接变成后门。
READONLY_PREFIXES = ("df", "free", "top", "ps", "uptime", "cat", "tail", "head", "ls", "grep", "ss", "netstat",
                     "systemctl status", "systemctl is-active", "journalctl", "docker ps", "docker logs",
                     "openssl s_client", "curl -I", "du", "vmstat", "iostat", "nproc", "uname",
                     "lsof", "pgrep", "pstree", "sha256sum", "md5sum", "stat", "wc", "hostname", "id",
                     "date")
    # ⚠️ 刻意**不加** "find"：find 可带 -delete/-exec（写操作），命中前缀会变成免审批后门
    # （D161-5：d9 用例用 `find /tmp -type f -mtime +7 -delete` 逮住了这一条）

# 写重定向：`> file` / `2>> file`；只有重定向到 /dev/null 才仍算只读
_WRITE_REDIRECT = re.compile(r"\d?>>?\s*([^\s;&|]+)")
# curl 的变更类参数：显式方法、上传/表单/请求体、落盘输出。带任一即判非只读。
_CURL_MUTATING = re.compile(r"(?:^|\s)-(?:[XTFdDoO])|--(?:data|form|upload-file|request|output|remote-name)\b", re.I)

# denylist（护栏第二层：危险命令一律拒绝，AI 与人都不能越过）
DENYLIST_PATTERNS = [
    r"\brm\s+-rf\s+/(\s|$)", r"\bmkfs\b", r"\bdd\s+if=.*of=/dev/", r"\b:\(\)\{", r"\bshutdown\b", r"\breboot\b",
    r"\bhalt\b", r"\bpoweroff\b", r"\bchmod\s+-R\s+777\s+/", r"\bchown\s+-R\s+.*\s+/\s*$", r">\s*/dev/sd",
    r"\bcurl\b.*\|\s*(ba)?sh", r"\bwget\b.*\|\s*(ba)?sh", r"\bgit\s+push\s+--force\b", r"\bdrop\s+database\b",
    r"\btruncate\b.*\btable\b", r"\biptables\s+-F\b", r"\bkill\s+-9\s+-1\b", r"\buserdel\b",
    # D163-6：`passwd` 只在**命令首词**时拦（改密码动作）；`cat /etc/passwd`、
    # `grep root /etc/passwd` 这类读用户列表是最高频运维操作，此前被 \bpasswd\b 误伤。
    r"^\s*passwd\b",
    # D162-4：任意命令管道进 shell = 在目标机执行远端下发的任意脚本。此前只覆盖
    # curl|wget 管道，`df -h | sh` 这类**白名单命令**接管道仍只被判成"需人工审批"
    # ——审批一次就能在生产机上跑任意代码，与 denylist 的"不可越过"语义不一致。
    # 管道进 shell 一律直接拒绝（denylist），不再给审批留口子。
    r"\|\s*(ba)?sh\b",
]

# ── D173/P2：技能授权命令的**只读形状**校验 ────────────────────────────────
#
# 为什么需要这一层：技能授权（commands）是"精确匹配放行"，若只靠上面的 denylist
# 兜底会留下真实缺口——denylist 只拦 `rm -rf /`（根目录），`rm -rf /tmp/x`
# 这类普通删除并不在其中；一旦技能授权了它，平台侧就会放行一次**写操作**。
#
# 所以技能授权路径额外要求命令满足「只读形状」：
#   1. 不含 shell 组合字符（; | & ` $ ( ) < > 换行）——不能串联、不能重定向、不能替换；
#   2. 首词（可执行名）在**只读工具集**内；
#   3. 个别工具有更严的子命令约束（systemctl/docker/mysql/psql/redis-cli/find/curl）。
# 这三步与「只读前缀白名单」是两条独立路径——前者管"技能能授权什么"，后者管"平台认哪些只读命令"。
SKILL_CMD_METACHARS = re.compile(r"[;|&`$()\n\r<>]")
SKILL_CMD_SAFE_VERBS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "wc", "du", "df", "stat", "nproc",
    "free", "uptime", "top", "ps", "ss", "netstat", "lsof", "pgrep", "pstree", "vmstat", "iostat",
    "hostname", "id", "date", "ping", "traceroute", "dmesg", "lscpu", "lsblk", "journalctl",
    "openssl", "sha256sum", "md5sum", "find", "systemctl", "docker", "mysql", "mysqldump",
    "psql", "redis-cli", "curl",
}
# find 可带 -delete/-exec（写操作）——与只读前缀白名单同理，这里单独禁
_FIND_WRITE_FLAGS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprintf", "-fls")
# systemctl 只允许查询类子命令
_SYSTEMCTL_READONLY = ("status", "is-active", "is-failed", "list-units", "list-unit-files",
                       "show", "cat", "list-timers")
# docker 只允许查询类子命令
_DOCKER_READONLY = ("ps", "logs", "images", "inspect", "stats", "top", "version", "info", "port")
# SQL 只允许读语句
_SQL_READONLY = ("select", "show", "describe", "desc", "explain", "with")
# redis-cli 只允许读命令
_REDIS_READONLY = ("get", "mget", "info", "ttl", "type", "llen", "lrange", "lpos", "scard", "smembers",
                   "hget", "hgetall", "hkeys", "hvals", "zrange", "zcard", "exists", "dbsize",
                   "keys", "scan", "strlen", "getrange", "mget")


def skill_command_allowed(cmd: str) -> tuple[bool, str]:
    """技能授权命令的只读形状校验（D173/P2）。返回 (是否允许, 原因)。

    只在「技能授权路径」上使用——普通只读前缀白名单走 _is_readonly，不受此约束。
    """
    c = (cmd or "").strip()
    if not c:
        return False, "空命令"
    if SKILL_CMD_METACHARS.search(c):
        return False, "命令含 shell 组合字符（; | & ` $ ( ) < >）——技能授权只允许单条无组合的只读命令"
    parts = c.split()
    verb = parts[0]
    if verb not in SKILL_CMD_SAFE_VERBS:
        return False, f"命令首词「{verb}」不在技能授权的只读工具集内"
    low = c.lower()
    if verb == "find" and any(f in low for f in _FIND_WRITE_FLAGS):
        return False, "find 不得带 -delete/-exec 等写操作参数"
    if verb == "systemctl" and not any(low.startswith(f"systemctl {s}") for s in _SYSTEMCTL_READONLY):
        return False, "systemctl 只允许查询类子命令（status/is-active/list-units/show…）"
    if verb == "docker" and not any(low.startswith(f"docker {s}") for s in _DOCKER_READONLY):
        return False, "docker 只允许查询类子命令（ps/logs/images/inspect/stats…）"
    if verb in ("mysql", "mysqldump", "psql"):
        m = re.search(r"-e\s+(['\"])(.*?)\1", c, re.S) or re.search(r"-c\s+(['\"])(.*?)\1", c, re.S)
        if m:
            stmt = m.group(2).strip().lower()
            if not stmt.startswith(_SQL_READONLY):
                return False, "SQL 只允许只读语句（SELECT/SHOW/DESCRIBE/EXPLAIN）"
    if verb == "redis-cli" and len(parts) >= 2 and parts[1].lower() not in _REDIS_READONLY:
        return False, "redis-cli 只允许读命令"
    if verb == "curl" and _CURL_MUTATING.search(c):
        return False, "curl 只允许 GET 类请求"
    return True, "只读形状校验通过"



# ---------------- 动作清单（FR-E2「脚本级白名单」）----------------
# 语义：**只有命中本清单的动作**才允许在 auto 模式（白名单自动）下免确认执行。
# 不命中 → 即使开了 auto 也走人工确认（"白名单自动"不等于"全部自动"）。
# 匹配的是"动作命令"，用锚定正则；清单可用环境变量 ACTION_ALLOWLIST 覆盖（分号分隔）。
DEFAULT_ACTION_ALLOWLIST: list[dict] = [
    {"id": "cleanup-tmp", "desc": "清理 7 天前的临时文件",
     "pattern": r"^find\s+/tmp\b.*-mtime\s+\+7.*-delete$"},
    {"id": "restart-nginx", "desc": "重启 nginx", "pattern": r"^systemctl\s+restart\s+nginx(\.service)?$"},
    {"id": "restart-mariadb", "desc": "重启 mariadb", "pattern": r"^systemctl\s+restart\s+mariadb(\.service)?$"},
    {"id": "nginx-fix-bad-conf", "desc": "nginx 配置急救（坏配置备份后重启）",
     "pattern": r"^nginx\s+-t\s+\|\|\s+隔离坏配置;\s+systemctl\s+restart\s+nginx$"},
    {"id": "mariadb-fix-bad-cnf", "desc": "mariadb 配置急救（坏配置备份后重启）",
     "pattern": r"^systemctl\s+restart\s+mariadb（坏配置自动隔离）$"},
    {"id": "restart-redis", "desc": "重启 redis", "pattern": r"^systemctl\s+restart\s+redis(-server)?(\.service)?$"},
    {"id": "logrotate", "desc": "手动触发日志轮转（不删数据）", "pattern": r"^logrotate\s+-f\s+/etc/logrotate\.conf$"},
    {"id": "drop-caches-safe", "desc": "释放 page cache（生产慎用，仅列白名单示例）",
     "pattern": r"^sync\s*&&\s*echo\s+1\s*>\s*/proc/sys/vm/drop_caches$"},
    {"id": "restart-app-template", "desc": "重启受管业务服务（示例：占位由运维按需改）",
     "pattern": r"^systemctl\s+restart\s+<your-service>$"},
    # L3/L4 自动恢复：配错文件 → 安全删配置（路径白名单 + 删前自动备份到 /var/backups/opsagent/）
    {"id": "unlink-nginx-conf", "desc": "删除 /etc/nginx/conf.d/ 下的错误配置（已自动备份）",
     "pattern": r"^rm\s+(-f\s+)?/etc/nginx/conf\.d/[A-Za-z0-9._-]+\.conf$"},
    {"id": "unlink-mariadb-cnf", "desc": "删除 /etc/my.cnf.d/ 下的错误配置（已自动备份）",
     "pattern": r"^rm\s+(-f\s+)?/etc/my\.cnf\.d/[A-Za-z0-9._-]+\.cnf$"},
    {"id": "nginx-reload-safe", "desc": "先 nginx -t 语法检查再 reload（仅在语法通过时执行）",
     "pattern": r"^nginx\s+-t\s*&&\s*nginx\s+-s\s+reload$"},
]


def derive_exec_actions(findings_md: str) -> list[dict]:
    """D110 兜底：AI 未按格式输出 EXEC 行时，把"已验证的重启/删配置建议"确定性地翻译成白名单动作。

    仅做**保守映射**（宁可少做不可做错）：
      - 文本里出现「重启/拉起 <unit>」且 unit ∈ {nginx, mariadb/mysql, redis/redis-server} → restart_service
      - 文本里出现「删除 <具体文件名>.conf（/etc/nginx/conf.d/）」→ unlink_nginx_conf
      - 文本里出现「删除 <具体文件名>.cnf（/etc/my.cnf.d/）」→ unlink_mariadb_cnf
      - 文本里出现 `nginx -t && nginx -s reload` → nginx_reload_safe
    返回 [{action, params, cmd, source:"derived"}]；去重；最多 4 个。
    """
    text = findings_md or ""
    out: list[dict] = []
    seen: set = set()
    # P1-8：只有**报告里确实出现配置错误证据**时才把 restart 升级成“配置急救”。
    # 旧实现无条件升级：崩溃/僵死（没有坏配置）也会去隔离 .cnf/.conf——L2b 实测
    # 生成了错误的 mariadb_fix_bad_cnf。证据不足时保留最小动作 restart_service。
    nginx_cfg_evidence = bool(re.search(
        r"nginx\s+-t|unknown directive|/etc/nginx/conf\.d/|nginx.{0,8}配置(错误|损坏)|坏配置", text, re.I))
    mariadb_cfg_evidence = bool(re.search(
        r"unknown variable|/etc/my\.cnf\.d/|(mariadb|mysql).{0,8}配置(错误|损坏)|坏配置", text, re.I))

    def add(action: str, params: dict, reason: str):
        if action in ("restart_service", "reconnect_pool") and params.get("service"):
            params = dict(params)
            params["service"] = {"mysql": "mariadb", "mysqld": "mariadb"}.get(params["service"], params["service"])
        # 对 nginx/mariadb 的"重启"意图升级为配置急救动作：先校验/隔离坏配置再重启（幂等且安全）。
        # 但必须已有该组件的配置错误证据；否则崩溃/僵死的标准动作就是 restart。
        if action == "restart_service" and params.get("service") == "nginx" and nginx_cfg_evidence:
            action, params = "nginx_fix_bad_conf", {}
        elif action == "restart_service" and params.get("service") == "mariadb" and mariadb_cfg_evidence:
            action, params = "mariadb_fix_bad_cnf", {}
        key = (action, tuple(sorted(params.items())))
        if key in seen or len(out) >= 4:
            return
        seen.add(key)
        try:
            cmd = EXEC_ACTIONS[action]["render"](params)
        except Exception:
            return
        out.append({"action": action, "params": params, "cmd": cmd, "source": "derived", "reason": reason})

    # ① 内联 EXEC / API 风格动作（AI 常把 EXEC 写进句子里或带 service= 前缀）：
    #    "EXEC: restart_service service=nginx" / "restart_service service=nginx"
    # 参数后必须是「边界」：空白/行尾/反引号/中文标点——否则（如 bad;rm）整条不匹配。
    # 注意不能只用负向前瞻：{1,64} 会回溯成 bad→ba 绕过检查（实测踩到）。
    for m in re.finditer(
            r"(?:EXEC:\s*)?([a-z_]+)\s+(service|file)=([A-Za-z0-9_.@:\-]{1,64})"
            r"(?=\s|$|[`\"')\uff0c\u3002\uff1b\u3001])", text):
        act, key, val = m.group(1), m.group(2), m.group(3)
        if act in EXEC_ACTIONS and EXEC_ACTIONS[act]["needs"] == key:
            add(act, {key: val}, f"内联动作引用 {act} {key}={val}")
    # ② 自然语言「重启 X」
    # 服务重启：只认三个已知 unit（避免把报告里的任意英文词当服务名）
    unit_map = {"nginx": "nginx", "mariadb": "mariadb", "mysql": "mariadb",
                "redis": "redis", "redis-server": "redis"}
    for m in re.finditer(r"(?:重启|拉起|重起|restart|start)\s*[`\"']?\s*([A-Za-z][A-Za-z0-9_.@-]{1,31})", text):
        raw = m.group(1).lower().rstrip(".")
        unit = unit_map.get(raw)
        if unit:
            add("restart_service", {"service": unit}, f"文本建议重启 {raw}")
    # nginx 配置删除（必须带具体文件名）
    for m in re.finditer(r"/etc/nginx/conf\.d/([A-Za-z0-9._-]+\.conf)", text):
        add("unlink_nginx_conf", {"file": m.group(1)}, f"文本引用配置文件 {m.group(1)}")
    for m in re.finditer(r"/etc/my\.cnf\.d/([A-Za-z0-9._-]+\.cnf)", text):
        add("unlink_mariadb_cnf", {"file": m.group(1)}, f"文本引用配置文件 {m.group(1)}")
    if "nginx -t && nginx -s reload" in text:
        add("nginx_reload_safe", {}, "文本建议安全重载 nginx")
    return out


def action_allowlist() -> list[dict]:
    """有效动作清单：环境变量覆盖 > 内置。"""
    import os

    raw = (os.getenv("ACTION_ALLOWLIST") or "").strip()
    if not raw:
        return DEFAULT_ACTION_ALLOWLIST
    out = []
    for i, item in enumerate(raw.split(";")):
        item = item.strip()
        if not item:
            continue
        pid, _, pat = item.partition("=")
        out.append({"id": (pid or f"custom-{i}").strip(), "desc": "自定义（环境变量）",
                    "pattern": (pat or pid).strip()})
    return out or DEFAULT_ACTION_ALLOWLIST


def match_action_allowlist(cmd: str) -> dict | None:
    """命令是否命中动作清单。返回命中的条目（含 id/desc）或 None。"""
    c = (cmd or "").strip()
    for item in action_allowlist():
        try:
            if re.search(item["pattern"], c, re.I):
                return item
        except re.error:  # 坏正则只跳过该条，不影响其它
            continue
    return None


class GuardResult:
    def __init__(self, allowed: bool, verdict: str, reason: str = "", need_hitl: bool = False):
        self.allowed, self.verdict, self.reason, self.need_hitl = allowed, verdict, reason, need_hitl


def _is_readonly(c: str) -> bool:
    """只读分类（护栏第一层）。判定**保守**：拿不准就判非只读 → 走人工审批。

    - 前缀命中白名单 **且** 无写重定向：`cat > /etc/hosts` 这种写法会绕过纯前缀匹配，
      所以前缀白名单必须叠加 _WRITE_REDIRECT 检查。
    - 或 GET-only curl（无 -X/-d/-F/-T/-o/-O 等变更类参数）。这是唯一的"形状推断"，
      因为 `curl -I <health>` 这类探活命令若判成需审批，最常见的验证步骤就永远放行不了。
      代价：一个"用 GET 触发变更"的畸形服务会被误判为只读——护栏第二层的 denylist 与
      人工审批仍可拦截，且平台 V1 本就不代执行远程命令。
    """
    if any(m.group(1) != "/dev/null" for m in _WRITE_REDIRECT.finditer(c)):
        return False
    # D161-6/D162-4：管道进 shell = 实际执行任意代码，不是只读（`df -h | sh` 这类
    # 此前靠 "df" 前缀蒙混过关 → 免审批后门）。denylist 已覆盖（管道进 shell 直接拒绝），
    # 这里保留为**纵深防御**：即便 denylist 被改松，这一层仍判非只读。
    if re.search(r"\|\s*(ba)?sh\b", c, re.I):
        return False
    if any(c.startswith(p) for p in READONLY_PREFIXES):
        return True
    return bool(re.match(r"^curl\b", c, re.I)) and not _CURL_MUTATING.search(c)


def step_command(step: dict) -> str:
    """执行/护栏使用的命令来源：cmd 优先，其次 check，最后 action。

    check 是**验证命令**，不能当执行命令用。L2 实机踩坑：步骤同时有
    cmd="systemctl restart nginx" 与 check="systemctl is-active nginx" 时，
    旧代码优先取 check → 被 _is_readonly 判为只读 → 直接返回成功但从未下发 Agent。
    """
    return str(step.get("cmd") or step.get("check") or step.get("action") or "").strip()


def guard_command(cmd: str) -> GuardResult:
    """护栏分层前两层的确定性判定：denylist > 只读分类 > 需审批。

    （第三层 LLM judge 与第四层 HITL 在 execute 里按需叠加。）
    """
    c = (cmd or "").strip()
    if not c:
        return GuardResult(False, "empty", "空命令")
    for pat in DENYLIST_PATTERNS:
        if re.search(pat, c, re.I):  # 忽略大小写：DROP DATABASE / Drop Database 都要拦
            return GuardResult(False, "denylist", f"命中禁用模式 {pat}")
    if _is_readonly(c):
        return GuardResult(True, "allowlist", "只读命令，直接放行")
    return GuardResult(True, "hitl", "非只读命令：需人工审批后执行", need_hitl=True)


# 显式建议段的候选标题（模型须用其中之一；见 prompts 输出契约）
# 规范名为 `验证过的修复动作`；其余为兼容别名（历史报告、模型偶发用词）
FIX_SECTIONS = ("验证过的修复动作", "VALIDATED FIXES", "VALIDATED FIXES:", "Recommended fix", "Recommended fixes",
                "Remediation", "Fix", "修复建议", "建议处置", "处置建议")


def has_fixes_section(findings_md: str) -> bool:
    """报告里是否存在**显式建议段**（VALIDATED FIXES 及兼容别名）。

    前端据此决定"生成变更单"按钮是否可用 —— 让用户点了必然 400 是坏体验；
    判定逻辑与 fixcheck 共用同一份 FIX_SECTIONS，避免两处漂移。
    """
    return any(_section(findings_md or "", name) for name in FIX_SECTIONS)


def fixcheck_findings(findings_md: str) -> dict:
    """从报告的**显式建议段**抽取条目并做验证匹配。

    返回 {validated, unvalidated, fixes_md, note}。

    设计要点（D7 实测修正）：**不做 Summary 兜底**。此前若报告没有建议段，就退化成从 Summary 抓
    项目符号，结果把"故障事实（可证实）：…"这类**事实陈述**当成修复建议去匹配预置验证对，产出
    看似合规但内容无意义的变更单——这违背 FR-D6"建议必须经过验证"的本意。没有建议段就返回空，
    由调用方拒绝生成变更单（引导用户重新诊断或补充建议）。
    """
    body = findings_md or ""
    section = next((s for name in FIX_SECTIONS if (s := _section(body, name))), "")
    if not section:
        return {"validated": [], "unvalidated": [], "fixes_md": "",
                "note": "报告中没有「## 验证过的修复动作」建议段——无可验证的建议，不生成变更单。"}

    bullets = [re.sub(r"^[-*\d.\s]+", "", l).strip()
               for l in section.splitlines() if l.strip().startswith(("-", "*")) or re.match(r"^\s*\d+[.)]", l)]
    bullets = [b for b in bullets if len(b) > 6][:20]

    validated, unvalidated = [], []
    for b in bullets:
        ro = _readonly_command_in(b)
        hit = next((p for p in VERIFY_PAIRS if re.search(p["match"], b, re.I)), None)
        if ro:
            # 只读建议**本身就构成验证**（执行它不改变任何状态，也就无所谓"猜错动作"），
            # 因此不要求它再命中 VERIFY_PAIRS 的预置验证对。
            # D9 实测教训：报告里最标准的取证命令是 curl -v / lsof / tail 日志，它们既不匹配
            # 预置验证对、又不含 _ACTION_HINTS 里的动词，于是整份报告被判 NO_VALIDATED_FIX，
            # 变更单一步都生成不出来——"防幻觉"防线反过来把正常流程堵死了。
            validated.append({
                "action": b[:300], "kind": "readonly", "evidence_ref": ro,
                "impact": "只读取证：不改变目标机状态，执行后回填输出", "rollback": _rollback_for("readonly"),
                "risk": _risk_for("readonly"),
            })
        elif hit and _looks_like_action(b):
            item = {
                "action": b[:300], "kind": hit["kind"], "evidence_ref": hit["check"],
                "impact": "按建议执行后需复查该项", "rollback": _rollback_for(hit["kind"]),
                "risk": _risk_for(hit["kind"]),
            }
            ex = parse_exec_line(b)          # D110：结构化执行意图（没有也不能生成变更单外的动作）
            if ex:
                item["exec"] = ex
                item["kind"] = ex["action"]
                item["execution"] = "agent"
            validated.append(item)
        elif hit:
            unvalidated.append({"action": b[:300],
                                "reason": "命中验证对，但句子不像可执行动作（疑似结论/事实陈述）"})
        else:
            unvalidated.append({"action": b[:300], "reason": "未匹配到预置验证对，缺少可核对的证据引用"})
    fixes_md = ""
    if validated:
        fixes_md = "## 验证过的修复动作\n\n" + "\n".join(
            f"- **{v['action']}**\n  - 验证依据：`{v['evidence_ref']}`\n  - 影响：{v['impact']}\n"
            f"  - 风险：{v['risk']}　回滚：{v['rollback']}" for v in validated)
    if unvalidated:
        fixes_md += "\n\n## 未验证（不进变更单）\n\n" + "\n".join(
            f"- {u['action']}（{u['reason']}）" for u in unvalidated)
    return {"validated": validated, "unvalidated": unvalidated, "fixes_md": fixes_md, "note": ""}


# 动作性判定：建议应以动词/祈使开头（中英），避免把"XX 是原因"这类陈述当建议
_ACTION_HINTS = ("清理", "删除", "重启", "扩容", "调整", "开启", "关闭", "修复", "回滚", "增加", "降低", "提升",
                 "检查", "验证", "迁移", "轮转", "限流", "隔离", "替换", "重建", "优化", "排查", "执行", "定位",
                 "clean", "remove", "delete", "restart", "scale", "tune", "enable", "disable", "fix",
                 "rollback", "increase", "decrease", "check", "verify", "rotate", "throttle")

# 建议句里的命令片段（模型习惯写反引号）
_CMD_RE = re.compile(r"`([^`]+)`")


def _readonly_command_in(text: str) -> str:
    """从建议句里抠出**只读命令**；抠不出（或抠出来的不是只读）就返回空串。

    先用 guard_command 复判一次，而不是自己维护第二份白名单——护栏只有一处定义，
    变更单的可执行性与 execute 时的判定必须永远同源（否则会生成"平台认为可执行、
    执行时又被拦"的变更单）。
    """
    cands = [m.group(1).strip() for m in _CMD_RE.finditer(text)]
    cands.append(text.strip().lstrip("-*# ").strip())
    for c in cands:
        if not c or " " not in c:      # 至少要"命令 参数"，排除 `disk.percent` 这类标识符
            continue
        g = guard_command(c)
        if g.allowed and not g.need_hitl:
            return c
    return ""


def _looks_like_action(text: str) -> bool:
    """整句**任意位置**含动作词即算建议——不要限定"必须动词开头"。

    D7 实测教训：模型很自然地把建议写成"若确认进程不存在需立即恢复业务：在主机上重启 mock-service
    （如 systemctl restart …）"，语义上明确是"重启服务"（且能匹配到 restart 验证对），
    但"必须动词开头"的启发式会把它误判成陈述句而拒收——真正该拒的是"XX 是原因"这类**无动作词**的句子。
    """
    t = text.strip().lstrip("-*# ").strip()
    return any(h in t for h in _ACTION_HINTS)


def _risk_for(kind: str) -> str:
    return {"restart": "mid", "threshold": "low", "disk": "mid", "log": "low", "memory": "mid",
            "cpu": "low", "conn": "low", "cert": "low", "probe": "low", "readonly": "low"}.get(kind, "mid")


def _rollback_for(kind: str) -> str:
    return {
        "restart": "若启动失败：保留原配置回滚（git checkout / 备份文件），再观察错误日志",
        "threshold": "改回原阈值（原值记录在变更单 params 中）",
        "disk": "删除临时文件前先备份目录清单；必要时从备份恢复",
        "log": "轮转/清理前确认非审计必需日志；保留最近备份",
        "memory": "若调参后不稳定：恢复原配置并重启服务",
        "readonly": "只读取证命令，无副作用（不需回滚）",
        "probe": "只读探活，无副作用（不需回滚）",
    }.get(kind, "变更前备份配置，异常时按备份回滚")


# ---------------- Runbook ----------------
async def generate_runbook(incident_id: int) -> dict:
    """从事件的最新诊断报告生成变更单（含版本快照）。"""
    async with SessionLocal() as db:
        inc = await db.get(Incident, incident_id)
        if inc is None:
            return {"ok": False, "msg": "INCIDENT_NOT_FOUND"}
        f = (await db.execute(select(RCAFinding).where(RCAFinding.incident_id == incident_id)
                              .order_by(RCAFinding.id.desc()))).scalars().first()
        if f is None or not f.findings_md:
            return {"ok": False, "msg": "NO_FINDINGS：请先执行诊断"}

        fc = fixcheck_findings(f.findings_md)
        derived_steps: list[dict] = []
        if not fc["validated"]:
            # D111 兜底：AI 没给可执行建议（如 INCONCLUSIVE）时，按**服务声明 down + unit** 推导重启。
            # 证据门槛：服务在声明里确实 down 且填了 systemd unit——不是凭空猜动作。
            from app.models.svc import Service

            svcs = (await db.execute(select(Service).where(
                Service.host_id == (await db.execute(select(Host).where(Host.ident == inc.host_ident))).scalar_one().id,
                Service.enabled.is_(True), Service.status == "down"))).scalars().all()
            for svc in svcs[:2]:
                unit = (svc.spec_json or {}).get("unit") or ""
                if unit and re.fullmatch(r"[A-Za-z0-9_.@:\-]{1,64}", unit):
                    cmd = f"systemctl restart {unit}"
                    derived_steps.append({"idx": len(derived_steps) + 1,
                                          "desc": f"[平台规则] 服务 {svc.name} 声明为 down → {cmd}",
                                          "check": f"服务声明 {svc.name} status=down，spec.unit={unit}",
                                          "mode": "approval", "action": "restart_service",
                                          "params": {"service": unit}, "cmd": cmd,
                                          "rollback": "systemctl start 拉回；若起不来查看 systemctl status 报错"})
            if not derived_steps:
                # 第二兜底：AI 报告正文里明确写了「重启 nginx」这类字眼 → 确定性翻译
                for d in derive_exec_actions(f.findings_md or ""):
                    derived_steps.append({"idx": len(derived_steps) + 1,
                                          "desc": f"[平台规则] {d['reason']} → {d['cmd']}",
                                          "check": "（平台规则翻译：报告未按 VALIDATED FIX 格式输出，"
                                                   "按正文建议与证据推导）",
                                          "mode": "approval", "action": d["action"],
                                          "params": d["params"], "cmd": d["cmd"],
                                          "rollback": "按备份路径回滚或 systemctl start 拉回"})
            if not derived_steps:
                return {"ok": False,
                        "msg": "NO_VALIDATED_FIX：" + (fc.get("note") or "报告中没有可通过验证的建议（不生成变更单）"),
                        "data": {"unvalidated": fc["unvalidated"]}}

        steps = []
        for i, v in enumerate(fc["validated"], 1):
            st = {"idx": i, "desc": v["action"], "check": v["evidence_ref"],
                  "mode": "readonly" if v["risk"] == "low" else "approval",
                  "rollback": v["rollback"]}
            if v.get("exec"):
                ex = v["exec"]
                st["action"] = ex["action"]          # 枚举动作（平台 → Agent 任务）
                st["params"] = ex["params"]
                st["cmd"] = ex["cmd"]                # 渲染命令（审计/展示/白名单复核）
            steps.append(st)

        # D110 兜底：AI 没给 EXEC 行时，用确定性翻译器从已验证建议 + 证据里提取动作。
        # 只补**非只读**步骤对应的动作，且必须能在同一份报告里找到「重启/删配置」字样。
        if not any(s.get("action") for s in steps) and derived_steps:
            steps.extend(derived_steps)
        elif not any(s.get("action") for s in steps):
            _derive_src = (fc.get("fixes_md") or "") + "\n" + (f.findings_md or "")
            for d in derive_exec_actions(_derive_src):
                steps.append({"idx": len(steps) + 1,
                              "desc": f"[平台规则] {d['reason']} → {d['cmd']}",
                              "check": "（平台规则翻译：AI 未按 EXEC 格式输出，按已验证建议与证据推导）",
                              "mode": "approval", "action": d["action"], "params": d["params"],
                              "cmd": d["cmd"], "rollback": "按备份路径回滚或 systemctl start 拉回"})
        rb = Runbook(
            name=f"处置：{inc.title[:60]}", incident_id=incident_id, host_ident=inc.host_ident,
            risk_assessment=f"最高风险等级 {max([v['risk'] for v in fc['validated']] or ['mid'], key=lambda r: {'low': 0, 'mid': 1, 'high': 2}[r])}；"
                            f"共 {len(steps)} 步，其中只读 {sum(1 for s in steps if s['mode'] == 'readonly')} 步需审批 "
                            f"{sum(1 for s in steps if s['mode'] != 'readonly')} 步",
            pre_checks=[{"desc": "确认事件仍处于活跃状态", "check": "GET /api/ops/alerts?host=" + inc.host_ident},
                        {"desc": "确认最近 10 分钟无新增同级告警", "check": "GET /api/ops/alerts"}],
            steps=steps,
            rollback_plan=("；".join({v["rollback"] for v in fc["validated"]}) or
                           "按备份路径回滚（/var/backups/opsagent/）或 systemctl start 拉回")[:2000],
            verification="执行后复查：" + "；".join({v["evidence_ref"] for v in fc["validated"]})[:1000],
            risk_level=max([v["risk"] for v in fc["validated"]] or ["mid"],
                           key=lambda r: {"low": 0, "mid": 1, "high": 2}[r]),
            whitelist_enabled=False, version=1, status="draft")
        db.add(rb)
        await db.commit()
        await db.refresh(rb)
        db.add(RunbookVersion(runbook_id=rb.id, version=1, snapshot_json=_snapshot(rb),
                              summary=f"初版（来自事件 #{incident_id} 的诊断）"))
        await db.commit()
        # D113：含可执行步骤的 manual 变更单 → 主动推送"待确认"（不等人刷页面）
        try:
            if "action" in json.dumps(steps, ensure_ascii=False):
                from app.services.notify import notify_attention
                async with SessionLocal() as ndb:
                    actions = [s.get("action") for s in steps
                               if isinstance(s, dict) and s.get("action")][:4]
                    await notify_attention(
                        ndb, "hitl_pending",
                        f"变更单 #{rb.id} 待确认",
                        f"变更单 #{rb.id} 已生成 {len(steps)} 个可执行步骤，等待你确认后再由 Agent 执行。",
                        meta={"runbook_id": rb.id, "incident_id": rb.incident_id,
                              "host": rb.host_ident, "steps": len(steps),
                              "risk": rb.risk_level, "actions": actions})
        except Exception:  # noqa: BLE001 —— 通知失败不影响变更单生成
            log.exception("待确认通知发送失败（不影响主流程）")
        return {"ok": True, "runbook_id": rb.id, "fixes": fc["fixes_md"], "validated": fc["validated"],
                "unvalidated": fc["unvalidated"]}


def _snapshot(rb: Runbook) -> dict:
    return {"name": rb.name, "risk_level": rb.risk_level, "risk_assessment": rb.risk_assessment,
            "pre_checks": rb.pre_checks, "steps": rb.steps, "rollback_plan": rb.rollback_plan,
            "verification": rb.verification, "whitelist_enabled": rb.whitelist_enabled}


async def update_runbook(runbook_id: int, patch: dict, summary: str = "") -> dict:
    """更新变更单并落新版本快照（copy-on-write：旧版本只读保留）。"""
    async with SessionLocal() as db:
        rb = await db.get(Runbook, runbook_id)
        if rb is None:
            return {"ok": False, "msg": "NOT_FOUND"}
        for k in ("name", "steps", "pre_checks", "rollback_plan", "verification", "risk_level",
                  "risk_assessment", "whitelist_enabled", "execution_mode", "status"):
            if k in patch and patch[k] is not None:
                setattr(rb, k, patch[k])
        rb.version += 1
        rb.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(rb)
        db.add(RunbookVersion(runbook_id=rb.id, version=rb.version, snapshot_json=_snapshot(rb),
                              summary=summary or f"更新到 v{rb.version}"))
        await db.commit()
        return {"ok": True, "runbook_id": rb.id, "version": rb.version}


CONFIRM_TTL_S = 300   # 审批凭据有效期（"看完确认卡→点执行"的合理窗口）


def _exec_mode(rb) -> str:
    """变更单的执行模式（suggest|manual|auto），兼容旧的 whitelist_enabled。"""
    m = (getattr(rb, "execution_mode", "") or "").strip()
    if m in ("suggest", "manual", "auto"):
        return m
    return "auto" if getattr(rb, "whitelist_enabled", False) else "manual"


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256((token or "").encode()).hexdigest()


async def issue_confirm(runbook_id: int, step_idx: int, operator: str = "user", ip: str = "") -> dict:
    """为"执行某一步"签发一次性审批凭据（FR-E2 HITL 的可追溯落地）。

    `approved=true` 由客户端自报时，审计证明不了"有人审过"（curl 就能过）。
    现在确认动作先落一条审批记录（操作者/时间/IP/命令快照），执行时必须出示 token：
    审计里的"已批准"有出处，且不可重放（用后即焚 + TTL）。
    """
    import secrets
    from datetime import timedelta

    from app.models.ops import ActionApproval

    async with SessionLocal() as db:
        rb = await db.get(Runbook, runbook_id)
        if rb is None:
            return {"ok": False, "msg": "NOT_FOUND"}
        step = next((x for x in (rb.steps or []) if int(x.get("idx", 0)) == int(step_idx)), None)
        if step is None:
            return {"ok": False, "msg": "STEP_NOT_FOUND"}
        cmd = step_command(step)
        g = guard_command(cmd)
        if not g.allowed:
            return {"ok": False, "msg": f"GUARDRAIL_BLOCKED({g.verdict})：{g.reason}"}
        if not g.need_hitl:
            return {"ok": False, "msg": "只读步骤无需确认，可直接执行"}
        mode = _exec_mode(rb)
        if mode == "suggest":
            return {"ok": False, "msg": "SUGGEST_ONLY：本变更单为「仅建议」模式，请人工在目标机执行"}

        token = "cfm-" + secrets.token_hex(16)
        expires = datetime.utcnow() + timedelta(seconds=CONFIRM_TTL_S)
        db.add(ActionApproval(runbook_id=runbook_id, step_idx=int(step_idx), operator=operator,
                              token_hash=_hash_token(token), ip=ip, mode="manual",
                              snapshot={"cmd": cmd, "desc": step.get("desc"),
                                        "risk": step.get("risk") or rb.risk_level},
                              expires_at=expires))
        await db.commit()
        hit = match_action_allowlist(cmd)
        return {"ok": True, "confirm_token": token, "expires_at": expires.isoformat() + "Z",
                "ttl_s": CONFIRM_TTL_S, "mode": mode,
                "allowlisted": bool(hit), "allowlist_id": (hit or {}).get("id", ""),
                "step_idx": int(step_idx), "cmd": cmd, "desc": step.get("desc"),
                "risk_level": rb.risk_level, "host_ident": rb.host_ident,
                "rollback": step.get("rollback") or rb.rollback_plan,
                "note": "凭据一次性有效；请在目标机执行返回的命令并回填结果"}


async def _consume_confirm(db, runbook_id: int, step_idx: int, token: str):
    """校验审批凭据（未用 + 未过期 + 同一 runbook/step）。返回记录或 None。"""
    if not token:
        return None
    from app.models.ops import ActionApproval

    row = (await db.execute(select(ActionApproval).where(
        ActionApproval.token_hash == _hash_token(token),
        ActionApproval.runbook_id == runbook_id,
        ActionApproval.step_idx == int(step_idx),
    ))).scalars().first()
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        return None
    return row


async def execute_step(runbook_id: int, step_idx: int, operator: str = "user", approved: bool = False,
                       confirm_token: str = "", operator_name: str = "") -> dict:
    """执行单步。护栏分层：denylist → 只读分类 → 动作清单（auto 模式）→ HITL 凭据。

    结构化动作（step.action）在审批通过后通过 Agent 任务通道真实下发执行；
    没有结构化动作的原始命令仍只生成待执行指令（V1 不执行任意 shell）。"执行"的语义是：
      ① 记录审计与护栏判定（含审批人/审批方式）；
      ② 只读步骤直接放行并返回其验证命令；
      ③ 非只读步骤按变更单 execution_mode 决定：
         suggest → 拒绝；auto 且命中动作清单 → 免确认（approval_mode=auto）；
         其余 → 必须出示一次性 confirm_token（approval_mode=manual）后才下发/放行。

    `approved` 参数保留是为了兼容老客户端，**不再作为放行依据**（客户端自报不可信）。
    """
    async with SessionLocal() as db:
        rb = await db.get(Runbook, runbook_id)
        if rb is None:
            return {"ok": False, "msg": "NOT_FOUND"}
        step = next((s for s in (rb.steps or []) if int(s.get("idx", 0)) == int(step_idx)), None)
        if step is None:
            return {"ok": False, "msg": "STEP_NOT_FOUND"}
        cmd = step_command(step)
        mode = _exec_mode(rb)
        who = operator_name or operator
        g = guard_command(cmd)

        def _audit(status: str, result: str, verdict: str, approval_mode: str = "",
                   by: str = "", at: datetime | None = None):
            db.add(ActionAudit(runbook_id=runbook_id, host_ident=rb.host_ident, action=f"step{step_idx}",
                               params={**step, "execution_mode": mode}, result=result, operator=operator,
                               status=status, guardrail_verdict=verdict, approval_mode=approval_mode,
                               approved_by=by, approved_at=at))

        if not g.allowed:                                     # denylist
            _audit("aborted", g.reason, g.verdict)
            await db.commit()
            return {"ok": False, "msg": f"GUARDRAIL_BLOCKED({g.verdict})：{g.reason}"}

        if not g.need_hitl:                                   # 只读：无需确认
            _audit("success", "只读步骤放行（无需审批）", g.verdict, approval_mode="readonly")
            rb.status = "executing"
            await db.commit()
            return {"ok": True, "guardrail": g.verdict, "approval_mode": "readonly", "host": rb.host_ident,
                    "execute_on_target": cmd, "desc": step.get("desc"), "execution_mode": mode,
                    "note": "V1 无远程命令通道：请在目标机执行该命令并回填结果（操作已入审计）"}

        if mode == "suggest":                                 # 仅建议：不出执行指令
            _audit("aborted", "仅建议模式：平台不出具可执行命令，请人工在目标机执行", "suggest_only")
            await db.commit()
            return {"ok": False, "msg": "SUGGEST_ONLY：本变更单为「仅建议」模式（只给建议，不生成执行指令）。"
                                        "需要执行请把执行方式改为「人工确认」或「白名单自动」"}

        hit = match_action_allowlist(cmd) if mode == "auto" else None

        # D110/D113：结构化动作 → 审批通过后真实下发 Agent 任务。
        # 顺序必须是「先校验审批，再下发」：此前 action 分支在 _consume_confirm 之前，
        # 只要知道 runbook/step 就能绕过人工确认直接执行（全流程演练发现的护栏缺口）。
        if step.get("action"):
            auto_ok = bool(mode == "auto" and hit)
            if auto_ok:
                _audit("success", f"白名单自动放行（命中动作 {hit['id']}）", "auto",
                       approval_mode="auto", by=who, at=datetime.utcnow())
            else:
                rec = await _consume_confirm(db, runbook_id, step_idx, confirm_token)
                if rec is None:
                    hint = "该动作未命中动作清单，需人工确认" if mode == "auto" else "需人工确认"
                    _audit("started", f"{hint}：未提供有效审批凭据", "hitl")
                    await db.commit()
                    return {"ok": False, "msg": f"HITL_REQUIRED：{hint}——请先调用 "
                                                f"POST /api/ops/runbooks/{runbook_id}/steps/{step_idx}/confirm "
                                                f"取得一次性确认凭据后再执行",
                            "data": {"guardrail": "hitl", "step": step, "execution_mode": mode,
                                     "allowlisted": bool(hit), "approved_param_ignored": bool(approved)}}
                rec.used_at = datetime.utcnow()
                _audit("success", f"人工确认放行（确认人 {rec.operator}）", "hitl",
                       approval_mode="manual", by=rec.operator, at=rec.created_at)

            from app.services.executor import enqueue_and_wait

            rb.status = "executing"
            await db.commit()
            res = await enqueue_and_wait(rb.host_ident, step["action"], step.get("params") or {},
                                         timeout_s=90)
            agent_result = res.get("result") or {}
            post = agent_result.get("post_check") or {}
            post_ok = post.get("ok") if isinstance(post, dict) else None
            # P1-9：动作执行成功 ≠ 服务恢复。post_check 未通过时要求回滚/转人工，
            # 不把“命令 rc=0”当成功（L2 演练里出现过 restart 成功但端口仍未起）。
            rollback_required = post_ok is False
            rollback_hint = ""
            if rollback_required:
                backup = str(agent_result.get("backup") or "")
                src = str(agent_result.get("src") or "")
                if backup and src:
                    rollback_hint = f"恢复备份：mv {backup} {src}"
                else:
                    rollback_hint = step.get("rollback") or rb.rollback_plan or "按变更单回滚计划人工处理"
            ok = bool(res.get("ok")) and not rollback_required
            _audit("success" if ok else "failed",
                   (f"Agent 执行：{res.get('error') or json.dumps(agent_result, ensure_ascii=False)[:400]}"
                    + (f"；post_check 未通过，需回滚/转人工：{rollback_hint}" if rollback_required else "")),
                   "auto" if auto_ok else "hitl",
                   approval_mode="auto" if auto_ok else "manual", by=who)
            await db.commit()
            return {"ok": ok, "guardrail": "auto" if auto_ok else "hitl",
                    "approval_mode": "auto" if auto_ok else "manual",
                    "host": rb.host_ident, "action": step["action"],
                    "executed_via": "agent", "cmd": step.get("cmd", ""),
                    "agent_result": agent_result or res.get("result"), "task_id": res.get("task_id"),
                    "post_check": post or None, "rollback_required": rollback_required,
                    "rollback_hint": rollback_hint, "error": res.get("error")}

        if hit:                                               # 非结构化动作 + auto 白名单：只生成待执行命令
            _audit("success", f"白名单自动放行（命中动作 {hit['id']}）", "auto",
                   approval_mode="auto", by=who, at=datetime.utcnow())
            rb.status = "executing"
            await db.commit()
            return {"ok": True, "guardrail": "auto", "approval_mode": "auto", "allowlist_id": hit["id"],
                    "host": rb.host_ident, "execute_on_target": cmd, "desc": step.get("desc"),
                    "execution_mode": mode,
                    "note": "命中动作清单，免人工确认放行（该步骤没有结构化动作，命令请人工在目标机执行）"}

        rec = await _consume_confirm(db, runbook_id, step_idx, confirm_token)   # 人工确认凭据
        if rec is None:
            hint = "该命令未命中动作清单，需人工确认" if mode == "auto" else "需人工确认"
            _audit("started", f"{hint}：未提供有效审批凭据", "hitl")
            await db.commit()
            return {"ok": False, "msg": f"HITL_REQUIRED：{hint}——请先调用 "
                                        f"POST /api/ops/runbooks/{runbook_id}/steps/{step_idx}/confirm "
                                        f"取得一次性确认凭据后再执行",
                    "data": {"guardrail": "hitl", "step": step, "execution_mode": mode,
                             "allowlisted": False, "approved_param_ignored": bool(approved)}}
        rec.used_at = datetime.utcnow()                        # 用后即焚
        _audit("success", f"人工确认放行（确认人 {rec.operator}）", "hitl",
               approval_mode="manual", by=rec.operator, at=rec.created_at)
        rb.status = "executing"
        await db.commit()
        return {"ok": True, "guardrail": "hitl", "approval_mode": "manual", "approved_by": rec.operator,
                "host": rb.host_ident, "execute_on_target": cmd, "desc": step.get("desc"),
                "execution_mode": mode,
                "note": "V1 无远程命令通道：请在目标机执行该命令并回填结果（操作已入审计）"}


# ── D160：变更单兜底收敛（真机验收发现）──────────────────────────
# 问题：变更单一旦置为 `executing`，**没有任何路径把它收回终态**（执行中断 / Agent 离线 /
# 审批人没点确认都会留在这个状态）。而自愈护栏⑧「发布/变更期间禁止自愈」按状态统计
# `approved|executing` 的数量 → 一张僵死的变更单会**永久挡住全平台自愈**。
# 真机实测：10-17 验收遗留 12 张 executing 卡了 4 天，期间任何自愈动作都只留痕 denied。
# 处置：① 超过阈值没有活动的 executing/approved 自动收敛为 aborted（留审计）；
#       ② 护栏只认「近期有活动」的变更单（双保险，见 services/heal.py）。
STALE_RUNBOOK_MINUTES = 30


async def sweep_stale_running(db, stale_minutes: int = STALE_RUNBOOK_MINUTES) -> int:
    """把超时无活动的 executing/approved 变更单收敛为 aborted，返回收敛条数。

    只改状态、不删数据（审计留痕在 action_audit 里可查）。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.models.ops import ActionAudit, Runbook

    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=stale_minutes)
    rows = (await db.execute(
        select(Runbook).where(Runbook.status.in_(("executing", "approved")),
                             Runbook.updated_at < cutoff))).scalars().all()
    for rb in rows:
        rb.status = "aborted"
        db.add(ActionAudit(runbook_id=rb.id, host_ident=rb.host_ident or "",
                           action="runbook_auto_abort",
                           params={"stale_minutes": stale_minutes},
                           result=f"执行中断且超过 {stale_minutes} 分钟无活动 → 自动收敛为 aborted",
                           operator="ai", status="success"))  # operator 是 ENUM('ai','user')
    if rows:
        await db.commit()
    return len(rows)
