"""D154：告警规则级事件 Pipeline + 事件注解列（FR-423）。

- alert_rules.event_pipelines (JSON)：告警规则级 Pipeline，在**事件产生时**执行
  （算指纹之前）：改标签（→ 进指纹）、富化上下文、或 drop（不产生事件）。
  位置与语义对齐夜莺的 alert_rule.event_relabel_config。
- alert_cur_events.annotations / alert_his_events.annotations (JSON)：**注解**
  （只给人看的上下文，**不参与**屏蔽/路由匹配——标签才参与）。

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert_rules", sa.Column("event_pipelines", sa.JSON(), nullable=True))
    op.add_column("alert_cur_events", sa.Column("annotations", sa.JSON(), nullable=True))
    op.add_column("alert_his_events", sa.Column("annotations", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("alert_his_events", "annotations")
    op.drop_column("alert_cur_events", "annotations")
    op.drop_column("alert_rules", "event_pipelines")
