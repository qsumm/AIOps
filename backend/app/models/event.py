"""事件与 AI 域模型：incidents / rca_findings / execution_steps / incident_thoughts / ai_chat_sessions（04 §2）。

incident_thoughts 为 04 §2 的补录表（03 §1.13 要求"三层留痕：thoughts / execution_steps /
tool_call_history"，04 表清单漏列），D4 补齐并回写 04 §9。
"""
from datetime import datetime

from sqlalchemy import CHAR, BigInteger, DATETIME, Enum, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Text as _Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT

MEDIUMTEXT_V = MEDIUMTEXT().with_variant(_Text(), "sqlite")  # MySQL MEDIUMTEXT，SQLite 降级 TEXT

from app.models.host import Base


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("idx_inc_status", "status", "severity"), Index("idx_inc_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    severity: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(Enum("open", "ack", "resolved", name="incident_status"), default="open")
    alert_hashes: Mapped[list] = mapped_column(JSON, default=list)      # 成员告警指纹
    source_key: Mapped[str] = mapped_column(CHAR(32), unique=True)      # 首告警指纹（幂等锚点）
    host_ident: Mapped[str] = mapped_column(CHAR(64), default="", index=True)
    cluster_reason: Mapped[str] = mapped_column(String(255), default="")  # rule | llm | manual
    ai_report_md: Mapped[str] = mapped_column(MEDIUMTEXT_V, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str | None] = mapped_column(
        Enum("CONFIRMED", "LIKELY", "INCONCLUSIVE", name="inc_confidence"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())


class RCAFinding(Base):
    __tablename__ = "rca_findings"
    __table_args__ = (Index("idx_findings_inc", "incident_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, index=True)
    agent_id: Mapped[str] = mapped_column(String(32), default="rca-1")
    role_name: Mapped[str] = mapped_column(String(64), default="")
    purpose: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        Enum("running", "succeeded", "failed", "timeout", name="finding_status"), default="running"
    )
    findings_md: Mapped[str] = mapped_column(MEDIUMTEXT_V, default="")
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    tool_call_history: Mapped[list] = mapped_column(JSON, default=list)   # 压缩前的调用流水（含参数）
    self_assessed_strength: Mapped[str | None] = mapped_column(
        Enum("strong", "moderate", "weak", "inconclusive", name="self_strength"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(64), default="")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)


class FindingTriage(Base):
    """D170（FR-426）：诊断报告的**人工分诊**记录——发现分诊闭环的核心表。

    背景：AI 诊断每份报告带强度自评（strong/moderate/weak/inconclusive），但
    「哪些可以忽略、哪些必须跟进」最终由人判断——判断必须**留痕并可复用**：
    同主机同指标的同类报告，人忽略过一次，后续自动降级（不再反复打扰）。

    action 三态：followup（跟进：转事件/变更处理）/ observing（观察：暂不处理但保留）/
    dismissed（忽略：附理由）。同 finding 多次处置取最新（幂等重判允许）。
    """
    __tablename__ = "finding_triage"
    __table_args__ = (Index("idx_triage_finding", "finding_id"),
                      Index("idx_triage_key", "host_ident", "metric"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = mapped_column(BigInteger, index=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, index=True)
    host_ident: Mapped[str] = mapped_column(String(64), default="", index=True)
    metric: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(
        Enum("followup", "observing", "dismissed", name="triage_action"))
    reason: Mapped[str] = mapped_column(Text, default="")     # 忽略/跟进的理由（忽略时必填，服务端校验）
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())


class ExecutionStep(Base):
    __tablename__ = "execution_steps"
    __table_args__ = (Index("idx_steps_inc", "incident_id", "step_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, index=True)
    finding_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    tool_name: Mapped[str] = mapped_column(String(64), default="")
    tool_input: Mapped[dict] = mapped_column(JSON, default=dict)
    tool_output: Mapped[str] = mapped_column(MEDIUMTEXT_V, default="")
    status: Mapped[str] = mapped_column(Enum("running", "success", "error", name="step_status"), default="success")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())


class IncidentThought(Base):
    """三层留痕之一：模型的思考/假设陈述（逐轮）。"""

    __tablename__ = "incident_thoughts"
    __table_args__ = (Index("idx_thoughts_inc", "incident_id", "step_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, index=True)
    finding_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(MEDIUMTEXT_V, default="")
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())


class AIChatSession(Base):
    __tablename__ = "ai_chat_sessions"
    __table_args__ = (Index("idx_chat_user", "user_id", "updated_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    messages: Mapped[list] = mapped_column(JSON, default=list)
    # D87 上下文管理：滚动摘要 + 覆盖边界。
    # `messages` 是**展示用的全量历史**（用户回看不会变短）；发给模型的只有 messages[summary_upto:]，
    # 更早的部分折进 `summary`。两者分开存，是为了"展示完整"与"上下文有界"能同时成立。
    summary: Mapped[str | None] = mapped_column(Text)
    summary_upto: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # D122：会话聚焦主机（用户在 AI 助手里勾选）——ident 列表，注入 system prompt 作为回答上下文
    target_hosts: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())
