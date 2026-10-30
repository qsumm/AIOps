"""LLM 网关模型：llm_configs / llm_usage（04 §2；FR-A2）。"""
from datetime import datetime

from sqlalchemy import BigInteger, DATETIME, Enum, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class LLMConfig(Base):
    __tablename__ = "llm_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(255), default="")
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    fast_model: Mapped[str] = mapped_column(String(64), default="")
    # D163-3：**降级通道**必须由用户显式指定，平台不能替用户挑。
    # 原实现降级时隐式改用同一配置的 fast_model —— 那是"轻量任务用的快模型"，
    # 用户配它**不等于**允许它兜底（配额/合规/计费都可能不允许）。
    # 现在只有勾了本标记的配置才会被用作降级通道；没配就如实报错，不擅自替用户决定。
    fallback_enabled: Mapped[bool] = mapped_column(default=False)
    extra_config: Mapped[dict] = mapped_column(JSON, default=dict)  # temperature/max_tokens/context_length/embedding_dim
    enabled: Mapped[bool] = mapped_column(default=True)
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(
        Enum("rca", "chat", "summary", "inspect", "fixcheck", "embed", "suggest", name="llm_purpose"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    total_tokens: Mapped[int] = mapped_column(default=0)
    # 用量归属（D21 修复新增）：谁触发的这次调用 —— "demo"/"admin"/"member" 为用户角色，
    # "engine" 为后台任务（告警 AI 摘要、收敛兜底、保留期治理等）。
    # 没有它就无法回答"演示模式今天自己用了多少 token"，配额只能按全平台统计（= 原缺陷）。
    role: Mapped[str] = mapped_column(String(16), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), index=True)
