"""D154：带维度指标序列表（FR-424 的存储地基）。

一条序列 = (ident, metric, tags_key)。维度用于让"同一条规则在不同维度上产生独立事件"
（如每台 PG 的连接数各算各的），也用于跨实例聚合（"这批 PG 的连接率普遍偏高吗"）。

为什么单独建表（不扩 metric_agg）：
- metric_agg 是**降采样**表（1m/5m 桶），一条曲线一个均值，塞不下"每条序列自己的点"；
- 维度**可选**——不带维度的点仍只走内存 ring + metric_agg，老行为逐字节不变。

索引手写（SeekDB 反射缺 BLOCK_SIZE，见《04-数据库设计说明书》§7/§10）；
tags_key 用规范化串（k1=v1;k2=v2 排序后）而非哈希——哈希无法按维度范围查询。

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_series",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ident", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("tags_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ts", sa.DATETIME(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_series_query", "metric_series",
                    ["ident", "metric", "tags_key", "ts"], unique=False)
    op.create_index("idx_series_ts", "metric_series", ["ts"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_series_ts", table_name="metric_series")
    op.drop_index("idx_series_query", table_name="metric_series")
    op.drop_table("metric_series")
