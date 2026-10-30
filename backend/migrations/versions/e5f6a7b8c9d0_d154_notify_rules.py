"""D154：通知路由四表（FR-423）。

notify_rules         通知规则（配置与 Pipeline 走 JSON，与夜莺同构）
alert_subscribes    订阅规则（订阅方自配，不改被订阅的规则）
busi_groups         业务组（主机归属 + 路由标签；本轮不做权限边界）
busi_group_members  业务组成员（团队级 + 读写标记）

老路径 AlertRule.channels_by_level（按级别选渠道名）**完全保留**，两套并存且互斥。

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notify_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("busi_group_id", sa.BigInteger(), nullable=True),
        sa.Column("configs", sa.JSON(), nullable=False),
        sa.Column("pipelines", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_nrule_enabled", "notify_rules", ["enabled"], unique=False)

    op.create_table(
        "alert_subscribes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("busi_group_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("severity_filter", sa.JSON(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_sub_rule", "alert_subscribes", ["rule_id"], unique=False)

    op.create_table(
        "busi_groups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "busi_group_members",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("busi_group_id", sa.BigInteger(), nullable=False),
        sa.Column("team", sa.String(length=64), nullable=False),
        sa.Column("permission", sa.Enum("read", "write", name="busi_permission"),
                  nullable=False, server_default="read"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_bgm_group", "busi_group_members", ["busi_group_id", "team"], unique=True)


def downgrade() -> None:
    op.drop_index("idx_bgm_group", table_name="busi_group_members")
    op.drop_table("busi_group_members")
    op.drop_table("busi_groups")
    op.drop_index("idx_sub_rule", table_name="alert_subscribes")
    op.drop_table("alert_subscribes")
    op.drop_index("idx_nrule_enabled", table_name="notify_rules")
    op.drop_table("notify_rules")
