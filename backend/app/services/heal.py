# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""分级恢复的护栏（P5c，FR-411；详设 §1.31）。

**本模块的定位**：恢复能力的难点不在"执行"，而在"敢于不执行"（方案 §4）。
所以护栏不是附加检查，而是**准入判决**——任何一条不通过就不许动，并给出人话原因。

九条护栏（对齐行业清单，逐条可测）：
  ① 动作白名单（只认 5 个枚举 ID，无自由文本命令）
  ② 限流：每主机每时 ≤3 次
  ③ 连续失败熔断：连续 2 次校验未过 → 该服务转「需人工」，停止自动执行
  ④ 爆炸半径：一次只动一个服务；**禁止跨主机批量**
  ⑤ 依赖感知：上游（数据库/网络）异常时不做本地自愈（否则雪上加霜）
  ⑥ 无回滚不执行：动作必须声明后置校验
  ⑦ 幂等：执行前判状态（已恢复就不重启）
  ⑧ 发布窗口互斥：有正在执行的变更单时禁自愈
  ⑨ 全局熔断开关 + 自愈计数（7 天内 ≥3 次 → 升级为「请人工修根因」）
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select

log = logging.getLogger("heal")

# ── 动作白名单（枚举 ID；每个动作自带前置条件与后置校验声明）──────────────
# `target` 说明该动作要操作**谁**：systemd 单元（unit）还是容器（container）——
# 平台据此从服务声明里取目标，绝不回落到服务显示名（见 resolve_target 的实测教训）。
ACTIONS: dict[str, dict] = {
    "restart_service": {
        "label": "重启服务", "mode": "systemctl", "target": "unit",
        "pre": "systemd 单元存在且未处于 active",
        "requirement": "需在服务声明里填写 systemd 单元名（unit）",
        "post": "systemd is-active / 端口可连（任一通过）",
    },
    "restart_container": {
        "label": "重启容器", "mode": "docker", "target": "container",
        "pre": "容器存在",
        "requirement": "需在服务声明里填写容器名（container；缺省时回落到 unit）",
        "post": "容器运行中 / 端口可连（任一通过）",
    },
    "reconnect_pool": {
        "label": "重连连接池", "mode": "systemctl", "target": "unit",
        "pre": "应用服务存在（语义化动作，落地为重启并单独审计）",
        "requirement": "需在服务声明里填写 systemd 单元名（unit）",
        "post": "端口可连（任一通过）",
    },
    "rotate_logs": {
        "label": "轮转日志", "mode": "systemctl", "target": "unit",
        "pre": "服务由 systemd 管理且支持 HUP 重开日志",
        "requirement": "需在服务声明里填写 systemd 单元名（unit）",
        "post": "systemd is-active / 端口可连（任一通过）",
    },
}
# 目标名字符集：与 Agent 侧 task_service_name_ok 的白名单**必须一致**（回归锁锁定），
# 用于防止把 shell 元字符塞进命令行参数。平台侧先判一次，用户不必等一个来回。
_TARGET_RE = re.compile(r"[A-Za-z0-9_.@:\-]{1,64}")


def resolve_target(action: str, spec: dict) -> tuple[str, str | None]:
    """解析动作的目标对象，返回 (target, 错误原因)。

    **实测教训（真机跑出来的）**：原来回落成 `spec.unit or s.name`，而服务显示名可以是中文
    （"订单服务"）——Agent 侧对目标名做字符集白名单（防注入），于是用显示名下发的动作
    **100% 被拒**，用户只看到一句"服务名含非法字符"，而且平台侧毫无前置提示。
    结论：目标只能来自用户声明的 systemd 单元名 / 容器名；声明缺失就**明确拒绝并告诉去哪填**，
    绝不拿显示名去猜（猜错了要么失败、要么动错对象）。
    """
    a = ACTIONS.get(action) or {}
    kind = a.get("target")
    spec = spec or {}
    if kind == "container":
        target = str(spec.get("container") or spec.get("unit") or "").strip()
        human = "容器名（container）"
    elif kind == "unit":
        target = str(spec.get("unit") or "").strip()
        human = "systemd 单元名（unit）"
    else:
        return "", f"动作 {action!r} 未声明目标类型——无法确定操作对象"
    if not target:
        return "", (f"该服务未声明{human}——请先在服务详情「改声明」里填写；"
                    "平台不会拿服务显示名去猜目标（猜错要么白失败、要么动错对象）")
    if not _TARGET_RE.fullmatch(target):
        return "", (f"{human}含非法字符：{target[:40]!r}——只允许字母数字与 _ . @ : -（≤64 字符）。"
                    "该白名单与 Agent 侧一致，用于防止把 shell 元字符塞进参数")
    return target, None

# 注意：**「清理临时文件」刻意不在首版清单里**（方案 §8 待决策项 2）——
# 它涉及"删除"，行业虽认为可自动，但风险与收益比不如重启类明确；
# 待真实故障数据积累后再评估。这条注释是为了防止后人以为"漏了"。

HEAL_PER_HOUR = 3            # ② 限流
FAIL_STREAK_TRIP = 2         # ③ 连续失败熔断
FREQUENT_WINDOW_DAYS = 7     # ⑨ 自愈计数窗口
FREQUENT_TRIP = 3            # ⑨ 窗口内次数阈值


class HealDenied(Exception):
    """护栏拒绝：携带可读原因与命中的规则名（前端/AI 都靠它解释"为什么不让动"）。"""

    def __init__(self, rule: str, reason: str):
        super().__init__(reason)
        self.rule = rule
        self.reason = reason


