"""rule_scope 增加 service（服务状态告警，FR-415）

Revision ID: c3f0a74b9e21
Revises: b5c7e91a4f38

为什么要迁移（而不是只改模型）：`alert_rules.scope` 是 **ENUM** 列——只改模型不改库，
写入 `scope='service'` 会被 MySQL/SeekDB 静默截断成空串（D47-1 就是同一类坑：
purpose ENUM 不同步导致"AI 生成"每次静默降级）。ENUM 加值在 MySQL 上必须 MODIFY 整列。
"""
from alembic import op

revision = "c3f0a74b9e21"
down_revision = "e7c41b9a2d38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alert_rules MODIFY scope "
               "ENUM('metric','probe','prom','service') NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE alert_rules MODIFY scope "
               "ENUM('metric','probe','prom') NOT NULL")
