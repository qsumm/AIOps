"""D171（交付规范化）：可编辑配置类表补 updated_at

盘点结论（38 表全量）：时序/事件/运行记录类表（alert_cur_events、metric_series、
notification_records、rca_findings 等）语义上不可变或以业务时间戳为准，不加；
**用户可编辑的配置类表**（渠道/模型/用户/订阅/业务组/巡检/自愈策略/抑制）此前
缺 updated_at——"这条配置最后一次被谁/什么时候改过"无法回答，交付前补齐。
"""
from alembic import op
import sqlalchemy as sa

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

TABLES = ["alert_mutes", "notification_channels", "llm_configs", "users",
          "alert_subscribes", "busi_groups", "busi_group_members",
          "inspections", "heal_policies"]


def upgrade() -> None:
    for t in TABLES:
        op.add_column(t, sa.Column("updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for t in TABLES:
        op.drop_column(t, "updated_at")
