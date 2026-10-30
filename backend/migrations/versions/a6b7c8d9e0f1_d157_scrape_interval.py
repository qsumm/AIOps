"""D157：PromDatasource.scrape_interval_s 字段（FR-422 补充）。

mode=scrape 的拉取间隔（秒），默认 30s（与现有 sched.py 全局硬编码一致）；
0=不自动拉（只能手动试采）。前端下拉 30/60/120/300/600。

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prom_datasources", sa.Column("scrape_interval_s", sa.Integer(),
                                                 nullable=False, server_default="30"))


def downgrade() -> None:
    op.drop_column("prom_datasources", "scrape_interval_s")
