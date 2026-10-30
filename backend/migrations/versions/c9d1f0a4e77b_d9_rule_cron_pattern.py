"""D9-fix: alert_rules 增加 cron_pattern（FR-C1「评估间隔（秒）或 cron」）

此前只支持固定间隔评估，连字段都没有；需求 C1 明写"评估间隔（秒）或 cron"。
本迁移加列，引擎侧 `next_rule_delay()` 按 cron 求下次触发时刻（非法表达式回落固定间隔）。

见 03 §1.9、04 §2。
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9d1f0a4e77b'
down_revision = 'b7c4e1a95d20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert_rules", sa.Column("cron_pattern", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("alert_rules", "cron_pattern")
