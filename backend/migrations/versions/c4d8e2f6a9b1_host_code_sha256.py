"""Host.agent_code_sha256 列（D121）：Agent 代码文件指纹，版本漂移判定用。

Revision ID: c4d8e2f6a9b1
Revises: a9b3c7d5e1f2
"""
from alembic import op
import sqlalchemy as sa

revision = "c4d8e2f6a9b1"
down_revision = "a9b3c7d5e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("agent_code_sha256", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("hosts", "agent_code_sha256")