def evaluate_guards(*, action: str, service, guard, recent_heals: int, host_tasks_last_hour: int,
                    upstream_ok: bool, active_runbooks: int, kill_switch: bool,
                    idle_confirmed: bool, dry_run: bool = False,
                    target_error: str | None = None) -> dict:
    """准入判决（**纯函数**，便于逐条单测）。

    入参都是"事实"（由调用方查库准备），返回值：
      {"allow": bool, "rule": str, "reason": str, "warnings": [...]}
    这样把"查数据"与"做判决"分开：判决逻辑可以脱离数据库穷举测试。
    """
    warnings: list[str] = []

    def deny(rule: str, reason: str) -> dict:
        return {"allow": False, "rule": rule, "reason": reason, "warnings": warnings}

    # ⑨ 全局熔断开关（最高优先：大故障时先停所有自愈）
    if kill_switch and not dry_run:
        return deny("kill_switch", "全局自愈开关已关闭（大故障期间保护措施）——需在服务详情里重新开启")

    # ① 动作白名单
    spec = ACTIONS.get(action)
    if spec is None:
        return deny("whitelist", f"动作不在白名单内：{action!r}（仅允许 {'/'.join(ACTIONS)}）")

    # ①b 动作目标可解析（声明缺失/字符集不合法 → 明确拒绝，不去猜目标）
    if target_error:
        return deny("target", target_error)

    # ③ 熔断（连续失败）
    if guard is not None and getattr(guard, "tripped", False):
        return deny("tripped", f"该服务已熔断：{guard.tripped_reason or '连续失败'}——"
                               "请先排查根因，再在服务详情里重置熔断")

    # ⑦ 幂等前置：已确认服务是好的，就不重启
    if idle_confirmed:
        return deny("idempotent", "该服务当前状态正常（无需恢复动作）——幂等保护：不会重复重启")

    # ② 限流
    if host_tasks_last_hour >= HEAL_PER_HOUR:
        return deny("rate_limit", f"该主机本小时恢复动作已达上限（{HEAL_PER_HOUR} 次/时）——"
                                  "频繁自愈说明根因未除，请转人工排查")

    # ⑥ 无回滚不执行（动作必须有后置校验声明）
    if not spec.get("post"):
        return deny("no_rollback", f"动作 {spec['label']} 未声明后置校验——无校验不执行")

    # ⑧ 发布窗口互斥
    if active_runbooks and not dry_run:
        return deny("deploy_window", f"存在正在执行的变更单（{active_runbooks} 个）——"
                                     "发布/变更期间禁止自愈，避免与变更互相干扰")

    # ⑤ 依赖感知（上游异常时本地自愈无效，且可能雪上加霜）
    if not upstream_ok and not dry_run:
        return deny("upstream", "上游依赖（数据库/网络）异常——本地重启无法解决问题，"
                                "按行业教训先排查上游，避免重启风暴把故障放大")

    # ⑨ 自愈计数：频繁自愈是缺陷信号
    if recent_heals >= FREQUENT_TRIP:
        warnings.append(f"该服务近 {FREQUENT_WINDOW_DAYS} 天已被自愈 {recent_heals} 次——"
                        "频繁自愈通常是缺陷信号，建议排查根因而非依赖自动重启")

    # ④ 爆炸半径：单服务单动作（批量入口在 API 层直接拒绝，这里做语义声明）
    warnings.append("单次仅对一个服务执行；不支持跨主机批量")

    return {"allow": True, "rule": "ok", "reason": "通过全部护栏", "warnings": warnings}


async def collect_facts(db, *, service_id: int, host_id: int, kill_switch: bool,
                        upstream_ok: bool) -> dict:
    """准备判决所需事实（查库集中在这里，判决逻辑保持纯函数）。"""
    from app.models.ops import Runbook
    from app.models.svc import AgentTask, HealGuard, ServiceHeal

    now = datetime.utcnow()
    guard = (await db.execute(select(HealGuard).where(HealGuard.service_id == service_id))
             ).scalar_one_or_none()
    recent = (await db.execute(select(func.count()).select_from(ServiceHeal).where(
        ServiceHeal.service_id == service_id,
        ServiceHeal.created_at >= now - timedelta(days=FREQUENT_WINDOW_DAYS)))).scalar() or 0
    host_hour = (await db.execute(select(func.count()).select_from(AgentTask).where(
        AgentTask.host_id == host_id, AgentTask.kind == "svc_action",
        AgentTask.created_at >= now - timedelta(hours=1)))).scalar() or 0
    # D160：只把**近期有活动**的变更单算作"发布窗口"——僵死（执行中断/审批未处理）的
    # 单子不该永久挡住自愈。兜底还有 services/runbook.py 的定时收敛（双保险）。
    rb_cutoff = now - timedelta(minutes=30)
    active_rb = (await db.execute(select(func.count()).select_from(Runbook).where(
        Runbook.status.in_(("approved", "executing")),
        Runbook.updated_at >= rb_cutoff))).scalar() or 0
    return {"guard": guard, "recent_heals": recent, "host_tasks_last_hour": host_hour,
            "active_runbooks": active_rb, "kill_switch": kill_switch, "upstream_ok": upstream_ok}


async def check_upstream(db, host_ident: str) -> bool:
    """上游健康判定（依赖感知的输入）。

    口径（保守）：该主机上**数据库类服务**若处于 down，则视为上游异常。
    查不到任何服务信息时按"健康"处理（不因为信息缺失就阻断恢复——否则护栏会变成瘫痪）。
    """
    from app.models.host import Host
    from app.models.svc import Service

    h = (await db.execute(select(Host).where(Host.ident == host_ident))).scalar_one_or_none()
    if h is None:
        return True
    rows = (await db.execute(select(Service).where(
        Service.host_id == h.id, Service.type.in_(("mysql", "redis", "mq"))))).scalars().all()
    if not rows:
        return True
    return all(r.status != "down" for r in rows)
