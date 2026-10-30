"""D12：通知渠道熔断（连续失败自动冷却）

背景（夯实轮实测）：
测 file:// 拦截时建了两个坏渠道，几分钟内 notification_records 累积 **33 条 failed**——
说明坏渠道（URL 写错、对端拒绝、被限额）会被**每次告警反复重试**，既不解决问题，
又让台账/日志膨胀、拖慢通知主流程（每次都要等 apprise 超时）。

本迁移给 notification_channels 加：
- `fail_streak`：连续失败次数（成功一次即清零）
- `disabled_until`：冷却期截止时刻（空 = 未熔断）

熔断规则（services/notify.py）：
连续失败 ≥ `NOTIFY_FAIL_THRESHOLD`（默认 5）→ 冷却 `NOTIFY_COOLDOWN_MIN`（默认 30 分钟），
冷却期内该渠道被跳过（且**不写失败台账**，避免刷屏）；冷却到期自动重试一次，
成功即恢复、失败则继续冷却。
"""
from alembic import op
import sqlalchemy as sa

revision = 'd3f8a1c47b92'
down_revision = 'a7c2e91b3d04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_channels", sa.Column("fail_streak", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("notification_channels", sa.Column("disabled_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_channels", "disabled_until")
    op.drop_column("notification_channels", "fail_streak")
