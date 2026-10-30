"""D9-fix: 处置执行的三模式 + 审批凭据（FR-E2 的"三选"与 HITL 可追溯）

背景（D9 复盘发现的两个问题）：
1. 需求 FR-E2 写的是"**执行方式三选**：仅建议（默认）/ 人工确认执行 / 白名单自动执行"，
   而实现里只有 `whitelist_enabled` 一个布尔，等于把"三选"压成了"开关"；
2. `approved=true` 由**客户端自报**：谁都能 `curl -d '{"approved":true}'` 过掉，
   action_audit 里那句"已放行"证明不了有人审过——审计不可信。

本迁移：
- runbooks 加 `execution_mode`（suggest|manual|auto），并把老数据按
  `whitelist_enabled=1 → auto` 回填（whitelist_enabled 保留为 legacy 同步列）；
- action_audit 加 `approval_mode / approved_by / approved_at`，让"谁在何时批准"可查；
- 新增 `action_approvals` 表：确认动作先落审批记录（操作者/时间/IP/一次性 token 哈希/命令快照），
  执行时须出示 token → 不可重放、可追溯（V2 远程执行通道直接复用这套凭据）。

见 03 §1.17、教程《08-处置闭环与护栏》。
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5b91c3a7d28'
down_revision = 'd1a7c02be9f4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runbooks", sa.Column("execution_mode", sa.String(16), nullable=False, server_default="manual"))
    op.execute("UPDATE runbooks SET execution_mode = 'auto' WHERE whitelist_enabled = 1")

    op.add_column("action_audit", sa.Column("approval_mode", sa.String(16), nullable=False, server_default=""))
    op.add_column("action_audit", sa.Column("approved_by", sa.String(64), nullable=False, server_default=""))
    op.add_column("action_audit", sa.Column("approved_at", sa.DATETIME(), nullable=True))

    op.create_table(
        "action_approvals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("runbook_id", sa.BigInteger(), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operator", sa.String(64), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False, server_default=""),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("expires_at", sa.DATETIME(), nullable=False),
        sa.Column("used_at", sa.DATETIME(), nullable=True),
        sa.Column("created_at", sa.DATETIME(), server_default=sa.func.now()),
    )
    # 索引手写（SeekDB 的索引反射不可靠，见 04 §10）
    op.execute("ALTER TABLE action_approvals ADD INDEX idx_appr_rb (runbook_id, step_idx)")
    op.execute("ALTER TABLE action_approvals ADD INDEX idx_appr_token (token_hash)")


def downgrade() -> None:
    op.drop_table("action_approvals")
    op.drop_column("action_audit", "approved_at")
    op.drop_column("action_audit", "approved_by")
    op.drop_column("action_audit", "approval_mode")
    op.drop_column("runbooks", "execution_mode")
