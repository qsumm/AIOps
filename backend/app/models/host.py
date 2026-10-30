"""首批 ORM 模型：hosts（FR-B5）与 smoke 表清理后的基线。

字段依据：04-数据库设计说明书 §2「采集与监控」。
注意：向量列（case_cards.embedding）在 SQLite/MySQL 降级为 TEXT，由 dal 层适配——
     SeekDB 冒烟已验证 VECTOR 类型可用（见 04 §5 回填）。
"""
from datetime import datetime

from sqlalchemy import JSON, CHAR, DATETIME, BigInteger, Boolean, Enum, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ident: Mapped[str] = mapped_column(CHAR(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="")
    ip: Mapped[str] = mapped_column(String(45), default="")
    os: Mapped[str] = mapped_column(String(64), default="")
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    # D121：Agent 代码文件指纹（agent_meta 上报）——版本漂移判定 = 与平台 serving 的文件 hash 对比
    agent_code_sha256: Mapped[str] = mapped_column(String(64), default="", server_default="")
    tags: Mapped[dict] = mapped_column(JSON, default=dict)   # 04 §2 为 JSON；原 tags_json(VARCHAR) 与 DDL 不一致，D7 对齐
    status: Mapped[str] = mapped_column(
        Enum("up", "down", "pending", name="host_status"), default="pending"
    )
    # 手动下线/移出（用户在【主机管理】操作，留存审计）。与 status 分工：
    #   offline=True → Agent 上报被拒、status 置 down 退出监控，可恢复；
    #   removed=True → 从主机列表隐藏（退役），历史告警/事件/巡检保留，需重新接入才能复原。
    offline: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_beat: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    # P0：Agent 认证/上报健康。只表达“平台是否收到过合法凭证”，不替代 status。
    auth_state: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    last_auth_ok_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    last_report_ok_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    cache_depth: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_agent_error: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # D146：机器硬件配置（Agent 随心跳上报 hostinfo）。
    # 为什么要落表：主机详情此前只有 IP/OS/版本——用户看不出「几核几 G」，而排障第一步
    # 往往就是问配置。这些值几乎不变，落表后告警详情/导出/历史都能用。
    cpu_cores: Mapped[int] = mapped_column(Integer, default=0, server_default="0")           # 逻辑核数
    cpu_cores_physical: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 物理核数
    cpu_model: Mapped[str] = mapped_column(String(160), default="", server_default="")
    mem_total: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")        # 内存字节数
    boot_time: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    hostinfo_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)            # 上报时间
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())
