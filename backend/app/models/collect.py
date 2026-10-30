"""采集模型：probes（04 §2 采集与监控区）；hosts 增补 agent_token（05 §2 register 返回）。"""
from sqlalchemy import BigInteger, Enum, String

from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base, Host

# hosts 表增补：Agent 机器令牌（register 发放，metrics 上报校验）
# 说明：04 DDL 未列此列——D1 实现按 05 接口契约补录，回填 04 变更记录（AGENTS.md 纪律）
if not any(c.name == "agent_token" for c in Host.__table__.columns):
    Host.agent_token: Mapped[str] = mapped_column(String(64), default="")


class Probe(Base):
    __tablename__ = "probes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(
        Enum("http", "tcp", "process", "https-cert", name="probe_type"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    last_status: Mapped[str] = mapped_column(Enum("up", "down", "unknown", name="probe_status"), default="unknown")
    last_latency_ms: Mapped[int] = mapped_column(default=-1)  # https-cert 类型时存剩余天数（04 注记）
