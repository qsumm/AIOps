"""host 手动下线/移出支持

主机管理新增两个手动操作（后端 auth 拦截 + 端点 + 前端按钮）：
  - offline：下线（Agent 上报被拒、status 置 down 退出监控），可恢复
  - removed：移出（从列表隐藏/退役，历史告警/事件/巡检保留），需重新接入复原
两列都是 Bool、server_default '0'，不动 host_status enum（避免 ENUM ALTER 风险）。

Revision ID: f0c1a2d3b4e5
Revises: 8e3f8654414c
"""
from alembic import op
import sqlalchemy as sa

revision = "f0c1a2d3b4e5"
down_revision = "8e3f8654414c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("offline", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("hosts", sa.Column("removed", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("hosts", "removed")
    op.drop_column("hosts", "offline")