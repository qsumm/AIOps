"""D9-fix: prom_datasources 增加直采模式（FR-B3+ node_exporter 直采）

需求 FR-B3+ 要求"平台直接 scrape node_exporter 的 /metrics（prometheus_client 解析器），
已有 exporter 的用户零改造接入"。此前只有"查询代理"一种模式（轨 B），
本迁移加 mode/scrape_config 两列，使同一张数据源表同时承载两种模式。

见 03 §1.6、04 §8。
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1a7c02be9f4'
down_revision = 'c9d1f0a4e77b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prom_datasources", sa.Column("mode", sa.String(16), nullable=False, server_default="query"))
    op.add_column("prom_datasources", sa.Column("scrape_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("prom_datasources", "scrape_config")
    op.drop_column("prom_datasources", "mode")
