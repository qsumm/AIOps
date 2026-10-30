"""D142/D143：AI 阈值推荐（无表变更）+ 预测型自愈列。

heal_policies 加 predict_min（预测预警提前量，分钟；0=关闭）。
预测命中走 heal_policy_runs（status=predicted）+ notify_attention 全渠道通知——
第一版刻意保守：只通知不动手（预测有误报，自动动作跑在误报上比错过真故障更危险）。

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("heal_policies",
                  sa.Column("predict_min", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("heal_policies", "predict_min")
