"""users 加 preferred_lang（UI 中英文切换 D97；默认 zh；老数据自动回填 zh）

Revision ID: c8a1f2b9d643
Revises: b7c2e5f10a94
"""
from alembic import op

revision = "c8a1f2b9d643"
down_revision = "b7c2e5f10a94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL DEFAULT 'zh'：老用户自动 zh；新用户默认 zh；后续可改。
    op.execute("ALTER TABLE users ADD COLUMN preferred_lang VARCHAR(8) NOT NULL DEFAULT 'zh'")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN preferred_lang")
