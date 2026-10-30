"""case_cards 增加 enabled（案例库可停用某张卡，FR-416）

Revision ID: d91b47f2c8a5
Revises: c3f0a74b9e21

为什么要这一列：案例卡是"平台学到的东西"，但它可能是错的（某次根因判断偏差、或处置无效）。
没有停用/删除手段时，一张错卡会一直参与向量召回、持续污染后续诊断
（D69 实测过：一张短泛化卡就能把 Top-1 命中率从 78% 拉到 67%）。
"""
from alembic import op

revision = "d91b47f2c8a5"
down_revision = "c3f0a74b9e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE case_cards ADD COLUMN enabled TINYINT(1) NOT NULL DEFAULT 1")


def downgrade() -> None:
    op.execute("ALTER TABLE case_cards DROP COLUMN enabled")
