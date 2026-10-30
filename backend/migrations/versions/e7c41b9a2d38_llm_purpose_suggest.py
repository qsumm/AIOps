"""llm_purpose 增加 suggest（D47：AI 助手的实时建议问题也计入用量）

Revision ID: e7c41b9a2d38
Revises: <head>
"""
from alembic import op

revision = "e7c41b9a2d38"
down_revision = "f4b2c8d91e35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ENUM 加值：MySQL 需 MODIFY 整列（不能 ADD VALUE 之外的方式）
    op.execute(
        "ALTER TABLE llm_usage MODIFY purpose "
        "ENUM('rca','chat','summary','inspect','fixcheck','embed','suggest') NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE llm_usage MODIFY purpose "
        "ENUM('rca','chat','summary','inspect','fixcheck','embed') NOT NULL"
    )
