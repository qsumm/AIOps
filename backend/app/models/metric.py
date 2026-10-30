"""带维度指标序列的持久化模型（FR-424；D154）。

为什么单独建表而不扩 `metric_agg`：
- `metric_agg` 是**降采样**表（1m/5m 桶），是"一段时间一个均值"；
- 带维度序列要保留**每条序列自己的点**（`pg.connections.current{server=db01}` 与
  `{server=db02}` 是两条独立序列，各自触发/恢复）——塞进聚合表会丢维度。
- 维度必须**可选**：不带维度的点仍然只走内存 ring + `metric_agg`（老行为逐字节不变）。

索引说明（SeekDB 纪律，见《04-数据库设计说明书》§7/§10）：
- 索引**手写**（反射缺 `BLOCK_SIZE` 会误报索引消失）；
- `tags_key` 用**规范化串**（`k1=v1;k2=v2` 排序后）而不是哈希——哈希无法按维度范围查；
- `DATETIME` 不加 `server_default=now()`（会带时区偏移）；`JSON` 列不加 `TEXT` 的 server_default。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DATETIME, Float, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class MetricSeries(Base):
    """带维度的一个指标序列的采样点。"""

    __tablename__ = "metric_series"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ident: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    tags_key: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ts: Mapped[datetime] = mapped_column(DATETIME, nullable=False)

    __table_args__ = (
        Index("idx_series_query", "ident", "metric", "tags_key", "ts"),
        Index("idx_series_ts", "ts"),
    )
