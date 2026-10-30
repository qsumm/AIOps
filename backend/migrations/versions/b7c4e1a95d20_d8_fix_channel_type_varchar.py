"""D8-fix: notification_channels.type 由 ENUM 放开为 VARCHAR(32)

问题（D8 全量矩阵实测）：type 被数据库 ENUM 限死在 feishu/dingtalk/email/webhook 四值上，
但通知传输层（ADR-002，apprise）支持任意 scheme。用户按 apprise 习惯填 `json`/`slack`
等类型时，INSERT 直接撞库：`(1265, "Data truncated for column 'type'")` → HTTP 500，
既没有可读提示，也把"150+ 通知渠道"的能力堵死（只有 apprise_url 逃生口能绕过）。

修复：列放开为 VARCHAR(32)，类型合法性改由应用层 `notify_transport.validate_channel()`
显式校验（不合法 → HTTP 400 + 人话），不再依赖数据库 ENUM。

顺带规避 04 §12 记录的 SeekDB 禁忌：ENUM 列不适合做索引/过滤列。

见 03 §1.16、04 §7。
"""
from alembic import op

revision = 'b7c4e1a95d20'
down_revision = '13bddd4dad5e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SeekDB 实测支持 MODIFY；表内 config_json 等列不受影响
    op.execute("ALTER TABLE notification_channels MODIFY type VARCHAR(32) NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE notification_channels SET type = 'webhook' "
               "WHERE type NOT IN ('feishu','dingtalk','email','webhook')")
    op.execute("ALTER TABLE notification_channels MODIFY "
               "type ENUM('feishu','dingtalk','email','webhook') NOT NULL")
