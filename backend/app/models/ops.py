"""巡检 / 处置 / 技能域模型（FR-E；04 §2）。

case_cards 不在此定义：它含 `embedding VECTOR(n)` 列，SQLAlchemy 反射会抛
`NullType() takes no arguments`（D0 冒烟实测），故**该表全部走 raw SQL**：
建表在迁移里手写，读写/向量检索在 `services/cases.py` 里手写（04 §5.1/§10）。
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DATETIME, Enum, Float, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.event import MEDIUMTEXT_V
from app.models.host import Base


class Inspection(Base):
    """定时巡检任务与报告（FR-D5；03 §1.16）。"""

    __tablename__ = "inspections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    cron_expr: Mapped[str] = mapped_column(String(64), default="0 9 * * *")
    target_scope: Mapped[dict] = mapped_column(JSON, default=dict)   # {"hosts": "all"|["ident"...], "items":[...]}
    enabled: Mapped[bool] = mapped_column(default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    last_status: Mapped[str] = mapped_column(
        Enum("idle", "running", "ok", "failed", name="insp_status"), default="idle"
    )
    report_md: Mapped[str] = mapped_column(MEDIUMTEXT_V, default="")
    findings: Mapped[list] = mapped_column(JSON, default=list)       # 异常项列表
    incidents_created: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class Runbook(Base):
    """变更单（FR-E1；03 §1.17）：由诊断结论生成的处置方案，版本化留痕。"""

    __tablename__ = "runbooks"
    __table_args__ = (Index("idx_rb_inc", "incident_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    incident_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    host_ident: Mapped[str] = mapped_column(String(64), default="")
    risk_assessment: Mapped[str] = mapped_column(Text, default="")
    pre_checks: Mapped[list] = mapped_column(JSON, default=list)     # [{desc, cmd|check}]
    steps: Mapped[list] = mapped_column(JSON, default=list)          # [{idx, desc, cmd, rollback, mode}]
    rollback_plan: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(Enum("low", "mid", "high", name="rb_risk"), default="mid")
    # 执行模式（FR-E2 的"三选"）：suggest=仅建议（默认，平台只给命令）/ manual=人工确认 /
    # auto=白名单自动（仅对"动作清单"命中的动作免确认）。
    # 兼容：whitelist_enabled 保留为 legacy 列，取值为 (execution_mode == "auto")，便于老客户端/查询。
    execution_mode: Mapped[str] = mapped_column(String(16), default="manual")
    whitelist_enabled: Mapped[bool] = mapped_column(default=False)   # legacy：等价 execution_mode=="auto"
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(
        Enum("draft", "approved", "executing", "done", "aborted", name="rb_status"), default="draft"
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())


class RunbookVersion(Base):
    """copy-on-write 全量快照：旧版本只读（03 §1.17）。"""

    __tablename__ = "runbook_versions"
    __table_args__ = (Index("idx_rbv_runbook", "runbook_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runbook_id: Mapped[int] = mapped_column(BigInteger, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())


class ActionAudit(Base):
    """处置动作审计（FR-E2）：每次执行/回滚一条，operator=ai|user。"""

    __tablename__ = "action_audit"
    __table_args__ = (Index("idx_audit_action", "runbook_id"), Index("idx_audit_action_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runbook_id: Mapped[int] = mapped_column(BigInteger, index=True)
    host_ident: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(128), default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(Enum("ai", "user", name="audit_operator"), default="user")
    status: Mapped[str] = mapped_column(
        Enum("started", "success", "failed", "aborted", name="audit_status"), default="started"
    )
    guardrail_verdict: Mapped[str] = mapped_column(String(64), default="")  # allowlist|denylist|llm_denied|hitl|auto
    approval_mode: Mapped[str] = mapped_column(String(16), default="")      # readonly|manual|auto（D9：让"批准"可追溯）
    approved_by: Mapped[str] = mapped_column(String(64), default="")         # 审批人用户名（manual/auto 时）
    approved_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())


class ActionApproval(Base):
    """处置步骤的**审批凭据**（FR-E2 HITL 的可追溯落地）。

    为什么单独一张表：`approved=true` 由客户端自报时，审计无法证明"有人审过"，
    谁都能 curl 直接过。改为：确认动作先落一条审批记录（操作者/时间/IP/一次性 token 哈希），
    执行时必须出示该 token → 审计里的"已批准"才有出处，且不可重放（用后即焚 + TTL）。
    """

    __tablename__ = "action_approvals"
    __table_args__ = (Index("idx_appr_rb", "runbook_id", "step_idx"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runbook_id: Mapped[int] = mapped_column(BigInteger, index=True)
    step_idx: Mapped[int] = mapped_column(Integer, default=0)
    operator: Mapped[str] = mapped_column(String(64), default="")
    token_hash: Mapped[str] = mapped_column(String(64), index=True)   # sha256(token)，不存明文
    ip: Mapped[str] = mapped_column(String(45), default="")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)        # 确认当时的命令/风险（防"批了 A 执行 B"）
    mode: Mapped[str] = mapped_column(String(16), default="manual")
    expires_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())


class Skill(Base):
    """技能（FR：SKILL.md 注册表；03 §1.20/调研 §1）。"""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")       # 必填（缺失即失配，Aurora 强校验）
    tools: Mapped[list] = mapped_column(JSON, default=list)
    connection_check: Mapped[dict] = mapped_column(JSON, default=dict)
    rca_priority: Mapped[int] = mapped_column(Integer, default=10)
    index_text: Mapped[str] = mapped_column(String(512), default="")  # 注入 prompt 的索引行
    body_md: Mapped[str] = mapped_column(MEDIUMTEXT_V, default="")
    alerts_scope: Mapped[list] = mapped_column(JSON, default=list)    # 按告警名 scope
    commands: Mapped[list] = mapped_column(JSON, default=list)       # 技能授权的只读命令（D173/P2）
    source: Mapped[str] = mapped_column(Enum("builtin", "user", "imported", name="skill_source"), default="builtin")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())

class HealPolicy(Base):
    """自动自愈策略（D140 用户需求）：指标越线 → 匹配策略 → 执行白名单动作。

    动作复用 services/heal.ACTIONS 白名单（restart_service / restart_container /
    reconnect_pool / rotate_logs）——不新增自由文本命令，护栏（evaluate_guards）照走。
    三档 mode 与 svc heal 同语义：suggest=只评估留痕 / manual=需人工确认 / auto=护栏通过即执行。
    """

    __tablename__ = "heal_policies"
    __table_args__ = (
        Index("idx_heal_pol_rule", "trigger_rule_id"),
        Index("idx_heal_pol_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    # 触发条件：优先关联告警规则（引擎触发时带事件来）；否则按 metric 直填比对
    trigger_rule_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trigger_metric: Mapped[str] = mapped_column(String(64), default="")
    trigger_op: Mapped[str] = mapped_column(String(8), default=">=")          # >= / <=
    trigger_threshold: Mapped[float] = mapped_column(Float, default=90)
    trigger_severity: Mapped[int] = mapped_column(Integer, default=1)         # 1/2/3
    # 动作与目标
    action: Mapped[str] = mapped_column(String(32), default="restart_service")
    service_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 空=告警主机自动解析
    # 三档语义（与 svc heal 一致）+ 冷却（同一策略两次触发最小间隔，防循环自愈）
    mode: Mapped[str] = mapped_column(String(16), default="suggest")
    cooldown_min: Mapped[int] = mapped_column(Integer, default=15)
    # D143 预测预警：>0 = 提前 N 分钟做趋势外推预测（只通知不动手）；0 = 关闭
    predict_min: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    # idle / dry_run / waiting_confirm / dispatched / denied / failed / cooldown
    last_status: Mapped[str] = mapped_column(String(24), default="idle")
    last_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class HealPolicyRun(Base):
    """自愈策略执行留痕：自愈不是黑盒——每次触发都留「发生了什么/为什么/做了什么」。"""

    __tablename__ = "heal_policy_runs"
    __table_args__ = (
        Index("idx_hpr_policy", "policy_id"),
        Index("idx_hpr_time", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_name: Mapped[str] = mapped_column(String(128), default="")
    alert_hash: Mapped[str] = mapped_column(String(64), default="")
    host_ident: Mapped[str] = mapped_column(String(64), default="")
    metric: Mapped[str] = mapped_column(String(64), default="")
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    action: Mapped[str] = mapped_column(String(32), default="")
    mode: Mapped[str] = mapped_column(String(16), default="suggest")
    # dry_run / waiting_confirm / dispatched / denied / failed
    status: Mapped[str] = mapped_column(String(24), default="dry_run")
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    guard_rule: Mapped[str] = mapped_column(String(64), default="")
    guard_reason: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
