"""健康诊断模型（D119，FR-C3）：规则诊断树报告落库，可历史回看。

items 用 JSON 内嵌（单报告 ≤64KB，platform 侧截断）——少一张表、查询一次到位。
"""
from datetime import datetime

from sqlalchemy import CHAR, BigInteger, DATETIME, Index, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class HealthReport(Base):
    __tablename__ = "health_reports"
    __table_args__ = (Index("idx_health_host_time", "host_ident", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_ident: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running", server_default="running")  # running/done/failed
    source: Mapped[str] = mapped_column(String(16), default="rule", server_default="rule")        # rule/ai/hybrid
    window_start: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)  # 诊断数据窗（回溯用）
    window_end: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # {overall, pass, warn, fail, skip}
    items: Mapped[list | None] = mapped_column(JSON, nullable=True)     # [{id,title,scope,status,evidence,conclusion,advises}]