"""P0：告警新鲜度（last_eval_at / stale / stale_reason）

背景：
  · alert_cur_events 是持久表；规则停用/删除、平台重启、数据源不可达时，
    活跃行会一直留在表里，界面持续显示 firing，事件收敛和 AI 诊断把它当当前证据；
  · 实测 L3b/L4a 的误诊就是被历史/僵尸告警污染。

字段：
  last_eval_at  最近一次被规则成功评估的时间（平台时间）
  stale         是否已陈旧（不参与通知/聚类/诊断）
  stale_reason  陈旧原因（rule_disabled / rule_deleted / no_evaluation / …）
"""
from alembic import op
import sqlalchemy as sa

revision = "c2d3e4f5a6b7"
down_revision = "b1a2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert_cur_events", sa.Column("last_eval_at", sa.DateTime(), nullable=True))
    op.add_column("alert_cur_events", sa.Column("stale", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("alert_cur_events", sa.Column("stale_reason", sa.String(64), nullable=False, server_default=""))
    op.create_index("ix_alert_cur_stale", "alert_cur_events", ["stale"])


def downgrade() -> None:
    op.drop_index("ix_alert_cur_stale", table_name="alert_cur_events")
    op.drop_column("alert_cur_events", "stale_reason")
    op.drop_column("alert_cur_events", "stale")
    op.drop_column("alert_cur_events", "last_eval_at")
