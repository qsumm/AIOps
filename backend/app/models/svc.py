"""P5 服务级监控与任务通道模型（v0.4；详设 §1.28/§1.30/§1.31）。

两张表：
  · `services`     —— 服务对象（主机 × 服务 × 类型 × 状态 × 指标），把"端口通不通"
                       升级为"服务内部健不健康"（FR-408）
  · `agent_tasks`  —— 平台→Agent 的受控任务队列（FR-409/411）：
                       只有 `log_fetch`（只读取证）与 `svc_action`（枚举动作）两类，
                       这是安全边界的全部（详设 §1.28）

为什么任务表要落库而不是内存队列：
  ① 取证与恢复都必须**可审计、可回放**（FR-411 要求 action_audit 可复核）；
  ② Agent 拉取是异步的（可能几秒后才来取），内存队列在平台重启时会丢任务；
  ③ 后续要统计"取了几次、成功率、耗时"（自愈计数与会话质量），落库才好聚合。
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DATETIME, Enum, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class Service(Base):
    """服务对象（FR-408）。一个"服务"= 主机上的一类可观测服务实例（如该机的 MySQL）。"""

    __tablename__ = "services"
    __table_args__ = (Index("idx_svc_host", "host_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)          # 展示名，如 "MySQL(3306)"
    type: Mapped[str] = mapped_column(
        Enum("mysql", "redis", "nginx", "mq", "app", "other", name="svc_type"), default="app")
    # 声明式配置：{"port":3306,"log_path":"/var/log/mysql/error.log","unit":"mysql","health_url":""}
    spec_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 三态 + unknown，与探活同语义（详设 §1.30）：
    #   up=能取到指标且关键项正常 / degraded=能取到但超阈 / down=取不到 / unknown=未探测
    status: Mapped[str] = mapped_column(
        Enum("up", "degraded", "down", "unknown", name="svc_status"), default="unknown")
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)         # 服务内部指标快照
    note: Mapped[str] = mapped_column(String(255), default="")             # 状态说明（人话）
    enabled: Mapped[bool] = mapped_column(default=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())


class AgentTask(Base):
    """平台→Agent 受控任务（FR-409 取证 / FR-411 恢复）。

    通道上**只允许两种 kind**（详设 §1.28）：
      · log_fetch  载荷 {path, since, until, pattern, level, context}  —— 只读取证，Agent 侧筛完只回传相关片段
      · svc_action 载荷 {action, service, ...}                        —— 只认枚举动作 ID，无自由文本命令
    任何"把用户输入拼进 shell"的路径都不存在（对抗测试项）。
    """

    __tablename__ = "agent_tasks"
    __table_args__ = (Index("idx_task_pick", "host_id", "status", "id"),
                      Index("idx_task_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 注意：这里刻意用 String 而非 ENUM —— SeekDB 实测 ENUM 列在**复合索引**中比较会报
    # (1210, 'Invalid argument')（`WHERE host_id=? AND status='picked'` 走 idx_task_pick 必炸）；
    # 本表 status/kind 高频用于复合索引查询，故用 VARCHAR 承担（取值由代码与迁移约束）。
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)   # 已脱敏的取回结果
    error: Mapped[str] = mapped_column(Text, default="")
    # 归属：哪次诊断/哪个变更单触发的（用于报告标注来源轮次与审计串联）
    origin: Mapped[str] = mapped_column(String(64), default="")     # 如 "incident:12" / "runbook:3" / "manual"
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    picked_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)


class ServiceHeal(Base):
    """自愈计数（FR-411：同一服务 7 天内被自愈 ≥3 次 → 升级为"请人工修根因"）。

    为什么要单独计数：行业教训是"每晚被重启的服务永远不会被修好"——
    自愈必须被**计量**，频繁自愈是缺陷信号而不是稳定性证据。
    """

    __tablename__ = "service_heals"
    __table_args__ = (Index("idx_heal_svc", "service_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    host_ident: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(32), default="")
    ok: Mapped[bool] = mapped_column(default=False)
    detail: Mapped[str] = mapped_column(String(255), default="")
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), index=True)


class HealGuard(Base):
    """自愈熔断状态（FR-411：连续 2 次失败即停 + 每时限额）。

    按"服务 × 动作"维度记录连续失败次数与最近执行时间，
    连续失败达阈值 → 该服务转"需人工"，不再自动下发（直到人工重置）。
    """

    __tablename__ = "heal_guards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)
    tripped: Mapped[bool] = mapped_column(default=False)          # 已熔断（需人工重置）
    tripped_reason: Mapped[str] = mapped_column(String(255), default="")
    last_action_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    hour_window_start: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    hour_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())
