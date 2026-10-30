"""ai_chat_sessions.target_hosts（D122）：会话聚焦主机。

Revision ID: e5f7a9c1d3b5
Revises: c4d8e2f6a9b1
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f7a9c1d3b5"
down_revision = "c4d8e2f6a9b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_chat_sessions", sa.Column("target_hosts", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_chat_sessions", "target_hosts")
